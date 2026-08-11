"""
Bulk SMS/email campaigns -- broadcast one message to a filtered group of
members and/or clients (e.g. "everyone overdue in Nairobi region"),
alongside the existing one-off per-recipient sends (loan reminders,
receipts) already handled by core/sms.py, core/mailer.py, and
core/utils.notify().

Recipients are resolved server-side from the same filters shown in the
preview, at send time -- not from a stored list -- so a campaign always
reaches whoever currently matches the filter, not a stale snapshot from
when the form was opened.

Each individual message still goes through send_sms_async/send_email_async
(same as everywhere else in the app), so per-recipient delivery status
lands in the existing sms_log/email_log tables. The `campaigns` table
(migration 23) records the broadcast-level summary: who matched the
filters, how many messages were queued, and how many records were skipped.
Individual delivery outcomes remain in the per-channel logs.
"""
from datetime import date
import hashlib
import html
import json
import os

import requests
from flask import Blueprint, current_app, request, jsonify, render_template
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from core.database import get_db, execute, utcnow
from core.auth import login_required, role_required, permission_required, get_current_user
from core.serializers import campaign_public
from core.utils import paginate, log_audit
from core.sms import normalize_phone, send_sms_async, is_configured as sms_configured
from core.mailer import send_email_async, is_configured as email_configured

campaigns_bp = Blueprint('campaigns', __name__)


def _fetch_by_ids(table, ids):
    """Look up specific members/clients by id, regardless of status/region --
    a hand-picked recipient was chosen deliberately, so it isn't silently
    dropped by the same active/region/overdue filters used for broad
    audiences."""
    if not ids:
        return []
    db = get_db()
    placeholders = ','.join(['%s'] * len(ids))
    rows = db.execute(
        f"SELECT id, first_name, last_name, phone, email FROM {table} WHERE id IN ({placeholders})",
        tuple(ids)
    ).fetchall()
    return [
        {
            'id': r['id'], 'type': table,
            'name': f"{r['first_name']} {r['last_name']}".strip(),
            'phone': r['phone'], 'email': r['email'],
        }
        for r in rows
    ]


def _resolve_recipients(audience_type, region, overdue_only, recipient_ids=None):
    """Returns a list of dicts: {name, phone, email, id, type}.

    audience_type is 'members', 'clients', 'both' (broad, filter-based
    audiences matched by region/overdue_only), or 'selected' (an explicit,
    hand-picked list of members/clients passed in recipient_ids as a list
    of {'type': 'members'|'clients', 'id': ...} dicts -- region and
    overdue_only are ignored in that case since the sender already chose
    exactly who should receive it).
    """
    if audience_type == 'selected':
        ids_by_table = {'members': [], 'clients': []}
        for entry in (recipient_ids or []):
            table = entry.get('type')
            if table in ids_by_table:
                ids_by_table[table].append(entry.get('id'))
        recipients = []
        for table, ids in ids_by_table.items():
            recipients += _fetch_by_ids(table, ids)
        return recipients

    db = get_db()
    recipients = []

    def _fetch(table, id_col):
        sql = f"SELECT id, first_name, last_name, phone, email FROM {table} WHERE status = 'active'"
        params = []
        if region:
            sql += " AND region = %s"
            params.append(region)
        if overdue_only:
            sql += f""" AND id IN (
                SELECT {id_col} FROM loans WHERE {id_col} IS NOT NULL AND id IN (
                    SELECT DISTINCT loan_id FROM loan_schedules
                    WHERE due_date < %s AND status IN ('pending', 'partial')
                )
            )"""
            params.append(date.today().isoformat())
        rows = db.execute(sql, tuple(params)).fetchall()
        return [
            {
                'id': r['id'], 'type': table,
                'name': f"{r['first_name']} {r['last_name']}".strip(),
                'phone': r['phone'], 'email': r['email'],
            }
            for r in rows
        ]

    if audience_type in ('members', 'both'):
        recipients += _fetch('members', 'member_id')
    if audience_type in ('clients', 'both'):
        recipients += _fetch('clients', 'client_id')

    return recipients


def _campaign_limit():
    try:
        return min(max(int(os.getenv('CAMPAIGN_MAX_RECIPIENTS', '250')), 1), 500)
    except ValueError:
        return 250


def _prepare_recipients(recipients, channel):
    deliverable = []
    seen_contacts = set()
    missing_contact_count = 0
    duplicate_contact_count = 0

    for recipient in recipients:
        if channel == 'sms':
            contact = normalize_phone(recipient['phone'])
        else:
            email = (recipient['email'] or '').strip().lower()
            contact = email if '@' in email else None

        if not contact:
            missing_contact_count += 1
            continue
        if contact in seen_contacts:
            duplicate_contact_count += 1
            continue

        seen_contacts.add(contact)
        deliverable.append({**recipient, 'contact': contact})

    return deliverable, {
        'matched_count': len(recipients),
        'deliverable_count': len(deliverable),
        'missing_contact_count': missing_contact_count,
        'duplicate_contact_count': duplicate_contact_count,
    }


def _recipient_fingerprint(recipients):
    values = sorted(
        f"{recipient['type']}:{recipient['id']}:{recipient['contact']}"
        for recipient in recipients
    )
    return hashlib.sha256('\n'.join(values).encode()).hexdigest()


def _selection_fingerprint(recipient_ids):
    """Fingerprints the sender's raw hand-picked selection (not the
    resolved recipient rows) so the preview token also catches the case
    where the sender changes *who they picked* between preview and send,
    even if that change happens not to alter the deliverable count."""
    if not recipient_ids:
        return None
    values = sorted(f"{e.get('type')}:{e.get('id')}" for e in recipient_ids)
    return hashlib.sha256('\n'.join(values).encode()).hexdigest()


def _parse_recipient_ids(raw):
    """Validates the client-supplied selection list: must be a list of
    {'type': 'members'|'clients', 'id': int}. Returns (list, error) --
    error is a (message, status) tuple on bad input."""
    if not isinstance(raw, list):
        return None, ('recipient_ids must be a list', 400)
    if not raw:
        return None, ('Select at least one member or client', 400)
    if len(raw) > 500:
        return None, ('Too many recipients selected', 400)
    parsed = []
    for entry in raw:
        if not isinstance(entry, dict) or entry.get('type') not in ('members', 'clients'):
            return None, ('Each selected recipient needs a valid type', 400)
        try:
            rid = int(entry.get('id'))
        except (TypeError, ValueError):
            return None, ('Each selected recipient needs a valid id', 400)
        parsed.append({'type': entry['type'], 'id': rid})
    return parsed, None


def _preview_serializer():
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='campaign-preview')


def _create_preview_token(channel, audience_type, region, overdue_only, recipients, recipient_ids=None):
    return _preview_serializer().dumps({
        'channel': channel,
        'audience_type': audience_type,
        'region': region,
        'overdue_only': overdue_only,
        'recipient_ids_fingerprint': _selection_fingerprint(recipient_ids),
        'recipient_count': len(recipients),
        'recipient_fingerprint': _recipient_fingerprint(recipients),
        'user_id': get_current_user()['id'],
    })


def _validate_preview_token(token, channel, audience_type, region, overdue_only, recipients, recipient_ids=None):
    if not token:
        return 'Refresh the recipient preview before sending the campaign', 400
    try:
        preview = _preview_serializer().loads(token, max_age=15 * 60)
    except SignatureExpired:
        return 'The recipient preview expired. Refresh it before sending the campaign', 409
    except BadSignature:
        return 'The recipient preview is invalid. Refresh it before sending the campaign', 400

    expected = {
        'channel': channel,
        'audience_type': audience_type,
        'region': region,
        'overdue_only': overdue_only,
        'recipient_ids_fingerprint': _selection_fingerprint(recipient_ids),
        'recipient_count': len(recipients),
        'recipient_fingerprint': _recipient_fingerprint(recipients),
        'user_id': get_current_user()['id'],
    }
    if preview != expected:
        return 'The recipient list changed. Refresh the preview and confirm the campaign again', 409
    return None


CAMPAIGN_TEMPLATES = {
    'overdue_reminder': {
        'label': 'Overdue payment reminder',
        'sms': "Dear {name}, our records show your Jodala Microfinance loan installment is now overdue. Kindly settle payment promptly to avoid penalty charges. For assistance, please contact us.",
        'email_subject': "Overdue Loan Installment Reminder",
        'email': "Dear {name},\n\nThis is a reminder that your loan installment with Jodala Microfinance is currently overdue. Please make a payment as soon as possible to avoid additional penalties.\n\nIf you're facing difficulties or need to discuss your repayment plan, please contact us -- we're happy to help.\n\nThank you,\nJodala Microfinance",
    },
    'welcome': {
        'label': 'Welcome message',
        'sms': "Dear {name}, welcome to Jodala Microfinance. Thank you for choosing us. For any queries regarding your account, please do not hesitate to contact us.",
        'email_subject': "Welcome to Jodala Microfinance",
        'email': "Dear {name},\n\nWelcome to Jodala Microfinance! We're glad to have you as part of our community.\n\nIf you have any questions about your account, loans, or savings, don't hesitate to reach out.\n\nWarm regards,\nJodala Microfinance",
    },
    'payment_confirmation': {
        'label': 'General payment thank-you',
        'sms': "Dear {name}, we confirm receipt of your recent payment to Jodala Microfinance. Thank you for your continued trust and prompt settlement.",
        'email_subject': "Thank You for Your Payment",
        'email': "Dear {name},\n\nThank you for your recent payment. We appreciate your continued partnership with Jodala Microfinance.\n\nIf you have any questions about your account, feel free to reach out.\n\nBest regards,\nJodala Microfinance",
    },
    'holiday_greeting': {
        'label': 'Holiday / seasonal greeting',
        'sms': "Dear {name}, season's greetings from Jodala Microfinance. We thank you for your valued partnership and wish you a joyful holiday season.",
        'email_subject': "Season's Greetings from Jodala Microfinance",
        'email': "Dear {name},\n\nAs the year draws to a close, we want to thank you for being a valued member of the Jodala Microfinance family.\n\nWishing you and your loved ones a joyful holiday season.\n\nWarm regards,\nJodala Microfinance",
    },
    'new_product': {
        'label': 'New product / service announcement',
        'sms': "Dear {name}, Jodala Microfinance is pleased to announce new savings and loan products. Kindly visit your nearest branch or contact us to learn more.",
        'email_subject': "New Products Now Available",
        'email': "Dear {name},\n\nWe're excited to let you know that Jodala Microfinance now offers new savings and loan products designed to serve you better.\n\nVisit your nearest branch or contact us to learn more about what's available.\n\nBest regards,\nJodala Microfinance",
    },
    'payment_due_soon': {
        'label': 'Upcoming payment reminder',
        'sms': "Dear {name}, this is a friendly reminder to prepare for your upcoming Jodala Microfinance loan repayment. Thank you for paying on time.",
        'email_subject': "Reminder: Upcoming Loan Repayment",
        'email': "Dear {name},\n\nThis is a friendly reminder that your next Jodala Microfinance loan repayment is coming up soon.\n\nPlease ensure you have made the necessary arrangements to pay on time. If you need help, kindly contact us before the due date.\n\nThank you,\nJodala Microfinance",
    },
    'savings_encouragement': {
        'label': 'Savings encouragement',
        'sms': "Dear {name}, every small saving brings you closer to your goals. Make a deposit into your Jodala Microfinance savings account today.",
        'email_subject': "Grow Your Savings, One Deposit at a Time",
        'email': "Dear {name},\n\nYour savings can help you prepare for opportunities and unexpected needs. Even a small, regular deposit makes a difference over time.\n\nVisit us or use your preferred payment channel to grow your Jodala Microfinance savings today.\n\nWarm regards,\nJodala Microfinance",
    },
    'loan_product_promotion': {
        'label': 'Loan product promotion',
        'sms': "Dear {name}, looking for support for your business, school fees, or personal goals? Ask Jodala Microfinance about our flexible loan options.",
        'email_subject': "Find a Loan That Supports Your Goals",
        'email': "Dear {name},\n\nWhether you are growing a business, paying school fees, or meeting another important need, Jodala Microfinance has loan options that may suit you.\n\nContact us or visit your nearest branch to learn about eligibility and repayment options.\n\nBest regards,\nJodala Microfinance",
    },
    'account_update': {
        'label': 'Account update request',
        'sms': "Dear {name}, please help us keep your records current. Visit Jodala Microfinance to confirm or update your phone number, email, and identification details.",
        'email_subject': "Help Us Keep Your Account Details Current",
        'email': "Dear {name},\n\nTo serve you better, please ensure that your contact and identification details are up to date.\n\nKindly visit Jodala Microfinance or contact our team to confirm or update your phone number, email address, and other account information.\n\nThank you,\nJodala Microfinance",
    },
    'branch_hours': {
        'label': 'Branch hours / service update',
        'sms': "Dear {name}, Jodala Microfinance has an important service update. Please contact your branch or check our official channels for the latest opening hours and assistance.",
        'email_subject': "Important Service Update from Jodala Microfinance",
        'email': "Dear {name},\n\nWe have an important service update to share with you. For the latest branch opening hours and service information, please contact your Jodala Microfinance branch or our support team.\n\nThank you for your understanding.\n\nJodala Microfinance",
    },
    'customer_appreciation': {
        'label': 'Customer appreciation',
        'sms': "Dear {name}, thank you for choosing Jodala Microfinance. We value your trust and remain committed to supporting your financial journey.",
        'email_subject': "Thank You for Banking with Jodala Microfinance",
        'email': "Dear {name},\n\nThank you for choosing Jodala Microfinance. Your trust means a great deal to us.\n\nWe remain committed to providing reliable financial services and supporting your goals at every step.\n\nWarm regards,\nJodala Microfinance",
    },
    'financial_literacy': {
        'label': 'Financial tips invitation',
        'sms': "Dear {name}, strengthen your financial future with good saving, budgeting, and repayment habits. Contact Jodala Microfinance for financial guidance.",
        'email_subject': "Simple Steps Toward Financial Wellbeing",
        'email': "Dear {name},\n\nGood financial habits can make a meaningful difference. Creating a budget, saving regularly, and paying loans on time are important steps toward your goals.\n\nContact Jodala Microfinance if you would like guidance on managing your savings or loan repayments.\n\nBest regards,\nJodala Microfinance",
    },
    'loan_approval_celebration': {
        'label': 'Loan approval congratulations',
        'sms': "Dear {name}, congratulations on your approved Jodala Microfinance loan. Our team will guide you on the next steps for disbursement.",
        'email_subject': "Congratulations: Your Loan Has Been Approved",
        'email': "Dear {name},\n\nCongratulations! Your Jodala Microfinance loan has been approved.\n\nOur team will guide you through the remaining disbursement steps. Please contact us if you have any questions.\n\nWarm regards,\nJodala Microfinance",
    },
    'loan_application_followup': {
        'label': 'Loan application follow-up',
        'sms': "Dear {name}, thank you for your loan application. Jodala Microfinance is reviewing it and will share an update as soon as possible.",
        'email_subject': "Update on Your Loan Application",
        'email': "Dear {name},\n\nThank you for your loan application. Our team is reviewing the information provided and will share an update as soon as possible.\n\nIf we need anything further, we will contact you directly.\n\nThank you,\nJodala Microfinance",
    },
    'loan_completion': {
        'label': 'Loan completion congratulations',
        'sms': "Dear {name}, congratulations on completing your Jodala Microfinance loan repayments. Thank you for your commitment and trust.",
        'email_subject': "Congratulations on Completing Your Loan",
        'email': "Dear {name},\n\nCongratulations on completing your loan repayments. Thank you for your commitment and for choosing Jodala Microfinance.\n\nWe look forward to continuing to support your financial goals.\n\nWarm regards,\nJodala Microfinance",
    },
    'referral_invitation': {
        'label': 'Refer a friend',
        'sms': "Dear {name}, share the benefits of Jodala Microfinance with a friend or family member who may need savings or loan services.",
        'email_subject': "Share Jodala Microfinance with Someone You Know",
        'email': "Dear {name},\n\nDo you know someone who could benefit from trusted savings or loan services? Invite them to learn more about Jodala Microfinance.\n\nThank you for being part of our community.\n\nBest regards,\nJodala Microfinance",
    },
    'member_meeting': {
        'label': 'Member meeting invitation',
        'sms': "Dear {name}, you are invited to an upcoming Jodala Microfinance member meeting. Please contact us for the date, time, venue, and agenda.",
        'email_subject': "You Are Invited to a Member Meeting",
        'email': "Dear {name},\n\nYou are invited to an upcoming Jodala Microfinance member meeting. It will be a chance to receive updates, ask questions, and engage with our team.\n\nPlease contact us for the date, time, venue, and agenda.\n\nJodala Microfinance",
    },
    'training_invitation': {
        'label': 'Training / workshop invitation',
        'sms': "Dear {name}, Jodala Microfinance invites you to our upcoming financial or business training. Contact us to reserve your place and get event details.",
        'email_subject': "Invitation: Financial and Business Training",
        'email': "Dear {name},\n\nJodala Microfinance invites you to an upcoming financial or business training session. The session is designed to help you build practical skills for your goals.\n\nContact us to reserve your place and receive the event details.\n\nWarm regards,\nJodala Microfinance",
    },
    'service_survey': {
        'label': 'Customer feedback request',
        'sms': "Dear {name}, your feedback helps us serve you better. Please share your experience with Jodala Microfinance by contacting our team.",
        'email_subject': "We Would Value Your Feedback",
        'email': "Dear {name},\n\nYour feedback helps us improve our services. We would appreciate hearing about your experience with Jodala Microfinance and how we can serve you better.\n\nPlease contact our team to share your suggestions.\n\nThank you,\nJodala Microfinance",
    },
    'fraud_awareness': {
        'label': 'Security and fraud awareness',
        'sms': "Dear {name}, keep your account safe: never share your PIN, password, or verification code. Contact Jodala Microfinance immediately if you notice suspicious activity.",
        'email_subject': "Keep Your Account Safe",
        'email': "Dear {name},\n\nPlease help protect your account. Never share your PIN, password, one-time verification code, or other sensitive information with anyone.\n\nIf you notice suspicious activity or receive an unexpected request for account information, contact Jodala Microfinance immediately.\n\nJodala Microfinance",
    },
    'public_holiday_notice': {
        'label': 'Public holiday notice',
        'sms': "Dear {name}, please note that Jodala Microfinance services may operate on adjusted hours during the upcoming public holiday. Contact us for details.",
        'email_subject': "Public Holiday Service Notice",
        'email': "Dear {name},\n\nPlease note that Jodala Microfinance branch hours and services may be adjusted during the upcoming public holiday.\n\nContact our team for the latest information and any assistance you may need.\n\nThank you,\nJodala Microfinance",
    },
    'year_end_thanks': {
        'label': 'Year-end thank you',
        'sms': "Dear {name}, thank you for your trust throughout the year. Jodala Microfinance wishes you a peaceful holiday season and a prosperous new year.",
        'email_subject': "Thank You for a Wonderful Year",
        'email': "Dear {name},\n\nAs the year comes to a close, we sincerely thank you for your trust and partnership.\n\nJodala Microfinance wishes you and your loved ones a peaceful holiday season and a prosperous new year.\n\nWarm regards,\nJodala Microfinance",
    },
}


@campaigns_bp.route('/api/templates', methods=['GET'])
@login_required
def list_campaign_templates():
    return jsonify({
        'templates': [{'key': k, 'label': v['label']} for k, v in CAMPAIGN_TEMPLATES.items()]
    })


@campaigns_bp.route('/api/templates/<key>', methods=['GET'])
@login_required
def get_campaign_template(key):
    channel = request.args.get('channel', 'sms')
    tpl = CAMPAIGN_TEMPLATES.get(key)
    if not tpl:
        return jsonify({'error': 'Unknown template'}), 404
    if channel == 'email':
        return jsonify({'message': tpl['email'], 'subject': tpl['email_subject']})
    return jsonify({'message': tpl['sms']})


@campaigns_bp.route('/api/draft', methods=['POST'])
@login_required
@role_required('admin', 'loan_officer')
@permission_required('campaigns.draft')
def draft_campaign_message():
    """Generate a custom campaign message with ChatGPT via the OpenAI API.
    Requires OPENAI_API_KEY to be set in the server environment."""
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        return jsonify({'error': 'ChatGPT drafting is not configured -- set OPENAI_API_KEY on the server'}), 400

    data = request.get_json() or {}
    prompt = (data.get('prompt') or '').strip()
    channel = data.get('channel', 'sms')
    audience_type = data.get('audience_type', 'both')

    if not prompt:
        return jsonify({'error': 'Describe what the message should say'}), 400

    audience_desc = {
        'members': 'registered members', 'clients': 'non-member client borrowers', 'both': 'members and clients',
        'selected': 'a hand-picked group of members and/or clients',
    }.get(audience_type, 'members and clients')

    length_hint = (
        "Keep it under 300 characters, plain text, no markdown, no subject line -- this is a single SMS."
        if channel == 'sms' else
        "Write a short, warm email body (3-6 short paragraphs), plain text, no markdown. "
        "Also provide a separate one-line subject."
    )

    system_prompt = (
        "You draft short outbound messages for a microfinance institution called Jodala Microfinance, "
        f"to be sent to {audience_desc}. Use the placeholder {{name}} wherever the recipient's name should "
        "go -- it will be substituted per-recipient at send time. Be professional, warm, and concise. "
        "Never invent specific loan amounts, dates, or figures that weren't given to you. " + length_hint
    )

    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json={
                # Keep the model configurable so deployments can choose the
                # OpenAI model appropriate for their cost/quality needs.
                "model": os.getenv('OPENAI_CAMPAIGN_MODEL', 'gpt-5'),
                "instructions": system_prompt,
                "input": prompt,
                "max_output_tokens": 500,
                "store": False,
            },
            timeout=20,
        )
        resp.raise_for_status()
        result = resp.json()
        # Responses API may provide a convenient output_text field, while the
        # canonical payload contains message/content items. Handle both.
        text = (result.get('output_text') or '').strip()
        if not text:
            text = ''.join(
                content.get('text', '')
                for item in result.get('output', [])
                for content in item.get('content', [])
                if content.get('type') == 'output_text'
            ).strip()
        if not text:
            return jsonify({'error': 'ChatGPT returned an empty draft -- try rephrasing your prompt'}), 502

        subject = None
        if channel == 'email':
            lines = text.split('\n', 1)
            if lines[0].lower().startswith('subject:'):
                subject = lines[0].split(':', 1)[1].strip()
                text = lines[1].strip() if len(lines) > 1 else text

        if '{name}' not in text:
            text = f"Dear {{name}}, " + text[0].lower() + text[1:] if channel == 'sms' else f"Dear {{name}},\n\n{text}"

        return jsonify({'message': text, 'subject': subject})
    except requests.exceptions.HTTPError as e:
        # Surface the OpenAI API's actual error message instead of a generic
        # "400 Bad Request" so misconfiguration (bad model name, bad key, etc.)
        # is diagnosable from the UI/logs.
        detail = None
        try:
            detail = e.response.json().get('error', {}).get('message')
        except Exception:
            pass
        return jsonify({'error': f'ChatGPT drafting failed: {detail or e}'}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'ChatGPT drafting failed: {e}'}), 502


@campaigns_bp.route('/')
@login_required
def index():
    regions = get_db().execute("SELECT name FROM regions WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template(
        'campaigns/index.html', user=get_current_user(),
        regions=[r['name'] for r in regions],
        sms_ready=sms_configured(), email_ready=email_configured(),
    )


@campaigns_bp.route('/api/search-recipients', methods=['GET'])
@login_required
def search_recipients():
    """Looks up members/clients by name or phone so the sender can hand-pick
    specific recipients for the 'selected' audience mode, instead of only
    broadcasting to everyone matching a region/status filter."""
    q = (request.args.get('q') or '').strip()
    audience_type = request.args.get('audience_type', 'both')
    if audience_type not in ('members', 'clients', 'both'):
        return jsonify({'error': 'Invalid audience_type'}), 400
    if len(q) < 2:
        return jsonify({'results': []})

    db = get_db()
    like = f"%{q}%"
    results = []

    def _search(table):
        rows = db.execute(
            f"""SELECT id, first_name, last_name, phone, email, status FROM {table}
                WHERE (first_name || ' ' || last_name) ILIKE %s OR phone ILIKE %s
                ORDER BY first_name, last_name LIMIT 15""",
            (like, like)
        ).fetchall()
        return [
            {
                'id': r['id'], 'type': table,
                'name': f"{r['first_name']} {r['last_name']}".strip(),
                'phone': r['phone'], 'email': r['email'], 'status': r['status'],
            }
            for r in rows
        ]

    if audience_type in ('members', 'both'):
        results += _search('members')
    if audience_type in ('clients', 'both'):
        results += _search('clients')

    return jsonify({'results': results[:20]})


@campaigns_bp.route('/api/preview', methods=['POST'])
@login_required
def preview_recipients():
    """Returns the count (and a short sample) of who a campaign with these
    filters would currently reach -- lets the sender confirm before
    committing to an actual send."""
    data = request.get_json() or {}
    channel = data.get('channel', 'sms')
    audience_type = data.get('audience_type', 'both')
    region = data.get('region') or None
    overdue_only = bool(data.get('overdue_only'))
    if channel not in ('sms', 'email'):
        return jsonify({'error': 'channel must be "sms" or "email"'}), 400
    if audience_type not in ('members', 'clients', 'both', 'selected'):
        return jsonify({'error': 'Invalid audience_type'}), 400

    recipient_ids = None
    if audience_type == 'selected':
        recipient_ids, err = _parse_recipient_ids(data.get('recipient_ids'))
        if err:
            return jsonify({'error': err[0]}), err[1]

    recipients = _resolve_recipients(audience_type, region, overdue_only, recipient_ids)
    deliverable, summary = _prepare_recipients(recipients, channel)
    max_recipient_count = _campaign_limit()
    return jsonify({
        **summary,
        'count': summary['deliverable_count'],
        'sample': [recipient['name'] for recipient in deliverable[:8]],
        'max_recipient_count': max_recipient_count,
        'over_limit': summary['deliverable_count'] > max_recipient_count,
        'preview_token': _create_preview_token(
            channel, audience_type, region, overdue_only, deliverable, recipient_ids
        ),
    })


@campaigns_bp.route('/api', methods=['GET'])
@login_required
def list_campaigns():
    page = request.args.get('page', 1, type=int)
    rows, total, pages = paginate(
        "SELECT * FROM campaigns ORDER BY created_at DESC",
        "SELECT COUNT(*) FROM campaigns",
        (), page, 20
    )
    return jsonify({
        'campaigns': [campaign_public(c) for c in rows],
        'total': total, 'pages': pages,
    })


@campaigns_bp.route('/api', methods=['POST'])
@login_required
@role_required('admin', 'loan_officer')
@permission_required('campaigns.send')
def send_campaign():
    data = request.get_json() or {}
    channel = data.get('channel')
    audience_type = data.get('audience_type', 'both')
    region = data.get('region') or None
    overdue_only = bool(data.get('overdue_only'))
    message = (data.get('message') or '').strip()
    subject = (data.get('subject') or 'Jodala Microfinance').strip()

    if channel not in ('sms', 'email'):
        return jsonify({'error': 'channel must be "sms" or "email"'}), 400
    if not message:
        return jsonify({'error': 'Message is required'}), 400
    if audience_type not in ('members', 'clients', 'both', 'selected'):
        return jsonify({'error': 'Invalid audience_type'}), 400
    max_message_length = 640 if channel == 'sms' else 5000
    if len(message) > max_message_length:
        return jsonify({'error': f'{channel.upper()} messages are limited to {max_message_length} characters'}), 400
    if channel == 'email' and len(subject) > 200:
        return jsonify({'error': 'Email subjects are limited to 200 characters'}), 400

    if channel == 'sms' and not sms_configured():
        return jsonify({'error': 'SMS is not configured -- set it up in Settings > Notifications first'}), 400
    if channel == 'email' and not email_configured():
        return jsonify({'error': 'Email is not configured -- set it up in Settings > Notifications first'}), 400

    recipient_ids = None
    if audience_type == 'selected':
        recipient_ids, err = _parse_recipient_ids(data.get('recipient_ids'))
        if err:
            return jsonify({'error': err[0]}), err[1]

    recipients = _resolve_recipients(audience_type, region, overdue_only, recipient_ids)
    deliverable, summary = _prepare_recipients(recipients, channel)
    if not deliverable:
        return jsonify({'error': f'No recipients have a usable {channel} contact'}), 400
    max_recipient_count = _campaign_limit()
    if summary['deliverable_count'] > max_recipient_count:
        return jsonify({
            'error': f'This campaign has {summary["deliverable_count"]} deliverable recipients, exceeding the configured limit of {max_recipient_count}'
        }), 400
    preview_error = _validate_preview_token(
        data.get('preview_token'), channel, audience_type, region, overdue_only, deliverable, recipient_ids
    )
    if preview_error:
        return jsonify({'error': preview_error[0]}), preview_error[1]

    queued = 0
    for r in deliverable:
        personalized = message.replace('{name}', r['name'] or 'there')
        if channel == 'sms':
            send_sms_async(r['contact'], personalized)
        else:
            body_html = '<p>' + html.escape(personalized).replace('\n', '<br>') + '</p>'
            send_email_async(r['contact'], subject, personalized, body_html)
        queued += 1

    skipped = summary['missing_contact_count'] + summary['duplicate_contact_count']

    cur = execute(
        """INSERT INTO campaigns (channel, audience_type, region, overdue_only, message, subject,
                                   recipient_count, sent_count, failed_count, created_by, created_at,
                                   recipient_ids)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (channel, audience_type, region, 1 if overdue_only else 0, message, subject,
         summary['matched_count'], queued, skipped, get_current_user()['id'], utcnow(),
         json.dumps(recipient_ids) if recipient_ids else None)
    )
    campaign_id = cur.lastrowid
    log_audit('SEND_CAMPAIGN', 'campaign', campaign_id,
              new_values={
                  'channel': channel,
                  'matched': summary['matched_count'],
                  'queued': queued,
                  'missing_contact': summary['missing_contact_count'],
                  'duplicate_contact': summary['duplicate_contact_count'],
              })

    return jsonify({
        'message': f'Campaign queued for {queued} recipient(s)' + (f', {skipped} skipped' if skipped else ''),
        'recipient_count': summary['matched_count'],
        'queued_count': queued,
        'skipped_count': skipped,
        'missing_contact_count': summary['missing_contact_count'],
        'duplicate_contact_count': summary['duplicate_contact_count'],
    })

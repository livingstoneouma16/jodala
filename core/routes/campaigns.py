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
(migration 23) only records the broadcast-level summary: who was
targeted, the message, and how many sends succeeded/failed.
"""
from datetime import date
import os

import requests
from flask import Blueprint, request, jsonify, render_template

from core.database import get_db, execute, utcnow
from core.auth import login_required, role_required, get_current_user
from core.serializers import campaign_public
from core.utils import paginate, log_audit
from core.sms import send_sms_async, is_configured as sms_configured
from core.mailer import send_email_async, is_configured as email_configured

campaigns_bp = Blueprint('campaigns', __name__)


def _resolve_recipients(audience_type, region, overdue_only):
    """Returns a list of dicts: {name, phone, email, id, type}. audience_type
    is 'members', 'clients', or 'both'. overdue_only restricts to
    borrowers with at least one currently-overdue loan installment."""
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


CAMPAIGN_TEMPLATES = {
    'overdue_reminder': {
        'label': 'Overdue payment reminder',
        'sms': "Hi {name}, this is a reminder from Jodala Microfinance that your loan installment is overdue. Please make a payment as soon as possible to avoid penalties. Contact us if you need assistance.",
        'email_subject': "Overdue Loan Installment Reminder",
        'email': "Dear {name},\n\nThis is a reminder that your loan installment with Jodala Microfinance is currently overdue. Please make a payment as soon as possible to avoid additional penalties.\n\nIf you're facing difficulties or need to discuss your repayment plan, please contact us -- we're happy to help.\n\nThank you,\nJodala Microfinance",
    },
    'welcome': {
        'label': 'Welcome message',
        'sms': "Welcome to Jodala Microfinance, {name}! We're glad to have you with us. Reach out anytime if you have questions about your account.",
        'email_subject': "Welcome to Jodala Microfinance",
        'email': "Dear {name},\n\nWelcome to Jodala Microfinance! We're glad to have you as part of our community.\n\nIf you have any questions about your account, loans, or savings, don't hesitate to reach out.\n\nWarm regards,\nJodala Microfinance",
    },
    'payment_confirmation': {
        'label': 'General payment thank-you',
        'sms': "Hi {name}, thank you for your recent payment to Jodala Microfinance. We appreciate your continued trust in us.",
        'email_subject': "Thank You for Your Payment",
        'email': "Dear {name},\n\nThank you for your recent payment. We appreciate your continued partnership with Jodala Microfinance.\n\nIf you have any questions about your account, feel free to reach out.\n\nBest regards,\nJodala Microfinance",
    },
    'holiday_greeting': {
        'label': 'Holiday / seasonal greeting',
        'sms': "Season's greetings from all of us at Jodala Microfinance, {name}! Wishing you a joyful holiday season.",
        'email_subject': "Season's Greetings from Jodala Microfinance",
        'email': "Dear {name},\n\nAs the year draws to a close, we want to thank you for being a valued member of the Jodala Microfinance family.\n\nWishing you and your loved ones a joyful holiday season.\n\nWarm regards,\nJodala Microfinance",
    },
    'new_product': {
        'label': 'New product / service announcement',
        'sms': "Hi {name}, Jodala Microfinance now offers new savings and loan products. Visit your nearest branch or contact us to learn more.",
        'email_subject': "New Products Now Available",
        'email': "Dear {name},\n\nWe're excited to let you know that Jodala Microfinance now offers new savings and loan products designed to serve you better.\n\nVisit your nearest branch or contact us to learn more about what's available.\n\nBest regards,\nJodala Microfinance",
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
def draft_campaign_message():
    """Generate a custom campaign message with the Anthropic API, based on a
    short prompt describing what the sender wants to say. Requires
    ANTHROPIC_API_KEY to be set in the environment."""
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error': 'AI drafting is not configured -- set ANTHROPIC_API_KEY on the server'}), 400

    data = request.get_json() or {}
    prompt = (data.get('prompt') or '').strip()
    channel = data.get('channel', 'sms')
    audience_type = data.get('audience_type', 'both')

    if not prompt:
        return jsonify({'error': 'Describe what the message should say'}), 400

    audience_desc = {
        'members': 'registered members', 'clients': 'non-member client borrowers', 'both': 'members and clients'
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
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 500,
                "system": system_prompt,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        result = resp.json()
        text = "".join(block.get('text', '') for block in result.get('content', []) if block.get('type') == 'text').strip()
        if not text:
            return jsonify({'error': 'AI returned an empty draft -- try rephrasing your prompt'}), 502

        subject = None
        if channel == 'email':
            lines = text.split('\n', 1)
            if lines[0].lower().startswith('subject:'):
                subject = lines[0].split(':', 1)[1].strip()
                text = lines[1].strip() if len(lines) > 1 else text

        return jsonify({'message': text, 'subject': subject})
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'AI drafting failed: {e}'}), 502


@campaigns_bp.route('/')
@login_required
def index():
    regions = get_db().execute("SELECT name FROM regions WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template(
        'campaigns/index.html', user=get_current_user(),
        regions=[r['name'] for r in regions],
        sms_ready=sms_configured(), email_ready=email_configured(),
    )


@campaigns_bp.route('/api/preview', methods=['POST'])
@login_required
def preview_recipients():
    """Returns the count (and a short sample) of who a campaign with these
    filters would currently reach -- lets the sender confirm before
    committing to an actual send."""
    data = request.get_json() or {}
    recipients = _resolve_recipients(
        data.get('audience_type', 'both'), data.get('region') or None, bool(data.get('overdue_only'))
    )
    return jsonify({
        'count': len(recipients),
        'sample': [r['name'] for r in recipients[:8]],
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
    if audience_type not in ('members', 'clients', 'both'):
        return jsonify({'error': 'Invalid audience_type'}), 400

    if channel == 'sms' and not sms_configured():
        return jsonify({'error': 'SMS is not configured -- set it up in Settings > Notifications first'}), 400
    if channel == 'email' and not email_configured():
        return jsonify({'error': 'Email is not configured -- set it up in Settings > Notifications first'}), 400

    recipients = _resolve_recipients(audience_type, region, overdue_only)
    if not recipients:
        return jsonify({'error': 'No recipients match those filters'}), 400

    sent, failed = 0, 0
    for r in recipients:
        if channel == 'sms':
            if r['phone']:
                send_sms_async(r['phone'], message)
                sent += 1
            else:
                failed += 1
        else:
            if r['email']:
                body_html = f"<p>{message}</p>"
                send_email_async(r['email'], subject, message, body_html)
                sent += 1
            else:
                failed += 1

    cur = execute(
        """INSERT INTO campaigns (channel, audience_type, region, overdue_only, message, subject,
                                   recipient_count, sent_count, failed_count, created_by, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (channel, audience_type, region, 1 if overdue_only else 0, message, subject,
         len(recipients), sent, failed, get_current_user()['id'], utcnow())
    )
    campaign_id = cur.lastrowid
    log_audit('SEND_CAMPAIGN', 'campaign', campaign_id,
              new_values={'channel': channel, 'recipients': len(recipients), 'sent': sent, 'failed': failed})

    return jsonify({
        'message': f'Campaign queued for {sent} recipient(s)' + (f', {failed} skipped (no {channel} contact on file)' if failed else ''),
        'recipient_count': len(recipients), 'sent_count': sent, 'failed_count': failed,
    })

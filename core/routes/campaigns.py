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
            sql += f"""AND id IN (
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

from flask import Blueprint, request, jsonify, render_template

from app.database import get_db, execute, utcnow
from app.auth import login_required, role_required, get_current_user
from app.serializers import member_public
from app.utils import generate_member_number, log_audit, paginate, notify

members_bp = Blueprint('members', __name__)


@members_bp.route('/')
@login_required
def index():
    return render_template('members/index.html', user=get_current_user())


@members_bp.route('/register')
@login_required
def register_page():
    return render_template('members/register.html', user=get_current_user())


@members_bp.route('/api', methods=['GET'])
@login_required
def list_members():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '')
    status = request.args.get('status', '')
    region = request.args.get('region', '')

    where = []
    params = []
    if search:
        like = f'%{search}%'
        where.append("(first_name LIKE ? OR last_name LIKE ? OR member_number LIKE ? OR phone LIKE ? OR national_id LIKE ?)")
        params += [like, like, like, like, like]
    if status:
        where.append("status = ?")
        params.append(status)
    if region:
        where.append("region = ?")
        params.append(region)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    base_sql = f"SELECT * FROM members{where_sql} ORDER BY created_at DESC"
    count_sql = f"SELECT COUNT(*) FROM members{where_sql}"

    rows, total, pages = paginate(base_sql, count_sql, tuple(params), page, per_page)

    return jsonify({
        'members': [member_public(r) for r in rows],
        'total': total,
        'pages': pages,
        'current_page': page
    })


@members_bp.route('/api', methods=['POST'])
@login_required
def create_member():
    data = request.get_json()
    user = get_current_user()

    first_name = (data.get('first_name') or '').strip()
    last_name = (data.get('last_name') or '').strip()

    status = data.get('status', 'active')
    if status not in ['active', 'suspended', 'blacklisted', 'inactive']:
        status = 'active'

    if not first_name:
        return jsonify({'error': 'First name is required'}), 400

    phone = (data.get('phone') or '').strip()
    existing_phone = None
    if phone:
        existing_phone = get_db().execute(
            "SELECT id FROM members WHERE phone = ?", (phone,)
        ).fetchone()
    if existing_phone:
        return jsonify({'error': 'Phone number already registered'}), 400

    now = utcnow()
    cur = execute(
        """INSERT INTO members (member_number, first_name, last_name, gender, date_of_birth, phone, email,
               region, status, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (generate_member_number(),
         first_name,
         last_name,
         data.get('gender'),
         data.get('date_of_birth'),
         phone,
         data.get('email'),
         data.get('region'),
         status,
         user['id'], now, now)
    )
    member = get_db().execute("SELECT * FROM members WHERE id = ?", (cur.lastrowid,)).fetchone()
    log_audit('CREATE_MEMBER', 'member', member['id'], new_values=member_public(member))

    notify(
        user['id'],
        'New Member Registered',
        f"{first_name} {last_name} ({member['member_number']}) has been registered as a member.",
        notification_type='info', related_type='member', related_id=member['id'],
        email=member['email'],
        email_subject='Welcome to Jodala Microfinance',
        email_body_html=(
            f"<p>Dear {first_name},</p>"
            f"<p>Welcome! You have been successfully registered as a member of Jodala Microfinance.</p>"
            f"<p>Your member number is <strong>{member['member_number']}</strong>.</p>"
            f"<p>Thank you for joining us.</p>"
        )
    )

    return jsonify({'message': 'Member registered successfully', 'member': member_public(member)}), 201


@members_bp.route('/api/<int:member_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_member(member_id):
    member = get_db().execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if not member:
        return jsonify({'error': 'Member not found'}), 404

    active_loan = get_db().execute(
        "SELECT id FROM loans WHERE member_id = ? AND status = 'active'",
        (member_id,)
    ).fetchone()
    if active_loan:
        return jsonify({'error': 'Cannot delete a member with an active loan. Write off or close the loan first.'}), 400

    old_data = member_public(member)

    loan_ids = [r['id'] for r in get_db().execute(
        "SELECT id FROM loans WHERE member_id = ?", (member_id,)
    ).fetchall()]
    for loan_id in loan_ids:
        execute("DELETE FROM loan_schedules WHERE loan_id = ?", (loan_id,))
        execute("DELETE FROM repayments WHERE loan_id = ?", (loan_id,))
    if loan_ids:
        execute("DELETE FROM loans WHERE member_id = ?", (member_id,))

    account_ids = [r['id'] for r in get_db().execute(
        "SELECT id FROM savings_accounts WHERE member_id = ?", (member_id,)
    ).fetchall()]
    for account_id in account_ids:
        execute("DELETE FROM savings_transactions WHERE account_id = ?", (account_id,))
    if account_ids:
        execute("DELETE FROM savings_accounts WHERE member_id = ?", (member_id,))

    execute("DELETE FROM members WHERE id = ?", (member_id,))
    log_audit('DELETE_MEMBER', 'member', member_id, old_values=old_data)

    return jsonify({'message': 'Member deleted successfully'})


@members_bp.route('/api/<int:member_id>', methods=['GET'])
@login_required
def get_member(member_id):
    member = get_db().execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if not member:
        return jsonify({'error': 'Member not found'}), 404

    data = member_public(member)

    loans = get_db().execute(
        """SELECT loans.*, members.first_name, members.middle_name, members.last_name,
                  loan_products.name AS product_name
           FROM loans
           LEFT JOIN members ON members.id = loans.member_id
           LEFT JOIN loan_products ON loan_products.id = loans.product_id
           WHERE loans.member_id = ?""", (member_id,)
    ).fetchall()
    from app.serializers import loan_public, member_full_name
    data['loans'] = [
        loan_public({**dict(l), 'borrower_name': member_full_name(l)}) for l in loans
    ]

    savings = get_db().execute(
        """SELECT savings_accounts.*, savings_products.name AS product_name
           FROM savings_accounts
           LEFT JOIN savings_products ON savings_products.id = savings_accounts.product_id
           WHERE savings_accounts.member_id = ?""", (member_id,)
    ).fetchall()
    from app.serializers import savings_account_public
    data['savings_accounts'] = [
        savings_account_public({**dict(s), 'member_name': member_full_name(member)}) for s in savings
    ]

    return jsonify(data)


@members_bp.route('/api/<int:member_id>', methods=['PUT'])
@login_required
def update_member(member_id):
    member = get_db().execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if not member:
        return jsonify({'error': 'Member not found'}), 404
    old_data = member_public(member)
    data = request.get_json()

    fields = ['first_name', 'last_name', 'gender', 'date_of_birth', 'phone', 'email',
              'region', 'district', 'address', 'occupation', 'employer', 'next_of_kin_name',
              'next_of_kin_phone', 'next_of_kin_relation']
    values = {f: data.get(f, member[f]) for f in fields}
    values['monthly_income'] = float(data.get('monthly_income', member['monthly_income']))

    execute(
        f"""UPDATE members SET {', '.join(f'{f} = ?' for f in fields)}, monthly_income = ?, updated_at = ?
            WHERE id = ?""",
        tuple(values[f] for f in fields) + (values['monthly_income'], utcnow(), member_id)
    )

    updated = get_db().execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    log_audit('UPDATE_MEMBER', 'member', member_id, old_values=old_data, new_values=member_public(updated))

    return jsonify({'message': 'Member updated', 'member': member_public(updated)})


@members_bp.route('/api/<int:member_id>/status', methods=['PUT'])
@login_required
def update_status(member_id):
    member = get_db().execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if not member:
        return jsonify({'error': 'Member not found'}), 404
    data = request.get_json()
    status = data.get('status')

    if status not in ['active', 'suspended', 'blacklisted', 'inactive']:
        return jsonify({'error': 'Invalid status'}), 400

    old_status = member['status']
    execute("UPDATE members SET status = ?, updated_at = ? WHERE id = ?", (status, utcnow(), member_id))
    log_audit('CHANGE_MEMBER_STATUS', 'member', member_id,
              old_values={'status': old_status}, new_values={'status': status})

    return jsonify({'message': f'Member status updated to {status}'})


@members_bp.route('/<int:member_id>')
@login_required
def detail(member_id):
    member = get_db().execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    return render_template('members/detail.html', user=get_current_user(), member=member)


@members_bp.route('/<int:member_id>/edit')
@login_required
def edit_page(member_id):
    member = get_db().execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    return render_template('members/edit.html', user=get_current_user(), member=member)

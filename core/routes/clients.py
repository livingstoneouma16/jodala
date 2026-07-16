from flask import Blueprint, request, jsonify, render_template

from core.database import get_db, execute, utcnow
from core.auth import login_required, role_required, get_current_user
from core.serializers import client_public, loan_public, client_full_name
from core.utils import generate_client_number, log_audit, paginate, notify

clients_bp = Blueprint('clients', __name__)


@clients_bp.route('/')
@login_required
def index():
    return render_template('clients/index.html', user=get_current_user())


@clients_bp.route('/register')
@login_required
def register_page():
    return render_template('clients/register.html', user=get_current_user())


@clients_bp.route('/<int:client_id>/edit')
@login_required
def edit_page(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    return render_template('clients/edit.html', user=get_current_user(), client=client)


@clients_bp.route('/api', methods=['GET'])
@login_required
def list_clients():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '')
    status = request.args.get('status', '')

    where, params = [], []
    if search:
        like = f'%{search}%'
        where.append("(first_name LIKE ? OR last_name LIKE ? OR client_number LIKE ? OR phone LIKE ? OR national_id LIKE ?)")
        params += [like, like, like, like, like]
    if status:
        where.append("status = ?")
        params.append(status)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    rows, total, pages = paginate(
        f"SELECT * FROM clients{where_sql} ORDER BY created_at DESC",
        f"SELECT COUNT(*) FROM clients{where_sql}",
        tuple(params), page, per_page
    )

    return jsonify({
        'clients': [client_public(r) for r in rows],
        'total': total,
        'pages': pages,
        'current_page': page
    })


@clients_bp.route('/api', methods=['POST'])
@login_required
def create_client():
    data = request.get_json()
    user = get_current_user()
    now = utcnow()

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
            "SELECT id FROM clients WHERE phone = ?", (phone,)
        ).fetchone()
    if existing_phone:
        return jsonify({'error': 'Phone number already registered'}), 400

    cur = execute(
        """INSERT INTO clients (client_number, first_name, last_name, gender, date_of_birth,
               phone, email, region, status, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (generate_client_number(),
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
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (cur.lastrowid,)).fetchone()
    log_audit('CREATE_CLIENT', 'client', client['id'], new_values=client_public(client))

    notify(
        user['id'],
        'New Client Registered',
        f"{first_name} {last_name} ({client['client_number']}) has been registered as a client.",
        notification_type='info', related_type='client', related_id=client['id'],
        email=client['email'],
        email_subject='Welcome to Jodala Microfinance',
        email_body_html=(
            f"<p>Dear {first_name},</p>"
            f"<p>Welcome! You have been successfully registered as a client of Jodala Microfinance.</p>"
            f"<p>Your client number is <strong>{client['client_number']}</strong>.</p>"
            f"<p>Thank you for choosing us.</p>"
        )
    )

    return jsonify({'message': 'Client registered', 'client': client_public(client)}), 201


@clients_bp.route('/api/<int:client_id>', methods=['DELETE'])
@login_required
@role_required('admin')
def delete_client(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        return jsonify({'error': 'Client not found'}), 404

    active_loan = get_db().execute(
        "SELECT id FROM loans WHERE client_id = ? AND status IN ('active', 'completed')",
        (client_id,)
    ).fetchone()
    if active_loan:
        return jsonify({'error': 'Cannot delete a client with active or completed loans. Write off or close the loan first.'}), 400

    old_data = client_public(client)

    loan_ids = [r['id'] for r in get_db().execute(
        "SELECT id FROM loans WHERE client_id = ?", (client_id,)
    ).fetchall()]
    for loan_id in loan_ids:
        execute("DELETE FROM loan_schedules WHERE loan_id = ?", (loan_id,))
        execute("DELETE FROM repayments WHERE loan_id = ?", (loan_id,))
    if loan_ids:
        execute("DELETE FROM loans WHERE client_id = ?", (client_id,))

    execute("DELETE FROM clients WHERE id = ?", (client_id,))
    log_audit('DELETE_CLIENT', 'client', client_id, old_values=old_data)

    return jsonify({'message': 'Client deleted successfully'})


@clients_bp.route('/api/<int:client_id>', methods=['GET'])
@login_required
def get_client(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        return jsonify({'error': 'Client not found'}), 404

    data = client_public(client)
    loans = get_db().execute(
        """SELECT loans.*, clients.first_name, clients.last_name, loan_products.name AS product_name
           FROM loans
           LEFT JOIN clients ON clients.id = loans.client_id
           LEFT JOIN loan_products ON loan_products.id = loans.product_id
           WHERE loans.client_id = ?""", (client_id,)
    ).fetchall()
    data['loans'] = [
        loan_public({**dict(l), 'borrower_name': client_full_name(l)}) for l in loans
    ]
    return jsonify(data)


@clients_bp.route('/api/<int:client_id>', methods=['PUT'])
@login_required
def update_client(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        return jsonify({'error': 'Client not found'}), 404
    old_data = client_public(client)
    data = request.get_json()

    fields = ['first_name', 'last_name', 'gender', 'date_of_birth', 'phone', 'email',
              'region', 'district', 'address', 'occupation', 'employer', 'next_of_kin_name',
              'next_of_kin_phone', 'next_of_kin_relation']
    values = {f: data.get(f, client[f]) for f in fields}
    values['monthly_income'] = float(data.get('monthly_income', client['monthly_income']))

    execute(
        f"""UPDATE clients SET {', '.join(f'{f} = ?' for f in fields)}, monthly_income = ?, updated_at = ?
            WHERE id = ?""",
        tuple(values[f] for f in fields) + (values['monthly_income'], utcnow(), client_id)
    )

    updated = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    log_audit('UPDATE_CLIENT', 'client', client_id, old_values=old_data, new_values=client_public(updated))

    return jsonify({'message': 'Client updated', 'client': client_public(updated)})


@clients_bp.route('/api/<int:client_id>/status', methods=['PUT'])
@login_required
def update_status(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        return jsonify({'error': 'Client not found'}), 404
    data = request.get_json()
    status = data.get('status')

    if status not in ['active', 'suspended', 'blacklisted', 'inactive']:
        return jsonify({'error': 'Invalid status'}), 400

    old_status = client['status']
    execute("UPDATE clients SET status = ?, updated_at = ? WHERE id = ?", (status, utcnow(), client_id))
    log_audit('CHANGE_CLIENT_STATUS', 'client', client_id,
              old_values={'status': old_status}, new_values={'status': status})

    return jsonify({'message': f'Client status updated to {status}'})


@clients_bp.route('/<int:client_id>')
@login_required
def detail(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    return render_template('clients/detail.html', user=get_current_user(), client=client)

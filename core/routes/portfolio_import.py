"""
Bulk loan portfolio CSV import -- routes only. The actual parsing/
validation/insertion logic lives in core/portfolio_import.py; this module
is just the two-step HTTP flow around it:

    POST /portfolio-import/api/preview  -- upload a CSV, get back every row
        with any validation errors, nothing written to the DB yet.
    POST /portfolio-import/api/commit   -- given the same rows the preview
        returned (person has had a chance to review them), actually
        creates the loans. Re-validates server-side rather than trusting
        the client round-trip, since the rows could in principle be edited
        in the browser before being sent back.

Restricted to admin: this creates loans, borrower records, and ledger
entries in bulk without going through the normal application/approval
workflow, so it needs the same trust level as things like Settings >
Backups or user management, not the day-to-day loan_officer role.
"""
from flask import Blueprint, request, jsonify, render_template

from core.auth import login_required, role_required, get_current_user
from core.portfolio_import import parse_and_validate, commit_import

portfolio_import_bp = Blueprint('portfolio_import', __name__)


@portfolio_import_bp.route('/')
@login_required
@role_required('admin')
def index():
    return render_template('portfolio_import/index.html', user=get_current_user())


@portfolio_import_bp.route('/api/template', methods=['GET'])
@login_required
@role_required('admin')
def download_template():
    """A starter CSV with the expected header row and one example row, so
    people don't have to guess column names/order from documentation."""
    from flask import Response
    header = (
        "product_code,principal_amount,interest_rate,interest_type,term,repayment_frequency,"
        "disbursement_date,first_repayment_date,member_number,client_number,first_name,last_name,"
        "phone,email,region,guarantor_name,guarantor_phone,collateral,purpose,"
        "loan_officer_username,installments_paid,total_paid,notes\n"
    )
    example = (
        "PL01,50000,10,flat,12,monthly,2025-11-01,2025-12-01,,,Jane,Wanjiru,0722123456,,"
        "Nairobi,,,,,School fees,jofficer,3,15000,Migrated from old system\n"
    )
    return Response(
        header + example, mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=loan_import_template.csv'}
    )


@portfolio_import_bp.route('/api/preview', methods=['POST'])
@login_required
@role_required('admin')
def preview():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No file selected'}), 400
    if not file.filename.lower().endswith('.csv'):
        return jsonify({'error': 'File must be a .csv'}), 400

    try:
        result = parse_and_validate(file.stream)
    except UnicodeDecodeError:
        return jsonify({'error': 'Could not read the file as text -- make sure it was saved as CSV, not .xlsx'}), 400

    if result.get('error'):
        return jsonify({'error': result['error']}), 400

    return jsonify(result)


@portfolio_import_bp.route('/api/commit', methods=['POST'])
@login_required
@role_required('admin')
def commit():
    """Body: {rows: [...same shape parse_and_validate returned...],
    post_to_ledger: bool, filename: str}"""
    data = request.get_json() or {}
    rows = data.get('rows')
    if not rows:
        return jsonify({'error': 'No rows to import'}), 400

    post_to_ledger = bool(data.get('post_to_ledger', True))
    filename = data.get('filename', 'import.csv')
    user = get_current_user()

    result = commit_import(rows, post_to_ledger, user['id'], filename)
    return jsonify(result)

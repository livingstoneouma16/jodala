from flask import Blueprint, request, jsonify, render_template, send_file
from datetime import date
import io

from core.database import get_db
from core.auth import login_required, get_current_user
from core.serializers import loan_public, repayment_public, member_public, member_full_name, client_full_name

reports_bp = Blueprint('reports', __name__)


def _loan_join_sql(where_sql=''):
    return f"""SELECT loans.*,
                      COALESCE(
                          TRIM(members.first_name || ' ' || COALESCE(members.middle_name, '') || ' ' || members.last_name),
                          TRIM(clients.first_name || ' ' || clients.last_name)
                      ) AS borrower_name,
                      loan_products.name AS product_name
               FROM loans
               LEFT JOIN members ON members.id = loans.member_id
               LEFT JOIN clients ON clients.id = loans.client_id
               LEFT JOIN loan_products ON loan_products.id = loans.product_id{where_sql}"""


@reports_bp.route('/')
@login_required
def index():
    return render_template('reports/index.html', user=get_current_user())


@reports_bp.route('/api/loan-report')
@login_required
def loan_report():
    status = request.args.get('status', '')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    where, params = [], []
    if status:
        where.append("loans.status = ?"); params.append(status)
    if date_from:
        where.append("loans.application_date >= ?"); params.append(date_from)
    if date_to:
        where.append("loans.application_date <= ?"); params.append(date_to)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    loans = get_db().execute(_loan_join_sql(where_sql), tuple(params)).fetchall()

    by_status = {}
    for l in loans:
        by_status[l['status']] = by_status.get(l['status'], 0) + 1

    summary = {
        'total': len(loans),
        'total_principal': sum(l['principal_amount'] for l in loans),
        'total_disbursed': sum(l['amount_disbursed'] for l in loans),
        'total_outstanding': sum(l['outstanding_balance'] for l in loans),
        'total_collected': sum(l['total_paid'] for l in loans),
    }

    return jsonify({
        'loans': [loan_public(l) for l in loans[:100]],
        'summary': {k: round(v, 2) for k, v in summary.items()},
        'by_status': by_status
    })


@reports_bp.route('/api/arrears-report')
@login_required
def arrears_report():
    today = date.today()
    overdue = get_db().execute(
        """SELECT loan_schedules.*,
                  loans.loan_number, loans.status AS loan_status, loans.outstanding_balance,
                  loans.member_id, loans.client_id
           FROM loan_schedules
           LEFT JOIN loans ON loans.id = loan_schedules.loan_id
           WHERE loan_schedules.due_date < ? AND loan_schedules.status IN ('pending', 'partial')""",
        (today.isoformat(),)
    ).fetchall()

    result = []
    for s in overdue:
        if s['loan_status'] != 'active':
            continue
        days_overdue = (today - date.fromisoformat(s['due_date'])).days
        borrower = 'N/A'
        if s['member_id']:
            m = get_db().execute("SELECT * FROM members WHERE id = ?", (s['member_id'],)).fetchone()
            if m:
                borrower = member_full_name(m)
        elif s['client_id']:
            c = get_db().execute("SELECT * FROM clients WHERE id = ?", (s['client_id'],)).fetchone()
            if c:
                borrower = client_full_name(c)

        result.append({
            'loan_number': s['loan_number'],
            'borrower': borrower,
            'installment': s['installment_number'],
            'due_date': s['due_date'],
            'amount_due': s['total_due'] - s['total_paid'],
            'days_overdue': days_overdue,
            'outstanding_balance': s['outstanding_balance']
        })

    result.sort(key=lambda x: x['days_overdue'], reverse=True)
    total_arrears = sum(r['amount_due'] for r in result)
    return jsonify({'arrears': result, 'total_arrears': round(total_arrears, 2), 'count': len(result)})


@reports_bp.route('/api/collection-report')
@login_required
def collection_report():
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())

    repayments = get_db().execute(
        """SELECT repayments.*, loans.loan_number FROM repayments
           LEFT JOIN loans ON loans.id = repayments.loan_id
           WHERE repayments.payment_date >= ? AND repayments.payment_date <= ?
           ORDER BY repayments.payment_date DESC""",
        (date_from, date_to)
    ).fetchall()

    total = sum(r['amount'] for r in repayments)
    by_method = {}
    for r in repayments:
        by_method[r['payment_method']] = by_method.get(r['payment_method'], 0) + r['amount']

    return jsonify({
        'repayments': [repayment_public(r) for r in repayments],
        'total': round(total, 2),
        'by_method': {k: round(v, 2) for k, v in by_method.items()},
        'count': len(repayments)
    })


@reports_bp.route('/api/member-report')
@login_required
def member_report():
    members = get_db().execute("SELECT * FROM members ORDER BY created_at DESC").fetchall()
    by_status, by_region = {}, {}

    for m in members:
        by_status[m['status']] = by_status.get(m['status'], 0) + 1
        if m['region']:
            by_region[m['region']] = by_region.get(m['region'], 0) + 1

    return jsonify({
        'members': [member_public(m) for m in members[:200]],
        'total': len(members),
        'by_status': by_status,
        'by_region': by_region
    })


@reports_bp.route('/api/export/loans/excel')
@login_required
def export_loans_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    loans = get_db().execute(_loan_join_sql() + " ORDER BY loans.created_at DESC").fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "Loans Report"

    headers = ['Loan No', 'Borrower', 'Product', 'Principal', 'Interest Rate',
               'Total Repayable', 'Outstanding', 'Total Paid', 'Status',
               'Application Date', 'Disbursement Date', 'Due Date']

    header_fill = PatternFill(start_color="1B4332", end_color="1B4332", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')
        ws.column_dimensions[get_column_letter(col)].width = 15

    for row, loan in enumerate(loans, 2):
        ws.cell(row=row, column=1, value=loan['loan_number'])
        ws.cell(row=row, column=2, value=loan['borrower_name'] or 'N/A')
        ws.cell(row=row, column=3, value=loan['product_name'] or '')
        ws.cell(row=row, column=4, value=loan['principal_amount'])
        ws.cell(row=row, column=5, value=f"{loan['interest_rate']}%")
        ws.cell(row=row, column=6, value=loan['total_repayable'])
        ws.cell(row=row, column=7, value=loan['outstanding_balance'])
        ws.cell(row=row, column=8, value=loan['total_paid'])
        ws.cell(row=row, column=9, value=(loan['status'] or '').upper())
        ws.cell(row=row, column=10, value=loan['application_date'] or '')
        ws.cell(row=row, column=11, value=loan['disbursement_date'] or '')
        ws.cell(row=row, column=12, value=loan['expected_end_date'] or '')

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name='loans_report.xlsx',
                      mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@reports_bp.route('/api/export/members/excel')
@login_required
def export_members_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font

    members = get_db().execute("SELECT * FROM members ORDER BY created_at DESC").fetchall()
    wb = Workbook()
    ws = wb.active
    ws.title = "Members"

    headers = ['Member No', 'First Name', 'Last Name', 'Phone', 'Email',
               'Region', 'Occupation', 'Status', 'Joined']

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = Font(bold=True)

    for row, m in enumerate(members, 2):
        ws.cell(row=row, column=1, value=m['member_number'])
        ws.cell(row=row, column=2, value=m['first_name'])
        ws.cell(row=row, column=3, value=m['last_name'])
        ws.cell(row=row, column=4, value=m['phone'])
        ws.cell(row=row, column=5, value=m['email'])
        ws.cell(row=row, column=6, value=m['region'])
        ws.cell(row=row, column=7, value=m['occupation'])
        ws.cell(row=row, column=8, value=m['status'])
        ws.cell(row=row, column=9, value=m['created_at'])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name='members_report.xlsx',
                      mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

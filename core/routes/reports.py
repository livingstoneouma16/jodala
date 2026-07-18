from flask import Blueprint, request, jsonify, render_template, send_file, Response
from datetime import date
import csv
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
                      COALESCE(members.region, clients.region) AS region,
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
        where.append("loans.status = %s"); params.append(status)
    if date_from:
        where.append("loans.application_date >= %s"); params.append(date_from)
    if date_to:
        where.append("loans.application_date <= %s"); params.append(date_to)

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
           WHERE loan_schedules.due_date < %s AND loan_schedules.status IN ('pending', 'partial')""",
        (today.isoformat(),)
    ).fetchall()

    result = []
    for s in overdue:
        if s['loan_status'] != 'active':
            continue
        days_overdue = (today - date.fromisoformat(s['due_date'])).days
        borrower = 'N/A'
        if s['member_id']:
            m = get_db().execute("SELECT * FROM members WHERE id = %s", (s['member_id'],)).fetchone()
            if m:
                borrower = member_full_name(m)
        elif s['client_id']:
            c = get_db().execute("SELECT * FROM clients WHERE id = %s", (s['client_id'],)).fetchone()
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
           WHERE repayments.payment_date >= %s AND repayments.payment_date <= %s
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


def _compute_regional_performance():
    """Portfolio performance broken down by borrower region: reach (members),
    volume (loan counts/principal/disbursed), and health (outstanding,
    collected, arrears, PAR%, collection rate%). A loan's region comes from
    its member/client (see _loan_join_sql) -- loans themselves don't carry a
    region directly. Plain function (no @login_required) so both the JSON
    endpoint and the Excel/CSV exports can call it directly without an extra
    auth check or a jsonify->get_json round trip."""
    today = date.today()

    loans = get_db().execute(_loan_join_sql() + " ORDER BY loans.created_at DESC").fetchall()

    # Overdue installments on active loans, joined just enough to resolve a
    # region without an N+1 query per row (member_regions/client_regions are
    # prefetched once below instead of queried per overdue row).
    overdue = get_db().execute(
        """SELECT loan_schedules.loan_id, loan_schedules.total_due, loan_schedules.total_paid,
                  loans.status AS loan_status, loans.member_id, loans.client_id
           FROM loan_schedules
           LEFT JOIN loans ON loans.id = loan_schedules.loan_id
           WHERE loan_schedules.due_date < %s AND loan_schedules.status IN ('pending', 'partial')""",
        (today.isoformat(),)
    ).fetchall()

    member_regions = {m['id']: m['region'] for m in get_db().execute("SELECT id, region FROM members").fetchall()}
    client_regions = {c['id']: c['region'] for c in get_db().execute("SELECT id, region FROM clients").fetchall()}

    regions = {}

    def bucket(name):
        key = name or 'Unassigned'
        return regions.setdefault(key, {
            'region': key, 'member_count': 0, 'loan_count': 0, 'active_loan_count': 0,
            'total_principal': 0.0, 'total_disbursed': 0.0, 'total_outstanding': 0.0,
            'total_collected': 0.0, 'par_outstanding': 0.0, 'arrears_amount': 0.0,
        })

    # Loans with at least one overdue installment -- used below to attribute
    # outstanding balance to "at risk" (PAR) per region.
    overdue_loan_ids = set()
    for s in overdue:
        if s['loan_status'] != 'active':
            continue
        overdue_loan_ids.add(s['loan_id'])
        region = member_regions.get(s['member_id']) if s['member_id'] else client_regions.get(s['client_id'])
        bucket(region)['arrears_amount'] += (s['total_due'] - s['total_paid'])

    for l in loans:
        b = bucket(l['region'])
        b['loan_count'] += 1
        b['total_principal'] += l['principal_amount'] or 0
        b['total_disbursed'] += l['amount_disbursed'] or 0
        b['total_outstanding'] += l['outstanding_balance'] or 0
        b['total_collected'] += l['total_paid'] or 0
        if l['status'] == 'active':
            b['active_loan_count'] += 1
            if l['id'] in overdue_loan_ids:
                b['par_outstanding'] += l['outstanding_balance'] or 0

    for region in member_regions.values():
        bucket(region)['member_count'] += 1

    result = []
    for data in regions.values():
        outstanding = data['total_outstanding']
        # Collection rate uses collected / (collected + outstanding) rather
        # than collected / total_repayable, since total_repayable includes
        # interest on loans not yet fully scheduled/disbursed and would
        # understate the rate for a young, fast-growing region.
        collectible_base = data['total_collected'] + outstanding
        data['par_pct'] = round(data['par_outstanding'] / outstanding * 100, 2) if outstanding else 0.0
        data['collection_rate_pct'] = round(data['total_collected'] / collectible_base * 100, 2) if collectible_base else 0.0
        for key in ('total_principal', 'total_disbursed', 'total_outstanding', 'total_collected', 'arrears_amount'):
            data[key] = round(data[key], 2)
        del data['par_outstanding']
        result.append(data)

    result.sort(key=lambda r: r['total_outstanding'], reverse=True)

    totals = {
        'region_count': len(result),
        'member_count': sum(r['member_count'] for r in result),
        'loan_count': sum(r['loan_count'] for r in result),
        'total_outstanding': round(sum(r['total_outstanding'] for r in result), 2),
        'total_collected': round(sum(r['total_collected'] for r in result), 2),
        'arrears_amount': round(sum(r['arrears_amount'] for r in result), 2),
    }

    return {'regions': result, 'totals': totals}


@reports_bp.route('/api/regional-performance')
@login_required
def regional_performance():
    return jsonify(_compute_regional_performance())


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


_REGIONAL_HEADERS = ['Region', 'Members', 'Loans', 'Active Loans', 'Total Principal',
                     'Total Disbursed', 'Outstanding', 'Total Collected', 'Arrears',
                     'PAR %', 'Collection Rate %']


def _regional_rows(regions):
    return [
        [
            r['region'], r['member_count'], r['loan_count'], r['active_loan_count'],
            r['total_principal'], r['total_disbursed'], r['total_outstanding'],
            r['total_collected'], r['arrears_amount'], r['par_pct'], r['collection_rate_pct'],
        ]
        for r in regions
    ]


@reports_bp.route('/api/export/regional/excel')
@login_required
def export_regional_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    data = _compute_regional_performance()

    wb = Workbook()
    ws = wb.active
    ws.title = "Regional Performance"

    header_fill = PatternFill(start_color="1B4332", end_color="1B4332", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")

    for col, header in enumerate(_REGIONAL_HEADERS, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')
        ws.column_dimensions[get_column_letter(col)].width = 16

    for row, values in enumerate(_regional_rows(data['regions']), 2):
        for col, value in enumerate(values, 1):
            ws.cell(row=row, column=col, value=value)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(output, as_attachment=True, download_name='regional_performance.xlsx',
                      mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def _csv_response(headers, rows, download_name):
    """Build a CSV download from a header row + list of value tuples/lists.
    Uses Python's stdlib csv module (no new dependency) with \\r\\n line
    endings and full quoting, matching what Excel/Sheets expect on import --
    a plain '\\n'.join(','.join(...)) would break on any field containing a
    comma, quote, or newline (e.g. a member's notes field)."""
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    csv_bytes = output.getvalue().encode('utf-8-sig')  # BOM so Excel on Windows detects UTF-8 correctly
    return Response(
        csv_bytes,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={download_name}'}
    )


@reports_bp.route('/api/export/loans/csv')
@login_required
def export_loans_csv():
    loans = get_db().execute(_loan_join_sql() + " ORDER BY loans.created_at DESC").fetchall()

    headers = ['Loan No', 'Borrower', 'Product', 'Principal', 'Interest Rate',
               'Total Repayable', 'Outstanding', 'Total Paid', 'Status',
               'Application Date', 'Disbursement Date', 'Due Date']

    rows = [
        [
            loan['loan_number'],
            loan['borrower_name'] or 'N/A',
            loan['product_name'] or '',
            loan['principal_amount'],
            f"{loan['interest_rate']}%",
            loan['total_repayable'],
            loan['outstanding_balance'],
            loan['total_paid'],
            (loan['status'] or '').upper(),
            loan['application_date'] or '',
            loan['disbursement_date'] or '',
            loan['expected_end_date'] or '',
        ]
        for loan in loans
    ]

    return _csv_response(headers, rows, 'loans_report.csv')


@reports_bp.route('/api/export/members/csv')
@login_required
def export_members_csv():
    members = get_db().execute("SELECT * FROM members ORDER BY created_at DESC").fetchall()

    headers = ['Member No', 'First Name', 'Last Name', 'Phone', 'Email',
               'Region', 'Occupation', 'Status', 'Joined']

    rows = [
        [
            m['member_number'], m['first_name'], m['last_name'], m['phone'],
            m['email'], m['region'], m['occupation'], m['status'], m['created_at'],
        ]
        for m in members
    ]

    return _csv_response(headers, rows, 'members_report.csv')


@reports_bp.route('/api/export/collections/csv')
@login_required
def export_collections_csv():
    """CSV sibling of /api/collection-report -- same date_from/date_to filters
    and query, so the download always matches what's on screen."""
    date_from = request.args.get('date_from', date.today().replace(day=1).isoformat())
    date_to = request.args.get('date_to', date.today().isoformat())

    repayments = get_db().execute(
        """SELECT repayments.*, loans.loan_number FROM repayments
           LEFT JOIN loans ON loans.id = repayments.loan_id
           WHERE repayments.payment_date >= %s AND repayments.payment_date <= %s
           ORDER BY repayments.payment_date DESC""",
        (date_from, date_to)
    ).fetchall()

    headers = ['Loan No', 'Amount', 'Payment Method', 'Payment Date', 'Reference']

    rows = [
        [
            r['loan_number'], r['amount'], r['payment_method'],
            r['payment_date'], r['reference_number'] or '',
        ]
        for r in repayments
    ]

    return _csv_response(headers, rows, 'collections_report.csv')


@reports_bp.route('/api/export/regional/csv')
@login_required
def export_regional_csv():
    data = _compute_regional_performance()
    return _csv_response(_REGIONAL_HEADERS, _regional_rows(data['regions']), 'regional_performance.csv')

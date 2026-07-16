"""
serializers.py
--------------
Row (dict) -> API-shaped dict conversions, mirroring what the old
SQLAlchemy model.to_dict() methods produced. Routes SELECT the columns
they need (joining in related names like member_name/product_name
directly in SQL) and pass the resulting row dict through these.
"""


def member_full_name(row):
    d = dict(row)
    parts = [d.get('first_name', ''), d.get('middle_name') or '', d.get('last_name', '')]
    return ' '.join(p for p in parts if p).strip()


def user_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'username': row['username'],
        'email': row['email'],
        'full_name': row['full_name'],
        'role': row['role'],
        'phone': row['phone'],
        'is_active': bool(row['is_active']),
        'totp_enabled': bool(row['totp_enabled']),
        'last_login': row['last_login'],
        'created_at': row['created_at'],
    }


def member_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'member_number': row['member_number'],
        'full_name': member_full_name(row),
        'first_name': row['first_name'],
        'last_name': row['last_name'],
        'gender': row['gender'],
        'date_of_birth': row['date_of_birth'],
        'phone': row['phone'],
        'email': row['email'],
        'region': row['region'],
        'district': row['district'],
        'occupation': row['occupation'],
        'status': row['status'],
        'member_type': row['member_type'],
        'created_at': row['created_at'],
    }


def client_full_name(row):
    d = dict(row)
    parts = [d.get('first_name', ''), d.get('last_name', '')]
    return ' '.join(p for p in parts if p).strip()


def client_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'client_number': row['client_number'],
        'full_name': client_full_name(row),
        'first_name': row['first_name'],
        'last_name': row['last_name'],
        'gender': row['gender'],
        'date_of_birth': row['date_of_birth'],
        'national_id': row['national_id'],
        'phone': row['phone'],
        'email': row['email'],
        'region': row['region'],
        'district': row['district'],
        'occupation': row['occupation'],
        'status': row['status'],
        'created_at': row['created_at'],
    }


def loan_product_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'name': row['name'],
        'code': row['code'],
        'description': row['description'],
        'min_amount': row['min_amount'],
        'max_amount': row['max_amount'],
        'interest_rate': row['interest_rate'],
        'interest_type': row['interest_type'],
        'repayment_frequency': row['repayment_frequency'],
        'min_term': row['min_term'],
        'max_term': row['max_term'],
        'penalty_rate': row['penalty_rate'],
        'processing_fee': row['processing_fee'],
        'insurance_fee': row['insurance_fee'],
        'is_active': bool(row['is_active']),
    }


def loan_public(row):
    """Expects the row to include a joined `borrower_name` and `product_name`
    column (see routes/loans.py for the SELECT)."""
    if row is None:
        return None
    d = dict(row)
    return {
        'id': d['id'],
        'loan_number': d['loan_number'],
        'member_name': d.get('borrower_name', 'N/A'),
        'product_name': d.get('product_name', ''),
        'principal_amount': d['principal_amount'],
        'interest_rate': d['interest_rate'],
        'term': d['term'],
        'total_repayable': d['total_repayable'],
        'outstanding_balance': d['outstanding_balance'],
        'total_paid': d['total_paid'],
        'status': d['status'],
        'application_date': d.get('application_date'),
        'disbursement_date': d.get('disbursement_date'),
        'disbursement_method': d.get('disbursement_method', 'cash'),
        'expected_end_date': d.get('expected_end_date'),
    }


def loan_schedule_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'installment_number': row['installment_number'],
        'due_date': row['due_date'],
        'principal_due': row['principal_due'],
        'interest_due': row['interest_due'],
        'total_due': row['total_due'],
        'total_paid': row['total_paid'],
        'balance_after': row['balance_after'],
        'status': row['status'],
        'paid_date': row['paid_date'],
    }


def repayment_public(row):
    """Expects an optional joined `loan_number` column."""
    if row is None:
        return None
    d = dict(row)
    return {
        'id': d['id'],
        'receipt_number': d['receipt_number'],
        'loan_number': d.get('loan_number', ''),
        'amount': d['amount'],
        'principal_portion': d['principal_portion'],
        'interest_portion': d['interest_portion'],
        'penalty_portion': d['penalty_portion'],
        'payment_method': d['payment_method'],
        'payment_date': d['payment_date'],
        'created_at': d['created_at'],
    }


def savings_account_public(row):
    """Expects optional joined `member_name`/`product_name` columns."""
    if row is None:
        return None
    d = dict(row)
    return {
        'id': d['id'],
        'account_number': d['account_number'],
        'member_name': d.get('member_name', ''),
        'product_name': d.get('product_name', ''),
        'balance': d['balance'],
        'status': d['status'],
        'opened_at': d['opened_at'],
    }


def savings_transaction_public(row):
    """Expects an optional joined `account_number` column."""
    if row is None:
        return None
    d = dict(row)
    return {
        'id': d['id'],
        'transaction_number': d['transaction_number'],
        'account_number': d.get('account_number', ''),
        'transaction_type': d['transaction_type'],
        'amount': d['amount'],
        'balance_after': d['balance_after'],
        'payment_method': d['payment_method'],
        'transaction_date': d['transaction_date'],
    }


def mpesa_transaction_public(row):
    if row is None:
        return None
    d = dict(row)
    return {
        'id': d['id'],
        'checkout_request_id': d['checkout_request_id'],
        'originator_conversation_id': d.get('originator_conversation_id'),
        'conversation_id': d.get('conversation_id'),
        'transaction_id': d.get('transaction_id'),
        'purpose': d['purpose'],
        'target_id': d['target_id'],
        'phone': d['phone'],
        'amount': d['amount'],
        'status': d['status'],
        'result_desc': d.get('result_desc'),
        'mpesa_receipt_number': d.get('mpesa_receipt_number'),
        'repayment_id': d.get('repayment_id'),
        'savings_transaction_id': d.get('savings_transaction_id'),
        'created_at': d['created_at'],
        'updated_at': d['updated_at'],
    }


def account_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'code': row['code'],
        'name': row['name'],
        'account_type': row['account_type'],
        'balance': row['balance'],
        'is_active': bool(row['is_active']),
    }


def journal_entry_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'entry_number': row['entry_number'],
        'description': row['description'],
        'entry_date': row['entry_date'],
        'reference': row['reference'],
        'created_at': row['created_at'],
    }


def income_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'reference': row['reference'],
        'description': row['description'],
        'category': row['category'],
        'amount': row['amount'],
        'income_date': row['income_date'],
        'payment_method': row['payment_method'],
    }


def expense_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'reference': row['reference'],
        'description': row['description'],
        'category': row['category'],
        'amount': row['amount'],
        'expense_date': row['expense_date'],
        'vendor': row['vendor'],
    }


def notification_public(row):
    if row is None:
        return None
    return {
        'id': row['id'],
        'title': row['title'],
        'message': row['message'],
        'notification_type': row['notification_type'],
        'is_read': bool(row['is_read']),
        'created_at': row['created_at'],
    }


def audit_log_public(row):
    """Expects an optional joined `user_name` column."""
    if row is None:
        return None
    d = dict(row)
    return {
        'id': d['id'],
        'user_name': d.get('user_name') or 'System',
        'action': d['action'],
        'resource_type': d['resource_type'],
        'resource_id': d['resource_id'],
        'ip_address': d['ip_address'],
        'created_at': d['created_at'],
    }

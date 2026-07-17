"""
M-Pesa STK Push routes.

Two touchpoints in the UI trigger a push:
  - Loan repayments (record.html): purpose='loan_repayment', target_id=loan_id
  - Savings deposits (savings/index.html): purpose='savings_deposit', target_id=savings_account_id

Flow:
  1. Frontend POSTs /mpesa/api/stkpush with purpose/target_id/phone/amount.
     We validate the target, ask Safaricom to push a prompt to the phone,
     and store a 'pending' row in mpesa_transactions keyed by
     CheckoutRequestID.
  2. The frontend polls GET /mpesa/api/status/<checkout_request_id> every
     few seconds waiting for the row to leave 'pending'.
  3. Safaricom calls POST /mpesa/callback (no login -- this is Safaricom's
     server, not a browser) once the customer enters their PIN or cancels.
     On success we apply the payment using exactly the same internal logic
     as a manually-recorded repayment/deposit, so accounting stays
     consistent regardless of how the money came in.
"""
from flask import Blueprint, request, jsonify, url_for, current_app

from core.database import get_db, execute, utcnow
from core.auth import login_required, get_current_user
from core.serializers import mpesa_transaction_public
from core.utils import log_audit
from core.mpesa import initiate_stk_push, initiate_b2c_payment, MpesaError, get_mpesa_config, normalize_phone
from core import limiter

mpesa_bp = Blueprint('mpesa', __name__)


def _b2c_result_url():
    from core.mpesa import _setting
    override = _setting('mpesa_b2c_result_url')
    if override:
        return override
    return url_for('mpesa.b2c_result', _external=True)


def _b2c_timeout_url():
    from core.mpesa import _setting
    override = _setting('mpesa_b2c_timeout_url')
    if override:
        return override
    return url_for('mpesa.b2c_timeout', _external=True)


def _callback_url():
    # An explicit override is required in most real setups since Safaricom
    # must be able to reach this URL over the public internet with a valid
    # HTTPS certificate -- url_for(..., _external=True) alone only works if
    # this app is itself already being served from that public HTTPS host
    # (e.g. in production, or via a tunnel like ngrok pointed at localhost
    # during sandbox testing). Set mpesa_callback_url in Settings > M-Pesa
    # to override.
    from core.mpesa import _setting
    override = _setting('mpesa_callback_url')
    if override:
        return override
    return url_for('mpesa.callback', _external=True)


@mpesa_bp.route('/api/stkpush', methods=['POST'])
@login_required
@limiter.limit('10 per minute')
def stk_push():
    data = request.get_json() or {}
    user = get_current_user()

    purpose = data.get('purpose')
    target_id = data.get('target_id')
    phone = data.get('phone')
    amount = data.get('amount')

    if purpose not in ('loan_repayment', 'savings_deposit'):
        return jsonify({'error': 'Invalid purpose'}), 400
    if not target_id or not phone or not amount:
        return jsonify({'error': 'target_id, phone, and amount are required'}), 400

    db = get_db()
    if purpose == 'loan_repayment':
        target = db.execute("SELECT * FROM loans WHERE id = %s", (target_id,)).fetchone()
        if not target:
            return jsonify({'error': 'Loan not found'}), 404
        if target['status'] not in ('active', 'disbursed'):
            return jsonify({'error': 'Loan is not active'}), 400
        account_reference = target['loan_number']
        transaction_desc = 'Loan Repay'
    else:
        target = db.execute("SELECT * FROM savings_accounts WHERE id = %s", (target_id,)).fetchone()
        if not target:
            return jsonify({'error': 'Savings account not found'}), 404
        if target['status'] != 'active':
            return jsonify({'error': 'Account is not active'}), 400
        account_reference = target['account_number']
        transaction_desc = 'Savings Dep'

    try:
        phone_normalized = normalize_phone(phone)
        result = initiate_stk_push(
            phone=phone_normalized,
            amount=amount,
            account_reference=account_reference,
            transaction_desc=transaction_desc,
            callback_url=_callback_url(),
        )
    except MpesaError as e:
        return jsonify({'error': str(e)}), 502

    checkout_request_id = result.get('CheckoutRequestID')
    merchant_request_id = result.get('MerchantRequestID')
    now = utcnow()
    execute(
        """INSERT INTO mpesa_transactions (checkout_request_id, merchant_request_id, purpose, target_id,
               phone, amount, status, initiated_by, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)""",
        (checkout_request_id, merchant_request_id, purpose, int(target_id), phone_normalized,
         float(amount), user['id'], now, now)
    )
    log_audit('MPESA_STK_PUSH_INITIATED', purpose, int(target_id))

    return jsonify({
        'message': 'STK push sent -- ask the customer to check their phone and enter their M-Pesa PIN',
        'checkout_request_id': checkout_request_id,
    }), 201


@mpesa_bp.route('/api/status/<checkout_request_id>', methods=['GET'])
@login_required
def stk_status(checkout_request_id):
    txn = get_db().execute(
        "SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s", (checkout_request_id,)
    ).fetchone()
    if not txn:
        return jsonify({'error': 'Unknown checkout request'}), 404
    return jsonify(mpesa_transaction_public(txn))


@mpesa_bp.route('/callback', methods=['POST'])
def callback():
    """Safaricom posts the result here once the customer responds to the
    STK prompt (or it times out). No @login_required -- there is no user
    session on Safaricom's side. We only trust a callback that references a
    CheckoutRequestID we ourselves generated and are still waiting on;
    anything else is ignored. In production, additionally restrict inbound
    traffic to this path to Safaricom's published IP ranges at the
    network/firewall level (their IPs are documented on Daraja and change
    occasionally, so that allow-list is best kept outside application code).
    Always acknowledge with HTTP 200 so Safaricom doesn't retry forever,
    even if something below fails -- we log failures instead."""
    body = request.get_json(silent=True) or {}
    try:
        stk = body['Body']['stkCallback']
        checkout_request_id = stk['CheckoutRequestID']
        result_code = stk.get('ResultCode')
        result_desc = stk.get('ResultDesc', '')
    except (KeyError, TypeError):
        current_app.logger.warning('M-Pesa callback: malformed payload: %r', body)
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

    txn = get_db().execute(
        "SELECT * FROM mpesa_transactions WHERE checkout_request_id = %s", (checkout_request_id,)
    ).fetchone()
    if not txn:
        current_app.logger.warning('M-Pesa callback for unknown CheckoutRequestID: %s', checkout_request_id)
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200
    if txn['status'] != 'pending':
        # Already processed (Safaricom sometimes sends the callback more
        # than once) -- acknowledge without reprocessing.
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

    now = utcnow()
    if result_code != 0:
        execute(
            "UPDATE mpesa_transactions SET status = 'failed', result_code = %s, result_desc = %s, updated_at = %s "
            "WHERE id = %s",
            (result_code, result_desc, now, txn['id'])
        )
        return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

    items = {}
    try:
        for item in stk['CallbackMetadata']['Item']:
            items[item['Name']] = item.get('Value')
    except (KeyError, TypeError):
        pass

    mpesa_receipt = items.get('MpesaReceiptNumber')
    confirmed_amount = items.get('Amount', txn['amount'])

    try:
        if txn['purpose'] == 'loan_repayment':
            from core.routes.repayments import _record_repayment, _RepaymentError
            try:
                repayment, _ = _record_repayment(
                    loan_id=txn['target_id'], amount=confirmed_amount, payment_method='mobile_money',
                    reference_number=mpesa_receipt, notes='Paid via M-Pesa STK Push',
                    user_id=txn['initiated_by'],
                )
                execute(
                    "UPDATE mpesa_transactions SET status = 'success', result_code = %s, result_desc = %s, "
                    "mpesa_receipt_number = %s, repayment_id = %s, updated_at = %s WHERE id = %s",
                    (result_code, result_desc, mpesa_receipt, repayment['id'], now, txn['id'])
                )
            except _RepaymentError as e:
                current_app.logger.error('M-Pesa payment succeeded but could not be applied to loan %s: %s',
                                          txn['target_id'], e)
                execute(
                    "UPDATE mpesa_transactions SET status = 'success', result_code = %s, result_desc = %s, "
                    "mpesa_receipt_number = %s, updated_at = %s WHERE id = %s",
                    (result_code, f'Paid but not applied: {e}', mpesa_receipt, now, txn['id'])
                )
        else:
            from core.routes.savings import _create_transaction
            savings_txn, _ = _create_transaction(
                account_id=txn['target_id'], txn_type='deposit', amount=confirmed_amount,
                method='mobile_money', user_id=txn['initiated_by'], reference=mpesa_receipt,
                notes='Paid via M-Pesa STK Push',
            )
            execute(
                "UPDATE mpesa_transactions SET status = 'success', result_code = %s, result_desc = %s, "
                "mpesa_receipt_number = %s, savings_transaction_id = %s, updated_at = %s WHERE id = %s",
                (result_code, result_desc, mpesa_receipt, savings_txn['id'], now, txn['id'])
            )
    except Exception:
        current_app.logger.exception('M-Pesa callback: failed to apply payment for checkout_request_id=%s',
                                      checkout_request_id)
        execute(
            "UPDATE mpesa_transactions SET status = 'success', result_code = %s, result_desc = %s, "
            "mpesa_receipt_number = %s, updated_at = %s WHERE id = %s",
            (result_code, 'Paid but failed to apply -- needs manual reconciliation', mpesa_receipt, now, txn['id'])
        )

    log_audit('MPESA_PAYMENT_RECEIVED', txn['purpose'], txn['target_id'])
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200


@mpesa_bp.route('/api/b2c', methods=['POST'])
@login_required
@limiter.limit('10 per minute')
def b2c_disburse():
    """Kick off an M-Pesa B2C disbursement for an approved loan. The loan is
    only actually marked disbursed once Safaricom confirms success via
    /mpesa/b2c/result -- if the customer's number is invalid or the payment
    fails, the loan stays 'approved' so staff can retry or fall back to
    cash/bank disbursement."""
    data = request.get_json() or {}
    user = get_current_user()

    loan_id = data.get('loan_id')
    phone = data.get('phone')
    if not loan_id or not phone:
        return jsonify({'error': 'loan_id and phone are required'}), 400

    loan = get_db().execute("SELECT * FROM loans WHERE id = %s", (loan_id,)).fetchone()
    if not loan:
        return jsonify({'error': 'Loan not found'}), 404
    if loan['status'] != 'approved':
        return jsonify({'error': 'Loan must be approved (and not yet disbursed) to disburse via M-Pesa'}), 400

    amount = (loan['principal_amount'] - loan['rollover_amount']
              - loan['insurance_fee'])

    try:
        phone_normalized = normalize_phone(phone)
        result = initiate_b2c_payment(
            phone=phone_normalized, amount=amount, remarks=f"Loan {loan['loan_number']}",
            occasion='Loan disbursement', result_url=_b2c_result_url(), timeout_url=_b2c_timeout_url(),
        )
    except MpesaError as e:
        return jsonify({'error': str(e)}), 502

    originator_conversation_id = result.get('OriginatorConversationID')
    conversation_id = result.get('ConversationID')
    now = utcnow()
    execute(
        """INSERT INTO mpesa_transactions (originator_conversation_id, conversation_id, purpose, target_id,
               phone, amount, status, initiated_by, created_at, updated_at)
           VALUES (%s, %s, 'loan_disbursement', %s, %s, %s, 'pending', %s, %s, %s)""",
        (originator_conversation_id, conversation_id, int(loan_id), phone_normalized,
         float(amount), user['id'], now, now)
    )
    log_audit('MPESA_B2C_INITIATED', 'loan', int(loan_id))

    return jsonify({
        'message': 'Disbursement sent to Safaricom -- funds should reach the customer within a few seconds',
        'originator_conversation_id': originator_conversation_id,
    }), 201


@mpesa_bp.route('/api/b2c/status/<originator_conversation_id>', methods=['GET'])
@login_required
def b2c_status(originator_conversation_id):
    txn = get_db().execute(
        "SELECT * FROM mpesa_transactions WHERE originator_conversation_id = %s", (originator_conversation_id,)
    ).fetchone()
    if not txn:
        return jsonify({'error': 'Unknown disbursement request'}), 404
    return jsonify(mpesa_transaction_public(txn))


def _b2c_result_items(result):
    items = {}
    try:
        for p in result['ResultParameters']['ResultParameter']:
            items[p['Key']] = p.get('Value')
    except (KeyError, TypeError):
        pass
    return items


def _handle_b2c_result(body):
    """Shared handler for both the ResultURL (normal completion/failure) and
    QueueTimeOutURL (Safaricom gave up queuing it) callbacks -- both send
    the same `Result` envelope shape, so one function covers both."""
    try:
        result = body['Result']
        originator_conversation_id = result['OriginatorConversationID']
        result_code = result.get('ResultCode')
        result_desc = result.get('ResultDesc', '')
    except (KeyError, TypeError):
        current_app.logger.warning('M-Pesa B2C callback: malformed payload: %r', body)
        return

    txn = get_db().execute(
        "SELECT * FROM mpesa_transactions WHERE originator_conversation_id = %s",
        (originator_conversation_id,)
    ).fetchone()
    if not txn:
        current_app.logger.warning('M-Pesa B2C callback for unknown OriginatorConversationID: %s',
                                    originator_conversation_id)
        return
    if txn['status'] != 'pending':
        return  # already processed -- Safaricom retries occasionally

    now = utcnow()
    if result_code != 0:
        execute(
            "UPDATE mpesa_transactions SET status = 'failed', result_code = %s, result_desc = %s, updated_at = %s "
            "WHERE id = %s",
            (result_code, result_desc, now, txn['id'])
        )
        return

    items = _b2c_result_items(result)
    transaction_id = result.get('TransactionID') or items.get('TransactionReceipt')

    try:
        from core.routes.loans import _disburse_loan, _DisbursementError
        try:
            updated_loan = _disburse_loan(
                loan_id=txn['target_id'], user_id=txn['initiated_by'], disbursement_method='mpesa',
                mpesa_receipt=transaction_id,
            )
            execute(
                "UPDATE mpesa_transactions SET status = 'success', result_code = %s, result_desc = %s, "
                "mpesa_receipt_number = %s, transaction_id = %s, updated_at = %s WHERE id = %s",
                (result_code, result_desc, transaction_id, transaction_id, now, txn['id'])
            )
        except _DisbursementError as e:
            current_app.logger.error('M-Pesa B2C payment succeeded but loan %s could not be disbursed: %s',
                                      txn['target_id'], e)
            execute(
                "UPDATE mpesa_transactions SET status = 'success', result_code = %s, result_desc = %s, "
                "mpesa_receipt_number = %s, transaction_id = %s, updated_at = %s WHERE id = %s",
                (result_code, f'Paid out but not applied: {e}', transaction_id, transaction_id, now, txn['id'])
            )
    except Exception:
        current_app.logger.exception('M-Pesa B2C callback: failed to apply disbursement for '
                                      'originator_conversation_id=%s', originator_conversation_id)
        execute(
            "UPDATE mpesa_transactions SET status = 'success', result_code = %s, result_desc = %s, "
            "mpesa_receipt_number = %s, transaction_id = %s, updated_at = %s WHERE id = %s",
            (result_code, 'Paid out but failed to apply -- needs manual reconciliation',
             transaction_id, transaction_id, now, txn['id'])
        )

    log_audit('MPESA_B2C_PAYMENT_RECEIVED', 'loan', txn['target_id'])


@mpesa_bp.route('/b2c/result', methods=['POST'])
def b2c_result():
    """Safaricom's ResultURL for B2C -- called once the disbursement
    completes or is confirmed to have failed. No @login_required, same
    reasoning as /mpesa/callback. Always acknowledge 200."""
    body = request.get_json(silent=True) or {}
    _handle_b2c_result(body)
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200


@mpesa_bp.route('/b2c/timeout', methods=['POST'])
def b2c_timeout():
    """Safaricom's QueueTimeOutURL for B2C -- called if the request timed
    out in Safaricom's queue before completing. Same envelope shape as the
    result callback, so it's handled identically (will normally carry a
    non-zero ResultCode, marking the transaction failed so staff can retry)."""
    body = request.get_json(silent=True) or {}
    _handle_b2c_result(body)
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'}), 200

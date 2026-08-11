"""Client deletion safeguards."""
from conftest import auth_header


def test_client_with_loan_is_not_deleted(client, admin_token, db_conn, loan_product):
    borrower = client.post('/clients/api', json={
        'first_name': 'Loan', 'last_name': 'History', 'phone': '0712345678',
    }, headers=auth_header(admin_token)).get_json()['client']
    created = client.post('/loans/api', json={
        'client_id': borrower['id'], 'borrower_type': 'client',
        'product_id': loan_product['id'], 'principal_amount': 10000, 'term': 6,
    }, headers=auth_header(admin_token))
    assert created.status_code == 201

    response = client.delete(f"/clients/api/{borrower['id']}", headers=auth_header(admin_token))
    assert response.status_code == 400
    assert 'loan' in response.get_json()['error'].lower()
    assert db_conn.execute('SELECT id FROM clients WHERE id = %s', (borrower['id'],)).fetchone()


def test_client_without_history_can_be_deleted(client, admin_token):
    borrower = client.post('/clients/api', json={
        'first_name': 'No', 'last_name': 'History', 'phone': '0712345679',
    }, headers=auth_header(admin_token)).get_json()['client']

    response = client.delete(f"/clients/api/{borrower['id']}", headers=auth_header(admin_token))
    assert response.status_code == 200

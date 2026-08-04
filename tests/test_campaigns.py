"""Campaign preview and delivery-queue safety tests."""
from conftest import auth_header

import core.routes.campaigns as campaign_routes


def _recipients(phone='0712345678', email='jane@example.com'):
    return [
        {
            'id': 1,
            'type': 'members',
            'name': 'Jane Wanjiru',
            'phone': phone,
            'email': email,
        },
    ]


def _preview(client, token, channel='sms'):
    response = client.post('/campaigns/api/preview', json={
        'channel': channel,
        'audience_type': 'both',
        'region': None,
        'overdue_only': False,
    }, headers=auth_header(token))
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


class TestCampaignSafety:
    def test_preview_deduplicates_contacts_and_send_queues_once(self, client, admin_token, db_conn, monkeypatch):
        recipients = _recipients() + [
            {
                'id': 2,
                'type': 'clients',
                'name': 'Jane Client',
                'phone': '+254712345678',
                'email': None,
            },
            {
                'id': 3,
                'type': 'members',
                'name': 'No Phone',
                'phone': None,
                'email': None,
            },
        ]
        monkeypatch.setattr(campaign_routes, '_resolve_recipients', lambda *_: recipients)
        monkeypatch.setattr(campaign_routes, 'sms_configured', lambda: True)

        preview = _preview(client, admin_token)

        assert preview['matched_count'] == 3
        assert preview['deliverable_count'] == 1
        assert preview['missing_contact_count'] == 1
        assert preview['duplicate_contact_count'] == 1

        response = client.post('/campaigns/api', json={
            'channel': 'sms',
            'audience_type': 'both',
            'message': 'Hello {name}',
            'preview_token': preview['preview_token'],
        }, headers=auth_header(admin_token))

        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.get_json()['queued_count'] == 1
        assert response.get_json()['skipped_count'] == 2
        assert len(client.sent_sms) == 1
        assert client.sent_sms[0]['to'] == '254712345678'

        campaign = db_conn.execute(
            'SELECT recipient_count, sent_count, failed_count FROM campaigns ORDER BY id DESC LIMIT 1'
        ).fetchone()
        assert campaign['recipient_count'] == 3
        assert campaign['sent_count'] == 1
        assert campaign['failed_count'] == 2

    def test_send_rejects_a_preview_when_recipients_change(self, client, admin_token, monkeypatch):
        current_recipients = _recipients()
        monkeypatch.setattr(campaign_routes, '_resolve_recipients', lambda *_: current_recipients)
        monkeypatch.setattr(campaign_routes, 'sms_configured', lambda: True)

        preview = _preview(client, admin_token)
        current_recipients[:] = _recipients(phone='0798765432')

        response = client.post('/campaigns/api', json={
            'channel': 'sms',
            'audience_type': 'both',
            'message': 'Hello {name}',
            'preview_token': preview['preview_token'],
        }, headers=auth_header(admin_token))

        assert response.status_code == 409
        assert 'recipient list changed' in response.get_json()['error'].lower()
        assert client.sent_sms == []

    def test_email_campaign_escapes_html_message_content(self, client, admin_token, monkeypatch):
        monkeypatch.setattr(campaign_routes, '_resolve_recipients', lambda *_: _recipients())
        monkeypatch.setattr(campaign_routes, 'email_configured', lambda: True)
        sent_emails = []
        monkeypatch.setattr(
            campaign_routes,
            'send_email_async',
            lambda to, subject, body_text, body_html: sent_emails.append((to, subject, body_text, body_html)),
        )

        preview = _preview(client, admin_token, channel='email')
        response = client.post('/campaigns/api', json={
            'channel': 'email',
            'audience_type': 'both',
            'subject': 'Campaign update',
            'message': 'Hello {name}\n<script>alert(1)</script>',
            'preview_token': preview['preview_token'],
        }, headers=auth_header(admin_token))

        assert response.status_code == 200, response.get_data(as_text=True)
        assert len(sent_emails) == 1
        assert '<script>' not in sent_emails[0][3]
        assert '&lt;script&gt;' in sent_emails[0][3]

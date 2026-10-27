"""Synthetic device credentials; tests use the real registration and approval API."""
import hashlib


def device_headers(application, device_id='tv-test', token=None, shared=None):
    return {'Authorization': 'Bearer ' + (shared or application.extensions['lanscreen']['setting']('display_token')),
            'X-LanScreen-Device-Id': device_id,
            'X-LanScreen-Device-Token': token or hashlib.sha256(('synthetic-' + device_id).encode()).hexdigest()}


def register(application, device_id='tv-test', token=None, **body):
    headers = device_headers(application, device_id, token)
    response = application.test_client().post('/api/player/register', headers=headers, json={
        'deviceId': device_id, 'name': 'Test TV ' + device_id, 'version': '1.3.0', 'protocolVersion': 3, **body})
    return response, headers


def approved_headers(context, device_id='tv-test', token=None):
    application, admin, csrf = context[:3]
    response, headers = register(application, device_id, token)
    assert response.status_code == 200, response.json
    if response.json['bindingState'] == 'pending':
        approved = admin.post('/api/devices/' + device_id + '/approve',
                              json={'bindingVersion': response.json['bindingVersion']}, headers=csrf)
        assert approved.status_code == 200, approved.json
    return headers

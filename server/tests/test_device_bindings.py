"""Binding authorization, immutable receipts and concurrency; all synthetic files are retained."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from binding_helpers import approved_headers, device_headers, register

TEST_ROOT = Path(os.environ.get('LANSCREEN_TEST_ROOT', Path.cwd() / 'work' / 'binding-tests'))


@pytest.fixture
def server():
    application = create_app(TEST_ROOT / ('binding-' + uuid.uuid4().hex), testing=True)
    admin = application.test_client()
    setup = admin.post('/api/setup', json={
        'setupCode': application.extensions['lanscreen']['setting']('setup_code'),
        'password': 'Synthetic-Binding-Password-2026'})
    assert setup.status_code == 200
    try:
        yield application, admin, {'X-CSRF-Token': setup.json['csrf']}
    finally:
        application.extensions['lanscreen']['stop']()


def action(server, operation, version=1, device='tv-test', client=None, csrf=None):
    return (client or server[1]).post('/api/devices/' + device + '/' + operation,
                                     json={'bindingVersion': version}, headers=csrf or server[2])


def detail(server, revision=1):
    return server[1].get('/api/publications/' + str(revision)).json


def publish(server):
    response = server[1].post('/api/publish', json={'items': []}, headers=server[2])
    assert response.status_code == 200
    return response.json['revision']


def beat(server, headers, revision=-1, **extra):
    return server[0].test_client().post('/api/player/heartbeat', headers=headers, json={
        'deviceId': headers['X-LanScreen-Device-Id'], 'protocolVersion': 3,
        'appliedRevision': revision, 'downloadedRevision': revision, **extra})


def asset(server):
    application = server[0]
    item = uuid.uuid4().hex
    folder = application.extensions['lanscreen']['data'] / 'media' / item
    folder.mkdir()
    file = folder / 'page.png'
    file.write_bytes(b'synthetic fixture bytes for Range and authorization')
    url = '/media/' + item + '/page.png'
    with application.extensions['lanscreen']['db']() as c:
        c.execute('INSERT INTO media(id,name,kind,size,status,pages,created,source) VALUES (?,?,?,?,?,?,?,?)',
                  (item, 'Synthetic', 'image', file.stat().st_size, 'ready',
                   json.dumps([{'url': url, 'bytes': file.stat().st_size}]), time.time(), str(file)))
    return url


def test_shared_key_alone_has_no_manifest_heartbeat_or_media_backdoor(server):
    url = asset(server)
    stranger = server[0].test_client()
    shared = {'Authorization': device_headers(server[0])['Authorization']}
    for path in ('/api/player/manifest', url):
        response = stranger.get(path, headers=shared)
        assert response.status_code == 426 and response.json['code'] == 'upgrade_required'
        assert stranger.get(path).status_code == 401
    response = stranger.post('/api/player/heartbeat', headers=shared,
                             json={'deviceId': 'old-tv', 'protocolVersion': 2})
    assert response.status_code == 426
    assert server[1].get('/api/dashboard').json['devices'] == []
    assert stranger.get('/api/player/manifest', headers=device_headers(server[0])).json['code'] == 'device_unknown'


def test_pending_approval_credentials_and_admin_preview(server):
    url = asset(server)
    response, headers = register(server[0])
    assert response.status_code == 200 and response.json['bindingState'] == 'pending'
    assert response.json['bindingVersion'] == 1
    client = server[0].test_client()
    for path in ('/api/player/manifest', url):
        denied = client.get(path, headers=headers)
        assert denied.status_code == 403 and denied.json['code'] == 'device_pending'
    assert beat(server, headers).json['code'] == 'device_pending'
    assert action(server, 'cleanup').status_code == 409
    assert action(server, 'approve').status_code == 200
    assert action(server, 'approve').status_code == 200
    assert client.get('/api/player/manifest', headers=headers).json['bindingVersion'] == 1
    assert beat(server, headers).json['bindingVersion'] == 1
    with client.get(url, headers={**headers, 'Range': 'bytes=0-7'}) as response:
        assert response.status_code == 206 and response.data == b'syntheti'
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert all(header in response.headers['Vary'] for header in headers)
    with server[1].get(url) as response:
        assert response.status_code == 200
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert 'X-LanScreen-Device-Token' in response.headers['Vary']
    assert server[1].get('/api/preview/manifest').status_code == 200
    invalid = {**headers, 'X-LanScreen-Device-Token': 'f' * 64}
    assert client.get(url, headers=invalid).json['code'] == 'device_token_invalid'
    assert register(server[0], token='f' * 64)[0].json['code'] == 'device_token_invalid'
    assert client.post('/api/player/heartbeat', headers=headers,
                       json={'deviceId': 'different', 'protocolVersion': 3}).json['code'] == 'device_token_invalid'


def test_dashboard_never_exposes_device_credential_and_same_registration_does_not_write(server):
    response, headers = register(server[0])
    digest = hashlib.sha256(headers['X-LanScreen-Device-Token'].encode()).hexdigest()
    with server[0].extensions['lanscreen']['db']() as c:
        row = c.execute('SELECT * FROM devices').fetchone()
        assert row['device_token_hash'] == digest
        c.execute("CREATE TRIGGER no_repeat_registration_update BEFORE UPDATE ON devices BEGIN SELECT RAISE(ABORT,'unexpected write'); END")
    assert register(server[0])[0].json == response.json
    dashboard = server[1].get('/api/dashboard')
    assert digest not in dashboard.get_data(as_text=True)
    assert headers['X-LanScreen-Device-Token'] not in dashboard.get_data(as_text=True)
    device = dashboard.json['devices'][0]
    assert device['bindingState'] == 'pending' and device['bindingVersion'] == 1 and device['unboundAt'] is None
    assert not device['cleanupSupported'] and not device['online']
    assert not any(key in device for key in ('device_token_hash', 'binding_state', 'binding_version'))


def test_active_registration_poll_is_read_only(server):
    approved_headers(server)
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute("CREATE TRIGGER no_repeat_registration_update BEFORE UPDATE ON devices BEGIN SELECT RAISE(ABORT,'unexpected write'); END")
    assert register(server[0])[0].json['bindingState'] == 'active'


@pytest.mark.parametrize('operation', ['approve', 'unbind'])
def test_admin_binding_actions_require_session_csrf_and_current_integer_generation(server, operation):
    register(server[0])
    path = '/api/devices/tv-test/' + operation
    assert server[0].test_client().post(path, json={'bindingVersion': 1}, headers=server[2]).status_code == 401
    assert server[1].post(path, json={'bindingVersion': 1}).status_code == 403
    for value in (True, False, None, '1', 1.0, -1, 2 ** 63):
        assert server[1].post(path, json={'bindingVersion': value}, headers=server[2]).status_code == 400
    assert action(server, operation, 0).status_code == 409
    assert action(server, operation, device='missing').status_code == 404
    assert server[1].post(path, json={'bindingVersion': 1},
                          headers={**server[2], 'Origin': 'https://other.example'}).status_code == 403


@pytest.mark.parametrize('body', [[], [1], 'text', 12, True, None])
def test_nonobject_json_is_rejected_without_mutation(server, body):
    headers = approved_headers(server)
    for path in ('/api/player/register', '/api/player/heartbeat'):
        result = server[0].test_client().post(path, data=json.dumps(body), content_type='application/json', headers=headers)
        assert result.status_code == 400
    for operation in ('approve', 'unbind'):
        result = server[1].post('/api/devices/tv-test/' + operation, data=json.dumps(body),
                                content_type='application/json', headers=server[2])
        assert result.status_code == 400
    assert register(server[0])[0].json['bindingState'] == 'active'


@pytest.mark.parametrize('protocol', [None, True, 1, 2, 4, '3', 3.0])
def test_only_protocol_three_register_and_heartbeat_accepted(server, protocol):
    assert register(server[0], protocolVersion=protocol)[0].status_code == 426
    headers = approved_headers(server)
    assert beat(server, headers, protocolVersion=protocol).status_code == 426


@pytest.mark.parametrize('device', ['../tv', 'tv/name', 'a' * 101, '电视', 'tv test', '-tv'])
def test_device_identifier_is_ascii_bounded_and_path_safe(server, device):
    headers = device_headers(server[0], device)
    response = server[0].test_client().post('/api/player/register', headers=headers,
                                           json={'deviceId': device, 'protocolVersion': 3})
    assert response.status_code == 403 and response.json['code'] == 'device_token_invalid'
    assert server[1].get('/api/dashboard').json['devices'] == []


def test_header_body_id_mismatch_and_pending_token_takeover_rejected(server):
    response, headers = register(server[0])
    assert response.status_code == 200
    mismatch = server[0].test_client().post('/api/player/register', headers=headers,
                                           json={'deviceId': 'other', 'protocolVersion': 3})
    assert mismatch.json['code'] == 'device_token_invalid'
    assert register(server[0], token='e' * 64)[0].json['code'] == 'device_token_invalid'
    assert register(server[0])[0].json['bindingVersion'] == 1


def test_unbind_withdraws_current_only_preserves_old_receipts_and_blocks_all_player_routes(server):
    url = asset(server)
    headers = approved_headers(server)
    publish(server)
    assert beat(server, headers, 1).status_code == 200
    old = detail(server)
    publish(server)
    assert beat(server, headers, 2).status_code == 200
    current_before = detail(server, 2)
    assert action(server, 'unbind').status_code == 200
    first = server[1].get('/api/dashboard').json['devices'][0]['unboundAt']
    assert action(server, 'unbind').status_code == 200
    assert server[1].get('/api/dashboard').json['devices'][0]['unboundAt'] == first
    previous = detail(server)
    assert previous['targets'][0]['appliedAt'] == old['targets'][0]['appliedAt']
    assert previous['appliedCount'] == 1 and previous['withdrawnCount'] == 0
    current = detail(server, 2)
    assert current['targetCount'] == current['appliedCount'] == current['onlineCount'] == current['offlineCount'] == 0
    assert current['withdrawnCount'] == current['recordedTargetCount'] == current['counts']['withdrawn'] == 1
    assert current['targets'][0]['stage'] == 'withdrawn'
    assert current['targets'][0]['appliedAt'] == current_before['targets'][0]['appliedAt']
    assert current['targets'][0]['withdrawReason'] == 'device_unbound'
    for path in ('/api/player/manifest', url):
        denied = server[0].test_client().get(path, headers=headers)
        assert denied.status_code == 403 and denied.json['code'] == 'device_revoked'
    assert beat(server, headers, 2).json['code'] == 'device_revoked'
    assert register(server[0])[0].json['code'] == 'device_revoked'
    assert action(server, 'cleanup').status_code == 409
    uppercase = {**headers, 'X-LanScreen-Device-Token': headers['X-LanScreen-Device-Token'].upper()}
    assert beat(server, uppercase, 2).json['code'] == 'device_revoked'
    assert register(server[0], token=uppercase['X-LanScreen-Device-Token'])[0].json['code'] == 'device_revoked'
    publish(server)
    assert detail(server, 3)['targetCount'] == detail(server, 3)['recordedTargetCount'] == 0


def test_rebinding_same_revision_uses_new_target_and_old_tokens_stay_revoked_across_rounds_and_restart(server):
    old_headers = approved_headers(server)
    publish(server)
    assert beat(server, old_headers, 1).status_code == 200
    old_receipt = detail(server)['targets'][0]['appliedAt']
    assert action(server, 'unbind').status_code == 200
    response, new_headers = register(server[0], token='b' * 64)
    assert response.json['bindingVersion'] == 2 and response.json['bindingState'] == 'pending'
    assert action(server, 'approve', 1).status_code == 409
    assert action(server, 'unbind', 1).status_code == 409
    assert beat(server, new_headers, 1).json['code'] == 'device_pending'
    assert action(server, 'approve', 2).status_code == 200
    record = detail(server)
    assert record['recordedTargetCount'] == 2 and record['withdrawnCount'] == record['targetCount'] == 1
    assert record['appliedCount'] == record['downloadedCount'] == 0
    assert [t['bindingVersion'] for t in record['targets']] == [1, 2]
    assert record['targets'][0]['appliedAt'] == old_receipt and record['targets'][1]['appliedAt'] is None
    assert beat(server, old_headers, 1).json['code'] == 'device_revoked'
    assert beat(server, new_headers, 1).status_code == 200
    assert detail(server)['appliedCount'] == 1
    assert action(server, 'unbind', 2).status_code == 200
    assert register(server[0])[0].json['code'] == 'device_revoked'
    assert register(server[0], token='b' * 64)[0].json['code'] == 'device_revoked'
    assert register(server[0], token='c' * 64)[0].json['bindingVersion'] == 3
    restarted = create_app(server[0].extensions['lanscreen']['data'], testing=True)
    try:
        assert register(restarted)[0].json['code'] == 'device_revoked'
        assert register(restarted, token='b' * 64)[0].json['code'] == 'device_revoked'
        assert register(restarted, token='c' * 64)[0].json['bindingState'] == 'pending'
    finally:
        restarted.extensions['lanscreen']['stop']()


def test_new_binding_cannot_acknowledge_old_publication_or_inherit_online_status(server):
    first = approved_headers(server)
    publish(server)
    assert beat(server, first).status_code == 200
    publish(server)
    action(server, 'unbind')
    second = approved_headers(server, token='d' * 64)
    assert beat(server, second, 1).status_code == 200
    old = detail(server)
    assert old['targets'][0]['appliedAt'] is None and old['targets'][0]['downloadedAt'] is None
    assert old['targets'][0]['lastSeen'] is None and not old['targets'][0]['online']
    assert old['counts']['superseded'] == 1


def test_failed_approval_rolls_back_binding_and_target_together(server):
    register(server[0]); publish(server)
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute("CREATE TRIGGER fail_join BEFORE INSERT ON publication_targets BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    assert action(server, 'approve').status_code == 500
    assert register(server[0])[0].json['bindingState'] == 'pending'
    assert detail(server)['targetCount'] == 0


def test_pending_admission_is_bounded_and_existing_poll_does_not_consume_capacity(server):
    register(server[0])
    with server[0].extensions['lanscreen']['db']() as c:
        c.executemany("INSERT INTO devices(id,binding_state,binding_version) VALUES (?,'pending',1)",
                      [('synthetic-pending-' + str(i),) for i in range(99)])
    rejected = register(server[0], 'overflow')[0]
    assert rejected.status_code == 429 and rejected.json['code'] == 'pending_limit'
    assert register(server[0])[0].status_code == 200
    assert action(server, 'unbind').status_code == 200
    assert register(server[0], 'overflow')[0].status_code == 200


def test_revoked_notice_survives_shared_key_rotation_without_granting_active_old_key_access(server):
    url = asset(server)
    revoked = approved_headers(server)
    active = approved_headers(server, 'active-tv')
    previous = server[0].extensions['lanscreen']['setting']('display_token')
    changed = server[1].put('/api/display-token', headers=server[2], json={
        'displayToken': 'Rotated_Shared.Key-2026', 'currentDisplayToken': previous})
    assert changed.status_code == 200
    assert action(server, 'unbind').status_code == 200
    client = server[0].test_client()
    for path in ('/api/player/manifest', url):
        response = client.get(path, headers=revoked)
        assert response.status_code == 403 and response.json['code'] == 'device_revoked'
        denied = client.get(path, headers=active)
        assert denied.status_code == 401 and denied.json['code'] == 'key_invalid'
    for path in ('/api/player/heartbeat', '/api/player/register'):
        response = client.post(path, headers=revoked, json={'deviceId': 'tv-test', 'protocolVersion': 3})
        assert response.status_code == 403 and response.json['code'] == 'device_revoked'
        denied = client.post(path, headers=active, json={'deviceId': 'active-tv', 'protocolVersion': 3})
        assert denied.status_code == 401 and denied.json['code'] == 'key_invalid'


def test_concurrent_pending_admission_has_only_one_last_slot(server):
    with server[0].extensions['lanscreen']['db']() as c:
        c.executemany("INSERT INTO devices(id,binding_state,binding_version) VALUES (?,'pending',1)",
                      [('synthetic-pending-' + str(i),) for i in range(99)])
    barrier = threading.Barrier(2)
    def request(device):
        barrier.wait()
        return register(server[0], device)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request, 'candidate-a'), pool.submit(request, 'candidate-b')]
        results = [future.result(timeout=40) for future in futures]
    assert sorted(response.status_code for response in results) == [200, 429]
    assert len(server[1].get('/api/dashboard').json['devices']) == 100


def test_concurrent_approve_and_unbind_finishes_unbound_without_auto_reapproval(server):
    register(server[0]); publish(server)
    second = server[0].test_client()
    login = second.post('/api/login', json={'password': 'Synthetic-Binding-Password-2026'})
    csrf = {'X-CSRF-Token': login.json['csrf']}
    barrier = threading.Barrier(2)
    def approve():
        barrier.wait()
        return action(server, 'approve')
    def unbind():
        barrier.wait()
        return action(server, 'unbind', client=second, csrf=csrf)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve), pool.submit(unbind)]
        results = [future.result(timeout=40) for future in futures]
    assert results[0].status_code in (200, 409) and results[1].status_code == 200
    assert register(server[0])[0].json['code'] == 'device_revoked'
    assert detail(server)['targetCount'] == 0
    assert action(server, 'approve').status_code == 409


def test_concurrent_unbind_publish_and_heartbeat_cannot_revive_target(server):
    headers = approved_headers(server)
    publish(server)
    second_admin = server[0].test_client()
    login = second_admin.post('/api/login', json={'password': 'Synthetic-Binding-Password-2026'})
    csrf = {'X-CSRF-Token': login.json['csrf']}
    barrier = threading.Barrier(3)
    def unbind():
        barrier.wait()
        return action(server, 'unbind', client=second_admin, csrf=csrf)
    def publication():
        barrier.wait()
        return publish(server)
    def heartbeat():
        barrier.wait()
        return beat(server, headers, 1)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(unbind), pool.submit(publication), pool.submit(heartbeat)]
        results = [future.result(timeout=40) for future in futures]
    assert results[0].status_code == 200 and results[1] == 2 and results[2].status_code in (200, 403)
    current = detail(server, 2)
    assert current['targetCount'] == 0
    assert all(t['stage'] == 'withdrawn' for t in current['targets'])
    assert beat(server, headers, 2).json['code'] == 'device_revoked'
    assert register(server[0])[0].json['code'] == 'device_revoked'


def test_concurrent_duplicate_register_is_one_pending_generation(server):
    barrier = threading.Barrier(2)
    def request():
        barrier.wait()
        return register(server[0])[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=40) for future in [pool.submit(request), pool.submit(request)]]
    assert all(response.status_code == 200 and response.json['bindingVersion'] == 1 for response in results)
    assert len(server[1].get('/api/dashboard').json['devices']) == 1


def test_legacy_schema_migration_preserves_history_and_withdraws_only_current_on_upgrade():
    directory = TEST_ROOT / ('old-binding-schema-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / 'library.sqlite3') as c:
        c.executescript('''
          CREATE TABLE devices(id TEXT PRIMARY KEY,name TEXT,version TEXT,status TEXT,current_item TEXT,last_seen REAL);
          INSERT INTO devices VALUES ('old-tv','Old TV','1.2.1','playing','old',123);
          CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          INSERT INTO settings VALUES ('revision','2');
          CREATE TABLE publications(revision INTEGER PRIMARY KEY,source TEXT NOT NULL,created_at REAL,
            recorded_at REAL NOT NULL,snapshot TEXT NOT NULL,cache_policy TEXT NOT NULL,item_count INTEGER NOT NULL,
            page_count INTEGER NOT NULL,superseded_at REAL,superseded_by_revision INTEGER);
          INSERT INTO publications VALUES (1,'publish',10,10,'[]','{}',0,0,20,2);
          INSERT INTO publications VALUES (2,'publish',20,20,'[]','{}',0,0,NULL,NULL);
          CREATE TABLE publication_targets(revision INTEGER NOT NULL,device_id TEXT NOT NULL,device_name TEXT NOT NULL,
            joined_at REAL NOT NULL,joined_later INTEGER NOT NULL DEFAULT 0,downloaded_at REAL,applied_at REAL,
            PRIMARY KEY(revision,device_id));
          INSERT INTO publication_targets VALUES (1,'old-tv','Old TV',10,0,11,12);
          INSERT INTO publication_targets VALUES (2,'old-tv','Old TV',20,0,21,NULL);
        ''')
    application = create_app(directory, testing=True)
    try:
        admin = application.test_client()
        result = admin.post('/api/setup', json={'password': 'Synthetic-Binding-Password-2026',
                                               'setupCode': application.extensions['lanscreen']['setting']('setup_code')})
        context = (application, admin, {'X-CSRF-Token': result.json['csrf']})
        device = admin.get('/api/dashboard').json['devices'][0]
        assert device['bindingState'] == 'legacy' and device['bindingVersion'] == 0
        assert detail(context)['targets'][0]['appliedAt'] == 12
        assert detail(context, 2)['targets'][0]['bindingVersion'] == 0
        with application.extensions['lanscreen']['db']() as c:
            assert c.execute('SELECT COUNT(*) FROM publication_targets_before_binding').fetchone()[0] == 2
            pk = [r['name'] for r in sorted(c.execute('PRAGMA table_info(publication_targets)'), key=lambda r: r['pk']) if r['pk']]
            assert pk == ['revision', 'device_id', 'binding_version']
        response, headers = register(application, 'old-tv')
        assert response.json['bindingVersion'] == 1
        assert detail(context)['targets'][0]['appliedAt'] == 12 and detail(context)['withdrawnCount'] == 0
        assert detail(context, 2)['targetCount'] == 0 and detail(context, 2)['withdrawnCount'] == 1
        assert detail(context, 2)['targets'][0]['withdrawReason'] == 'binding_replaced'
        assert action(context, 'approve', device='old-tv').status_code == 200
        assert detail(context, 2)['recordedTargetCount'] == 2
        assert beat(context, headers, 1).status_code == 200
        assert detail(context)['targets'][0]['appliedAt'] == 12
        assert detail(context, 2)['appliedCount'] == 0
    finally:
        application.extensions['lanscreen']['stop']()


def test_binding_migration_failure_rolls_back_rename_columns_and_copy(monkeypatch):
    directory = TEST_ROOT / ('migration-failure-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    database = directory / 'library.sqlite3'
    with sqlite3.connect(database) as c:
        c.executescript('''
          CREATE TABLE devices(id TEXT PRIMARY KEY,name TEXT,version TEXT,status TEXT,current_item TEXT,last_seen REAL);
          INSERT INTO devices VALUES ('old-tv','Old TV','1.2.1','playing','old',123);
          CREATE TABLE publication_targets(revision INTEGER NOT NULL,device_id TEXT NOT NULL,device_name TEXT NOT NULL,
            joined_at REAL NOT NULL,joined_later INTEGER NOT NULL DEFAULT 0,downloaded_at REAL,applied_at REAL,
            PRIMARY KEY(revision,device_id));
          INSERT INTO publication_targets VALUES (1,'old-tv','Old TV',10,0,11,12);
        ''')
    original_connect = sqlite3.connect
    class FailMigrationCopy(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if 'FROM publication_targets_before_binding' in statement:
                raise sqlite3.OperationalError('synthetic interrupted migration')
            return super().execute(statement, *args, **kwargs)
    def connect(*args, **kwargs):
        return original_connect(*args, factory=FailMigrationCopy, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, 'connect', connect)
        with pytest.raises(sqlite3.OperationalError, match='synthetic interrupted'):
            create_app(directory, testing=True)
    with original_connect(database) as c:
        assert c.execute('SELECT * FROM publication_targets').fetchall() == [(1, 'old-tv', 'Old TV', 10, 0, 11, 12)]
        assert 'binding_state' not in [row[1] for row in c.execute('PRAGMA table_info(devices)')]
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='publication_targets_before_binding'").fetchone()
    recovered = create_app(directory, testing=True)
    try:
        with recovered.extensions['lanscreen']['db']() as c:
            assert c.execute('SELECT binding_version,applied_at FROM publication_targets').fetchone()[:] == (0, 12)
            assert c.execute('SELECT applied_at FROM publication_targets_before_binding').fetchone()[0] == 12
    finally:
        recovered.extensions['lanscreen']['stop']()

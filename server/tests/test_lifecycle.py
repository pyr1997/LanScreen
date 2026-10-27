"""Lifecycle integration checks. Removal is replaced by relocation; all fixture bytes are retained."""
from pathlib import Path
import concurrent.futures
import hashlib
import io
import json
import os
import sqlite3
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module
from binding_helpers import approved_headers

TEST_ROOT = Path(os.environ.get('LANSCREEN_TEST_ROOT', Path.cwd() / 'work' / 'lifecycle-tests'))


@pytest.fixture
def lifecycle(monkeypatch):
    directory = TEST_ROOT / ('lifecycle-' + uuid.uuid4().hex)
    application = module.create_app(directory, testing=True)
    client = application.test_client()
    result = client.post('/api/setup', json={'setupCode': application.extensions['lanscreen']['setting']('setup_code'),
                                           'password': 'Synthetic-Test-Password-2026'})
    headers = {'X-CSRF-Token': result.json['csrf']}
    preserve = directory / 'preserved-removals'
    preserve.mkdir()
    moves = []

    def retain(name, fd, is_directory):
        destination = preserve / (uuid.uuid4().hex + '-' + name)
        os.rename(name, destination, src_dir_fd=fd)
        moves.append((destination, is_directory))

    monkeypatch.setattr(module, '_remove_owned_entry', retain)
    yield application, client, headers, moves, retain
    application.extensions['lanscreen']['executor'].shutdown(wait=True)


def seed(context, name='宣传.png', state='ready', nested=False):
    application = context[0]
    item_id = uuid.uuid4().hex
    folder = application.extensions['lanscreen']['data'] / 'media' / item_id
    folder.mkdir()
    source = folder / 'original.png'
    source.write_bytes(b'original synthetic content')
    converted = folder / 'page.png'
    converted.write_bytes(b'converted synthetic content')
    if nested:
        (folder / 'profile' / 'user').mkdir(parents=True)
        (folder / 'profile' / 'user' / 'settings.xml').write_text('synthetic settings')
    pages = [{'url': f'/media/{item_id}/page.png', 'id': item_id + ':page.png', 'type': 'image',
              'sha256': hashlib.sha256(converted.read_bytes()).hexdigest(), 'bytes': converted.stat().st_size}]
    with application.extensions['lanscreen']['db']() as connection:
        connection.execute('INSERT INTO media(id,name,kind,size,status,pages,created,source) VALUES (?,?,?,?,?,?,?,?)',
                           (item_id, name, 'image', source.stat().st_size, state, json.dumps(pages), time.time(), str(source)))
    return item_id, folder


def preview(context, item_id):
    response = context[1].get(f'/api/media/{item_id}/deletion-preview')
    assert response.status_code == 200, response.json
    return response.json


def erase(context, item_id, snapshot=None):
    snapshot = snapshot or preview(context, item_id)
    return context[1].post(f'/api/media/{item_id}/delete',
                           json={'confirmName': snapshot['name'], 'deletionToken': snapshot['deletionToken']}, headers=context[2])


def player(context, device_id='tv-test'):
    application, client = context[:2]
    return application.test_client(), approved_headers(context, device_id)


def test_delete_confirmation_archive_and_no_real_fixture_deletion(lifecycle):
    application, client, headers, moves, _ = lifecycle
    item_id, folder = seed(lifecycle, nested=True)
    original_bytes = sum(path.stat().st_size for path in folder.rglob('*') if path.is_file())
    stranger = application.test_client()
    assert stranger.get(f'/api/media/{item_id}/deletion-preview').status_code == 401
    assert stranger.post(f'/api/media/{item_id}/delete', json={}).status_code == 401
    assert client.post(f'/api/media/{item_id}/delete', json={}).status_code == 403
    assert client.post(f'/api/media/{item_id}/archive', headers=headers).status_code == 200
    dashboard = client.get('/api/dashboard').json
    assert dashboard['media'] == [] and dashboard['archivedMedia'][0]['id'] == item_id
    snapshot = preview(lifecycle, item_id)
    assert snapshot['allowed'] and snapshot['fileCount'] == 3 and snapshot['totalBytes'] == original_bytes
    assert all(entry['path'].startswith(item_id + '/') for entry in snapshot['files'])
    assert client.post(f'/api/media/{item_id}/delete', json={'confirmName': 'wrong', 'deletionToken': snapshot['deletionToken']}, headers=headers).status_code == 400
    assert client.post(f'/api/media/{item_id}/delete', json={'confirmName': snapshot['name'], 'deletionToken': 'forged'}, headers=headers).status_code == 409
    assert client.post(f'/api/media/{item_id}/delete', json={'confirmName': snapshot['name'], 'deletionToken': str(int(time.time()) + 300) + '.' + '测' * 64}, headers=headers).status_code == 409
    result = erase(lifecycle, item_id, snapshot)
    assert result.status_code == 200 and result.json['totalBytes'] == original_bytes
    assert not folder.exists()
    assert sum(path.stat().st_size for path, directory in moves if not directory) == original_bytes
    assert client.get('/api/dashboard').json['archivedMedia'] == []
    assert client.get(f'/api/media/{item_id}/deletion-preview').status_code == 404


@pytest.mark.parametrize('state', ['queued', 'processing', 'deleting'])
def test_busy_media_cannot_be_deleted(lifecycle, state):
    item_id, folder = seed(lifecycle, state=state)
    assert not preview(lifecycle, item_id)['allowed']
    assert erase(lifecycle, item_id).status_code == 409
    assert folder.exists()


def test_both_draft_and_published_references_block_delete(lifecycle):
    _, client, headers, _, _ = lifecycle
    item_id, folder = seed(lifecycle)
    entries = [{'mediaId': item_id, 'duration': 10}]
    snapshot = preview(lifecycle, item_id)
    assert client.put('/api/playlist', json={'items': entries}, headers=headers).status_code == 200
    assert erase(lifecycle, item_id, snapshot).status_code == 409
    assert client.post('/api/publish', json={'items': entries}, headers=headers).status_code == 200
    assert client.put('/api/playlist', json={'items': []}, headers=headers).status_code == 200
    assert not preview(lifecycle, item_id)['allowed']
    assert client.post('/api/publish', json={'items': []}, headers=headers).status_code == 200
    assert erase(lifecycle, item_id).status_code == 200


def test_changed_file_and_expired_preview_block_delete(lifecycle, monkeypatch):
    item_id, folder = seed(lifecycle)
    snapshot = preview(lifecycle, item_id)
    (folder / 'page.png').write_bytes(b'changed synthetic bytes')
    assert erase(lifecycle, item_id, snapshot).status_code == 409
    snapshot = preview(lifecycle, item_id)
    current = time.time()
    monkeypatch.setattr(module.time, 'time', lambda: current + 301)
    assert erase(lifecycle, item_id, snapshot).status_code == 409
    assert (folder / 'original.png').exists()


def test_symlinks_and_outside_source_are_rejected(lifecycle):
    application = lifecycle[0]
    item_id, folder = seed(lifecycle)
    outside = application.extensions['lanscreen']['data'] / 'outside.txt'
    outside.write_bytes(b'untouched external fixture')
    (folder / 'link').symlink_to(outside)
    assert not preview(lifecycle, item_id)['allowed']
    assert erase(lifecycle, item_id).status_code == 409
    assert outside.read_bytes() == b'untouched external fixture'
    second, second_folder = seed(lifecycle)
    with application.extensions['lanscreen']['db']() as connection:
        connection.execute('UPDATE media SET source=? WHERE id=?', (str(outside), second))
    assert not preview(lifecycle, second)['allowed']
    third, third_folder = seed(lifecycle)
    third_folder.rename(third_folder.with_name(third + '-retained'))
    third_folder.symlink_to(second_folder, target_is_directory=True)
    assert not preview(lifecycle, third)['allowed']
    assert second_folder.exists()


def test_partial_failure_remains_visible_and_retryable(lifecycle, monkeypatch):
    application, client, _, moves, retain = lifecycle
    item_id, folder = seed(lifecycle, nested=True)
    count = [0]
    def fail_second(name, fd, is_directory):
        count[0] += 1
        if count[0] == 2:
            raise PermissionError('synthetic injected failure')
        retain(name, fd, is_directory)
    monkeypatch.setattr(module, '_remove_owned_entry', fail_second)
    first = preview(lifecycle, item_id)
    assert erase(lifecycle, item_id, first).status_code == 500
    row = client.get('/api/dashboard').json['media'][0]
    assert row['status'] == 'delete_error' and row['error']
    assert client.post('/api/publish', json={'items': [{'mediaId': item_id}]}, headers=lifecycle[2]).status_code == 400
    second = preview(lifecycle, item_id)
    assert second['allowed'] and second['fileCount'] < first['fileCount']
    monkeypatch.setattr(module, '_remove_owned_entry', retain)
    assert erase(lifecycle, item_id, second).status_code == 200
    assert not folder.exists()
    assert sum(path.stat().st_size for path, directory in moves if not directory) == first['totalBytes']


def test_interrupted_deleting_recovers_for_explicit_retry(lifecycle, monkeypatch):
    application, client, _, _, retain = lifecycle
    item_id, folder = seed(lifecycle)
    def interrupt(name, fd, is_directory):
        raise KeyboardInterrupt('synthetic process interruption')
    monkeypatch.setattr(module, '_remove_owned_entry', interrupt)
    with pytest.raises(KeyboardInterrupt):
        erase(lifecycle, item_id)
    with application.extensions['lanscreen']['db']() as connection:
        assert connection.execute('SELECT status FROM media WHERE id=?', (item_id,)).fetchone()[0] == 'deleting'
    recovered = module.create_app(application.extensions['lanscreen']['data'], testing=True)
    try:
        with recovered.extensions['lanscreen']['db']() as connection:
            assert connection.execute('SELECT status FROM media WHERE id=?', (item_id,)).fetchone()[0] == 'delete_error'
        monkeypatch.setattr(module, '_remove_owned_entry', retain)
        assert erase(lifecycle, item_id).status_code == 200
    finally:
        recovered.extensions['lanscreen']['executor'].shutdown(wait=True)


def test_publish_and_second_delete_cannot_race_started_removal(lifecycle, monkeypatch):
    application, _, _, _, retain = lifecycle
    item_id, _ = seed(lifecycle)
    snapshot = preview(lifecycle, item_id)
    entered, release = threading.Event(), threading.Event()
    def pause(name, fd, is_directory):
        entered.set()
        assert release.wait(5)
        retain(name, fd, is_directory)
    monkeypatch.setattr(module, '_remove_owned_entry', pause)
    def authenticated():
        client = application.test_client()
        result = client.post('/api/login', json={'password': 'Synthetic-Test-Password-2026'})
        return client, {'X-CSRF-Token': result.json['csrf']}
    def deletion():
        client, headers = authenticated()
        return client.post(f'/api/media/{item_id}/delete', json={'confirmName': snapshot['name'], 'deletionToken': snapshot['deletionToken']}, headers=headers).status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(deletion)
        try:
            assert entered.wait(5)
            client, headers = authenticated()
            assert client.post('/api/publish', json={'items': [{'mediaId': item_id}]}, headers=headers).status_code == 400
            assert client.post(f'/api/media/{item_id}/delete', json={'confirmName': snapshot['name'], 'deletionToken': snapshot['deletionToken']}, headers=headers).status_code == 409
        finally:
            release.set()
        assert future.result(timeout=5) == 200


def test_cache_policy_bounds_and_manifest_bytes(lifecycle):
    application, client, headers = lifecycle[:3]
    assert client.get('/api/dashboard').json['cachePolicy'] == module.DEFAULT_CACHE_POLICY
    assert client.put('/api/cache-policy', json=module.DEFAULT_CACHE_POLICY).status_code == 403
    for policy in ({'maxBytes': True, 'reserveBytes': 256 * 1024**2},
                   {'maxBytes': 255 * 1024**2, 'reserveBytes': 256 * 1024**2},
                   {'maxBytes': 33 * 1024**3, 'reserveBytes': 256 * 1024**2},
                   {'maxBytes': 1024**3, 'reserveBytes': 127 * 1024**2},
                   {'maxBytes': 1024**3, 'reserveBytes': 5 * 1024**3}):
        assert client.put('/api/cache-policy', json=policy, headers=headers).status_code == 400
    policy = {'maxBytes': 1024**3, 'reserveBytes': 128 * 1024**2}
    response = client.put('/api/cache-policy', json=policy, headers=headers)
    assert response.status_code == 200 and response.json['revision'] == 1
    assert client.put('/api/cache-policy', json=policy, headers=headers).json['revision'] == 1
    item_id, folder = seed(lifecycle)
    with application.extensions['lanscreen']['db']() as connection:
        pages = json.loads(connection.execute('SELECT pages FROM media WHERE id=?', (item_id,)).fetchone()[0])
        pages[0].pop('bytes')
        connection.execute('UPDATE media SET pages=? WHERE id=?', (json.dumps(pages), item_id))
    client.post('/api/publish', json={'items': [{'mediaId': item_id}]}, headers=headers)
    tv, bearer = player(lifecycle)
    manifest = tv.get('/api/player/manifest', headers=bearer).json
    assert manifest['cachePolicy'] == policy
    assert manifest['items'][0]['bytes'] == (folder / 'page.png').stat().st_size


def test_device_offline_legacy_telemetry_and_acknowledgments(lifecycle):
    application, client, headers = lifecycle[:3]
    tv, bearer = player(lifecycle, 'v2')
    assert tv.post('/api/player/heartbeat', json={'deviceId': 'v2'}, headers=bearer).status_code == 426
    with application.extensions['lanscreen']['db']() as connection:
        connection.execute("INSERT INTO devices(id,name,last_seen) VALUES ('legacy','Old TV',1)")
    legacy = next(d for d in client.get('/api/dashboard').json['devices'] if d['id'] == 'legacy')
    assert legacy['bindingState'] == 'legacy' and legacy['bindingVersion'] == 0
    assert legacy['cacheBytes'] == legacy['freeBytes'] == -1
    assert client.post('/api/devices/legacy/cleanup', headers=headers).status_code == 409
    body = {'deviceId': 'v2', 'protocolVersion': 3, 'appliedRevision': 0, 'downloadedRevision': 0, 'cleanupRevision': 0,
            'cleanupState': 'complete', 'cacheBytes': 100, 'freeBytes': 4000}
    assert tv.post('/api/player/heartbeat', json=body, headers=bearer).json['cleanupRequest'] == 0
    assert client.post('/api/devices/v2/cleanup').status_code == 403
    assert client.post('/api/devices/v2/cleanup', headers=headers).json['cleanupRequest'] == 1
    device = client.get('/api/dashboard').json['devices'][0]
    assert device['updateState'] == 'applied' and device['cleanupDisplayState'] == 'pending'
    assert tv.post('/api/player/heartbeat', json=body, headers=bearer).json['cleanupRequest'] == 1
    body['cleanupCompletedRequest'] = 1
    tv.post('/api/player/heartbeat', json=body, headers=bearer)
    assert client.get('/api/dashboard').json['devices'][0]['cleanupDisplayState'] == 'complete'
    client.post('/api/publish', json={'items': []}, headers=headers)
    device = client.get('/api/dashboard').json['devices'][0]
    assert device['updateState'] == device['cleanupDisplayState'] == 'pending'
    body.update(appliedRevision=1, cleanupRevision=1)
    tv.post('/api/player/heartbeat', json=body, headers=bearer)
    with application.extensions['lanscreen']['db']() as connection:
        connection.execute('UPDATE devices SET last_seen=? WHERE id=?', (time.time() - 100, 'v2'))
    device = next(value for value in client.get('/api/dashboard').json['devices'] if value['id'] == 'v2')
    assert not device['online'] and device['cleanupDisplayState'] == device['updateState'] == 'offline'
    assert client.post('/api/devices/v2/cleanup', headers=headers).json['cleanupRequest'] == 2
    body.update(cleanupCompletedRequest=500)
    assert tv.post('/api/player/heartbeat', json=body, headers=bearer).status_code == 200
    assert client.get('/api/dashboard').json['devices'][0]['cleanupCompletedRequest'] == 1
    assert client.get('/api/dashboard').json['devices'][0]['cleanupDisplayState'] == 'pending'
    assert tv.post('/api/player/heartbeat', json={'deviceId': 'v2', 'protocolVersion': 3, 'cacheBytes': None}, headers=bearer).status_code == 400
    assert tv.post('/api/player/heartbeat', json={'deviceId': 'v2', 'protocolVersion': True}, headers=bearer).status_code == 426


def test_v1_device_schema_migration_preserves_identity():
    directory = TEST_ROOT / ('migration-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / 'library.sqlite3') as connection:
        connection.execute('CREATE TABLE devices(id TEXT PRIMARY KEY,name TEXT,version TEXT,status TEXT,current_item TEXT,last_seen REAL)')
        connection.execute('INSERT INTO devices VALUES (?,?,?,?,?,?)', ('old-tv', '大厅', '1.0.0', 'playing', 'old-item', 123))
    application = module.create_app(directory, testing=True)
    try:
        with application.extensions['lanscreen']['db']() as connection:
            device = connection.execute('SELECT * FROM devices').fetchone()
            assert device['id'] == 'old-tv' and device['current_item'] == 'old-item'
            assert device['protocolVersion'] == 1 and device['appliedRevision'] == -1 and device['cacheBytes'] == -1
    finally:
        application.extensions['lanscreen']['executor'].shutdown(wait=True)


def test_invalid_uploaded_presentation_remains_manageable(lifecycle):
    _, client, headers = lifecycle[:3]
    result = client.post('/api/media', data={'file': (io.BytesIO(b'synthetic-invalid-presentation'), 'bad.pptx')}, headers=headers)
    assert result.status_code == 400 and result.json['id']
    item_id = result.json['id']
    row = client.get('/api/dashboard').json['media'][0]
    assert row['status'] == 'error' and row['id'] == item_id
    assert preview(lifecycle, item_id)['allowed']
    assert erase(lifecycle, item_id).status_code == 200


def test_symlink_replacement_after_preview_cannot_escape(lifecycle):
    item_id, folder = seed(lifecycle)
    other_id, other_folder = seed(lifecycle)
    snapshot = preview(lifecycle, item_id)
    folder.rename(folder.with_name(item_id + '-retained'))
    folder.symlink_to(other_folder, target_is_directory=True)
    assert erase(lifecycle, item_id, snapshot).status_code == 409
    assert (other_folder / 'original.png').read_bytes() == b'original synthetic content'


def test_missing_directory_after_interruption_can_be_confirmed(lifecycle):
    item_id, folder = seed(lifecycle, state='delete_error')
    # Simulate prior completed file removal with an interrupted DB finalization, retaining all bytes.
    folder.rename(folder.with_name(item_id + '-retained'))
    snapshot = preview(lifecycle, item_id)
    assert snapshot['allowed'] and snapshot['fileCount'] == snapshot['totalBytes'] == 0
    assert erase(lifecycle, item_id, snapshot).status_code == 200


def test_removal_primitive_uses_only_checked_parent_descriptors(monkeypatch):
    calls = []
    monkeypatch.setattr(module.os, 'unlink', lambda name, **kw: calls.append(('file', name, kw)))
    monkeypatch.setattr(module.os, 'rmdir', lambda name, **kw: calls.append(('directory', name, kw)))
    module._remove_owned_entry('leaf', 123, False)
    module._remove_owned_entry('child', 456, True)
    assert calls == [('file', 'leaf', {'dir_fd': 123}), ('directory', 'child', {'dir_fd': 456})]

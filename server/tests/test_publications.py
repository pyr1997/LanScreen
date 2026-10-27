"""Publication audit and device acknowledgement regression tests; all data is retained."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from binding_helpers import approved_headers

TEST_ROOT = Path(os.environ.get('LANSCREEN_TEST_ROOT', Path.cwd() / 'work' / 'publication-tests'))


@pytest.fixture
def server():
    application = create_app(TEST_ROOT / uuid.uuid4().hex, testing=True)
    client = application.test_client()
    setup = client.post('/api/setup', json={
        'setupCode': application.extensions['lanscreen']['setting']('setup_code'),
        'password': 'TestOnly-Password-2026'})
    assert setup.status_code == 200
    try:
        yield application, client, {'X-CSRF-Token': setup.json['csrf']}
    finally:
        application.extensions['lanscreen']['executor'].shutdown(wait=True)


def beat(server, device='tv-1', revision=-1, downloaded=None, protocol=3):
    application, _, _ = server
    client = application.test_client()
    headers = approved_headers(server, device)
    response = client.post('/api/player/heartbeat', json={
        'deviceId': device, 'name': '大厅电视 ' + device, 'protocolVersion': protocol,
        'downloadedRevision': revision if downloaded is None else downloaded,
        'appliedRevision': revision, 'status': 'playing'}, headers=headers)
    assert response.status_code == 200
    return response


def publish(server, entries=None):
    _, client, csrf = server
    response = client.post('/api/publish', json={'items': entries or []}, headers=csrf)
    assert response.status_code == 200, response.json
    return response.json


def detail(server, revision):
    response = server[1].get('/api/publications/' + str(revision))
    assert response.status_code == 200
    return response.json


def media_metadata(server, name='公司宣传.pdf', kind='pdf', pages=3):
    application, _, _ = server
    media_id = uuid.uuid4().hex
    folder = application.extensions['lanscreen']['data'] / 'media' / media_id
    folder.mkdir()
    source = folder / 'original.synthetic'
    source.write_bytes(b'retained original')
    assets = []
    for i in range(pages):
        target = folder / ('page-' + str(i) + '.jpg')
        target.write_bytes(b'retained playback bytes')
        assets.append({'url': f'/media/{media_id}/{target.name}', 'bytes': target.stat().st_size})
    with application.extensions['lanscreen']['db']() as c:
        c.execute('INSERT INTO media (id,name,kind,status,pages,created,source) VALUES (?,?,?,?,?,?,?)',
                  (media_id, name, kind, 'ready', json.dumps(assets), time.time(), str(source)))
    return media_id


def test_no_history_or_current_publication_before_first_publish(server):
    assert server[1].get('/api/dashboard').json['currentPublication'] is None
    assert server[1].get('/api/publications').json == {
        'items': [], 'total': 0, 'page': 1, 'pageSize': 10, 'hasNext': False, 'cleanupSupported': True}
    assert server[1].get('/api/publications/1').status_code == 404
    assert server[1].get('/api/health').json['version'] == '1.3.2'


def test_publish_records_empty_playlist_without_fabricated_targets(server):
    result = publish(server)
    current = server[1].get('/api/dashboard').json['currentPublication']
    assert current == result['publication']
    assert current['revision'] == 1 and current['source'] == 'publish'
    assert current['isCurrent'] and current['createdAt'] > 0
    assert current['targetCount'] == current['appliedCount'] == current['itemCount'] == current['pageCount'] == 0
    assert detail(server, 1)['targets'] == []


def test_snapshot_immutable_after_rename_and_metadata_removal(server):
    media_id = media_metadata(server)
    publish(server, [{'mediaId': media_id, 'duration': 17}])
    expected = [{'mediaId': media_id, 'name': '公司宣传.pdf', 'kind': 'pdf', 'pageCount': 3, 'duration': 17}]
    assert detail(server, 1)['items'] == expected
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute('UPDATE media SET name=?,pages=? WHERE id=?', ('新名称', '[]', media_id))
    publish(server)
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute('DELETE FROM media WHERE id=?', (media_id,))
    assert detail(server, 1)['items'] == expected
    assert detail(server, 1)['pageCount'] == 3
    assert detail(server, 2)['items'] == []


def test_snapshot_preserves_duplicate_entries_and_individual_duration(server):
    media_id = media_metadata(server, kind='image', pages=1)
    publish(server, [{'mediaId': media_id, 'duration': 3}, {'mediaId': media_id, 'duration': 19}])
    record = detail(server, 1)
    assert [item['duration'] for item in record['items']] == [3, 19]
    assert record['itemCount'] == record['pageCount'] == 2
    assert all(set(item) == {'mediaId', 'name', 'kind', 'pageCount', 'duration'} for item in record['items'])


def test_initial_device_targets_include_active_online_offline_but_exclude_legacy(server):
    beat(server, 'online'); beat(server, 'offline')
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute('UPDATE devices SET last_seen=? WHERE id=?', (time.time() - 120, 'offline'))
        c.execute("INSERT INTO devices(id,name,last_seen) VALUES ('legacy','Old TV',?)", (time.time(),))
    publish(server)
    record = detail(server, 1)
    assert record['targetCount'] == 2
    assert record['onlineCount'] == 1 and record['offlineCount'] == 1
    assert record['counts']['pending'] == record['counts']['offline'] == 1
    assert record['counts']['unsupported'] == 0
    assert all(not target['joinedLater'] for target in record['targets'])


def test_new_device_added_only_to_current_revision_with_join_time(server):
    publish(server); publish(server)
    beat(server, 'late-tv', revision=2)
    assert detail(server, 1)['targetCount'] == 0
    current = detail(server, 2)
    target = current['targets'][0]
    assert target['joinedLater'] and target['joinedAt'] >= current['createdAt']
    assert target['stage'] == 'applied' and target['appliedAt'] >= target['joinedAt']
    beat(server, 'late-tv', revision=2)
    assert detail(server, 2)['targetCount'] == 1


def test_download_then_exact_apply_are_persisted_first_observations(server):
    beat(server); publish(server)
    beat(server, revision=-1, downloaded=1)
    downloaded = detail(server, 1)
    assert downloaded['counts']['downloaded'] == downloaded['downloadedCount'] == 1
    assert downloaded['appliedCount'] == 0
    beat(server, revision=1)
    applied = detail(server, 1)
    assert applied['appliedCount'] == 1 and applied['targets'][0]['stage'] == 'applied'
    beat(server, revision=1)
    again = detail(server, 1)
    assert again['targets'][0]['downloadedAt'] == downloaded['targets'][0]['downloadedAt']
    assert again['targets'][0]['appliedAt'] == applied['targets'][0]['appliedAt']


def test_higher_revision_never_acknowledges_older_revision(server):
    beat(server); publish(server)
    beat(server, revision=2)
    record = detail(server, 1)
    assert record['appliedCount'] == record['downloadedCount'] == 0
    assert record['targets'][0]['stage'] == 'pending'
    publish(server)
    assert detail(server, 2)['appliedCount'] == 0  # No fabricated seed from the previous future report.
    beat(server, revision=2)
    assert detail(server, 2)['appliedCount'] == 1
    assert detail(server, 1)['appliedCount'] == 0
    assert detail(server, 1)['targets'][0]['stage'] == 'superseded'


def test_superseded_partial_progress_is_frozen_but_late_exact_receipt_retained(server):
    beat(server, 'finished'); beat(server, 'pending')
    publish(server); beat(server, 'finished', revision=1)
    publish(server)
    old = detail(server, 1)
    assert old['supersededByRevision'] == 2 and not old['isCurrent']
    assert old['appliedCount'] == 1 and old['counts']['superseded'] == 1
    time.sleep(0.002)
    beat(server, 'pending', revision=1)
    late = detail(server, 1)
    target = next(t for t in late['targets'] if t['deviceId'] == 'pending')
    assert target['appliedAt'] > late['supersededAt'] and target['downloadedAt'] is not None
    assert target['stage'] == 'superseded' and late['appliedCount'] == 1
    assert detail(server, 2)['appliedCount'] == 0


def test_offline_after_applied_retains_receipt_but_counts_connectivity_separately(server):
    beat(server); publish(server); beat(server, revision=1)
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute('UPDATE devices SET last_seen=?', (time.time() - 100,))
    record = detail(server, 1)
    assert record['appliedCount'] == 1 and record['offlineCount'] == 1
    assert record['targets'][0]['stage'] == 'applied' and not record['targets'][0]['online']


def test_protocol_one_cannot_claim_acknowledgement(server):
    headers = approved_headers(server, 'tv-1')
    publish(server)
    response = server[0].test_client().post('/api/player/heartbeat', headers=headers, json={
        'deviceId': 'tv-1', 'protocolVersion': 1, 'appliedRevision': 1})
    assert response.status_code == 426 and response.json['code'] == 'upgrade_required'
    record = detail(server, 1)
    assert record['appliedCount'] == record['downloadedCount'] == 0


def test_cleanup_error_is_not_fabricated_publication_error(server):
    beat(server); publish(server)
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute("UPDATE devices SET cleanupError='old unversioned failure',cleanupState='error'")
    record = detail(server, 1)
    assert record['targets'][0]['stage'] == 'pending'
    assert 'error' not in record['targets'][0]


def test_cache_policy_revision_gets_its_own_snapshot_and_noop_does_not(server):
    media_id = media_metadata(server)
    publish(server, [{'mediaId': media_id, 'duration': 10}])
    policy = {'maxBytes': 3 * 1024 ** 3, 'reserveBytes': 256 * 1024 ** 2}
    result = server[1].put('/api/cache-policy', json=policy, headers=server[2])
    assert result.status_code == 200 and result.json['revision'] == 2
    record = detail(server, 2)
    assert record['source'] == 'cache_policy' and record['cachePolicy'] == policy
    assert record['items'] == detail(server, 1)['items']
    assert server[1].get('/api/dashboard').json['currentPublication']['revision'] == 2
    assert server[1].put('/api/cache-policy', json=policy, headers=server[2]).json['revision'] == 2
    assert server[1].get('/api/publications').json['total'] == 2


def test_history_admin_auth_and_no_key_disclosure(server):
    publish(server)
    stranger = server[0].test_client()
    bearer = {'Authorization': 'Bearer ' + server[0].extensions['lanscreen']['setting']('display_token')}
    for url in ('/api/publications', '/api/publications/1'):
        assert stranger.get(url).status_code == 401
        assert stranger.get(url, headers=bearer).status_code == 401
        response = server[1].get(url)
        assert response.headers['Cache-Control'] == 'no-store'
        assert 'displayToken' not in response.get_data(as_text=True)


@pytest.mark.parametrize('query', ['page=0', 'page=-1', 'page=no', 'page=1.1', 'page=1000001',
                                  'pageSize=0', 'pageSize=51', 'pageSize=no', 'pageSize=1.2'])
def test_pagination_limits(server, query):
    assert server[1].get('/api/publications?' + query).status_code == 400


def test_pagination_order_beyond_end_and_detail_bounds(server):
    for _ in range(5):
        publish(server)
    page = server[1].get('/api/publications?page=2&pageSize=2').json
    assert [p['revision'] for p in page['items']] == [3, 2]
    assert page['total'] == 5 and page['hasNext']
    assert all('items' not in p and 'targets' not in p for p in page['items'])
    assert not server[1].get('/api/publications?page=3&pageSize=2').json['hasNext']
    last = server[1].get('/api/publications?page=20&pageSize=2').json
    assert last['page'] == 3 and [item['revision'] for item in last['items']] == [1]
    assert server[1].get('/api/publications/0').status_code == 404
    assert server[1].get('/api/publications/9223372036854775808').status_code == 404


def test_failed_publication_rolls_back_revision_draft_and_history_together(server):
    publish(server)
    before = server[1].get('/api/dashboard').json
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute("CREATE TRIGGER test_abort_publication BEFORE INSERT ON publications BEGIN SELECT RAISE(ABORT,'test'); END")
    failed = server[1].post('/api/publish', json={'items': []}, headers=server[2])
    assert failed.status_code == 500
    after = server[1].get('/api/dashboard').json
    for field in ('revision', 'draft', 'published', 'currentPublication'):
        assert after[field] == before[field]
    assert server[1].get('/api/publications').json['total'] == 1


def test_invalid_publish_does_not_create_history(server):
    response = server[1].post('/api/publish', json={'items': [{'mediaId': 'missing'}]}, headers=server[2])
    assert response.status_code == 400
    assert server[1].get('/api/publications').json['total'] == 0


def test_concurrent_publishes_and_heartbeat_remain_atomic(server):
    application, _, _ = server
    clients = []
    for _ in range(2):
        client = application.test_client()
        login = client.post('/api/login', json={'password': 'TestOnly-Password-2026'})
        assert login.status_code == 200
        clients.append((client, {'X-CSRF-Token': login.json['csrf']}))
    beat(server, 'existing')
    barrier = threading.Barrier(3)
    def do_publish(index):
        barrier.wait()
        return clients[index][0].post('/api/publish', json={'items': []}, headers=clients[index][1])
    def do_beat():
        barrier.wait()
        return beat(server, 'new-concurrent', revision=-1)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(do_publish, 0), pool.submit(do_publish, 1), pool.submit(do_beat)]
        results = [f.result(timeout=40) for f in futures]
    assert all(r.status_code == 200 for r in results)
    assert sorted(r.json['revision'] for r in results[:2]) == [1, 2]
    assert detail(server, 1)['supersededByRevision'] == 2
    current = detail(server, 2)
    assert current['targetCount'] == 2 and len({t['deviceId'] for t in current['targets']}) == 2
    assert current['appliedCount'] == 0


def test_restart_keeps_history_and_receipts_without_duplicate_import(server):
    application, _, _ = server
    beat(server); publish(server); beat(server, revision=1)
    original = detail(server, 1)
    restarted = create_app(application.extensions['lanscreen']['data'], testing=True)
    try:
        client = restarted.test_client()
        client.post('/api/login', json={'password': 'TestOnly-Password-2026'})
        assert client.get('/api/publications').json['total'] == 1
        restored = client.get('/api/publications/1').json
        assert restored['createdAt'] == original['createdAt']
        assert restored['targets'][0]['appliedAt'] == original['targets'][0]['appliedAt']
    finally:
        restarted.extensions['lanscreen']['executor'].shutdown(wait=True)


def test_migration_imports_only_current_revision_with_unknown_publish_time(server):
    application, _, _ = server
    media_id = media_metadata(server)
    beat(server, revision=7)
    with application.extensions['lanscreen']['db']() as c:
        c.execute("UPDATE settings SET value='7' WHERE key='revision'")
        c.execute("UPDATE settings SET value=? WHERE key='published'", (json.dumps([{'mediaId': media_id, 'duration': 8}]),))
    restarted = create_app(application.extensions['lanscreen']['data'], testing=True)
    try:
        client = restarted.test_client()
        client.post('/api/login', json={'password': 'TestOnly-Password-2026'})
        records = client.get('/api/publications').json
        assert records['total'] == 1 and records['items'][0]['revision'] == 7
        record = client.get('/api/publications/7').json
        assert record['source'] == 'migrated' and record['createdAt'] is None
        assert record['recordedAt'] > 0 and record['items'][0]['name'] == '公司宣传.pdf'
        assert record['targets'][0]['appliedAt'] is None and record['appliedCount'] == 0
        assert client.get('/api/publications/6').status_code == 404
    finally:
        restarted.extensions['lanscreen']['executor'].shutdown(wait=True)


def test_old_snapshot_does_not_block_archive_or_delete_of_unpublished_media(server):
    media_id = media_metadata(server)
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute('UPDATE media SET source=? WHERE id=?',
                  (str(server[0].extensions['lanscreen']['data'] / 'media' / media_id / 'source.pdf'), media_id))
    publish(server, [{'mediaId': media_id, 'duration': 12}]); publish(server)
    assert server[1].post('/api/media/' + media_id + '/archive', json={}, headers=server[2]).status_code == 200
    folder = server[0].extensions['lanscreen']['data'] / 'media' / media_id
    folder.rename(folder.with_name(media_id + '-retained'))
    preview = server[1].get('/api/media/' + media_id + '/deletion-preview').json
    assert preview['allowed'] and preview['fileCount'] == 0  # All test bytes retained by the move above.
    result = server[1].post('/api/media/' + media_id + '/delete', json={
        'confirmName': '公司宣传.pdf', 'deletionToken': preview['deletionToken']}, headers=server[2])
    assert result.status_code == 200 and result.json['fileCount'] == 0
    assert detail(server, 1)['items'][0]['name'] == '公司宣传.pdf'

"""Explicit, previewed history cleanup; synthetic databases and media remain in the test root."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from test_publications import server, beat, publish, detail, media_metadata
import app as module
from binding_helpers import approved_headers


def preview(server, **body):
    return server[1].post('/api/publications/cleanup-preview', json=body, headers=server[2])


def cleanup(server, token, client=None, headers=None):
    return (client or server[1]).post('/api/publications/cleanup', json={'confirmationToken': token},
                                     headers=headers or server[2])


def selected(server, revisions):
    response = preview(server, revisions=revisions)
    assert response.status_code == 200, response.json
    return response.json


def seed_history(server, count):
    with server[0].extensions['lanscreen']['db']() as c:
        c.executemany('INSERT INTO publications '
                      '(revision,source,created_at,recorded_at,snapshot,cache_policy,item_count,page_count,superseded_at,superseded_by_revision) '
                      'VALUES (?,?,?,?,?,?,?,?,?,?)',
                      [(revision, 'publish', float(revision), float(revision), '[]', '{}', 0, 0,
                        float(revision + 1) if revision != count else None,
                        revision + 1 if revision != count else None) for revision in range(1, count + 1)])
        c.execute("UPDATE settings SET value=? WHERE key='revision'", (json.dumps(count),))


def second_admin(server):
    client = server[0].test_client()
    response = client.post('/api/login', json={'password': 'TestOnly-Password-2026'})
    assert response.status_code == 200
    return client, {'X-CSRF-Token': response.json['csrf']}


def test_cleanup_auth_csrf_origin_and_capability(server):
    seed_history(server, 2)
    token = selected(server, [1])['confirmationToken']
    stranger = server[0].test_client()
    for path, body in (('/api/publications/cleanup-preview', {'revisions': [1]}),
                       ('/api/publications/cleanup', {'confirmationToken': token})):
        assert stranger.post(path, json=body, headers=server[2]).status_code == 401
        assert server[1].post(path, json=body).status_code == 403
        assert server[1].post(path, json=body, headers={**server[2], 'Origin': 'https://other.example'}).status_code == 403
    assert server[1].get('/api/publications').json['cleanupSupported'] is True
    assert server[1].get('/api/publications').json['total'] == 2


@pytest.mark.parametrize('body', [None, [], [1], 'text', True, 1, {}, {'revisions': [1], 'keepLatest': 1},
                                 {'revisions': [1], 'extra': 1}, {'keepLatest': 1, 'extra': 1}])
def test_preview_rejects_wrong_body_or_mixed_modes(server, body):
    response = server[1].post('/api/publications/cleanup-preview', data=json.dumps(body),
                              content_type='application/json', headers=server[2])
    assert response.status_code == 400


@pytest.mark.parametrize('revisions', [[], None, True, 1, '1', {}, [True], [1.0], ['1'], [None], [{}],
                                      [-1], [0], [2 ** 63], [1, 1], list(range(1, 202))])
def test_preview_revisions_are_unique_bounded_positive_integers(server, revisions):
    assert preview(server, revisions=revisions).status_code == 400


@pytest.mark.parametrize('keep', [None, True, False, 0, -1, 5001, '100', 1.0, [], {}])
def test_keep_latest_validates_integer_limits(server, keep):
    assert preview(server, keepLatest=keep).status_code == 400


def test_current_and_missing_revisions_cannot_be_previewed(server):
    seed_history(server, 3)
    assert preview(server, revisions=[3]).status_code == 409
    assert preview(server, revisions=[1, 3]).status_code == 409
    assert preview(server, revisions=[1, 10]).status_code == 409
    assert server[1].get('/api/publications').json['total'] == 3


def test_empty_history_and_nothing_older_than_keep_limit(server):
    empty = preview(server, keepLatest=1).json
    assert empty['revisions'] == [] and empty['deleteCount'] == empty['totalCandidates'] == empty['targetCount'] == 0
    assert not empty['hasMore'] and empty['currentRevision'] == 0
    assert cleanup(server, empty['confirmationToken']).json == {
        'ok': True, 'deletedCount': 0, 'targetCount': 0, 'remainingCount': 0, 'currentRevision': 0, 'alreadyDeletedCount': 0}
    seed_history(server, 2)
    assert preview(server, keepLatest=5000).json['deleteCount'] == 0
    assert preview(server, keepLatest=1).json['revisions'] == [1]


def test_single_history_cleanup_keeps_current_configuration_media_and_device_state(server):
    media = media_metadata(server)
    beat(server)
    publish(server, [{'mediaId': media, 'duration': 9}]); beat(server, revision=1)
    publish(server, [{'mediaId': media, 'duration': 17}]); beat(server, revision=2)
    application = server[0]
    with application.extensions['lanscreen']['db']() as c:
        unchanged_before = {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table)]
                            for table in ('settings', 'media', 'devices', 'revoked_device_tokens')}
    media_root = application.extensions['lanscreen']['data'] / 'media'
    files_before = {str(path.relative_to(media_root)): path.read_bytes() for path in media_root.rglob('*') if path.is_file()}
    snapshot = selected(server, [1])
    assert snapshot['revisions'] == [1] and snapshot['deleteCount'] == snapshot['totalCandidates'] == snapshot['targetCount'] == 1
    assert snapshot['currentRevision'] == 2 and not snapshot['hasMore']
    assert 0 < snapshot['expiresIn'] <= 300
    assert server[1].get('/api/publications/1').status_code == 200  # Preview never mutates.
    result = cleanup(server, snapshot['confirmationToken'])
    assert result.status_code == 200 and result.json == {
        'ok': True, 'deletedCount': 1, 'targetCount': 1, 'remainingCount': 1, 'currentRevision': 2, 'alreadyDeletedCount': 0}
    assert server[1].get('/api/publications/1').status_code == 404
    assert detail(server, 2)['appliedCount'] == 1
    with application.extensions['lanscreen']['db']() as c:
        unchanged_after = {table: [tuple(row) for row in c.execute('SELECT * FROM ' + table)]
                           for table in unchanged_before}
        assert not c.execute('SELECT 1 FROM publication_targets WHERE revision=1').fetchone()
    assert unchanged_after == unchanged_before
    assert {str(path.relative_to(media_root)): path.read_bytes() for path in media_root.rglob('*') if path.is_file()} == files_before


def test_keep_latest_batches_oldest_200_and_confirmation_does_not_expand_after_new_publish(server):
    seed_history(server, 306)
    first = preview(server, keepLatest=100).json
    assert first['revisions'] == list(range(1, 201))
    assert first['deleteCount'] == 200 and first['totalCandidates'] == 206 and first['hasMore']
    # A full 200-entry token exceeds the old generic 8 KiB request limit.
    assert len(first['confirmationToken']) > 8192
    publish(server)
    result = cleanup(server, first['confirmationToken'])
    assert result.status_code == 200 and result.json['deletedCount'] == 200
    assert result.json['remainingCount'] == 107 and result.json['currentRevision'] == 307
    second = preview(server, keepLatest=100).json
    assert second['revisions'] == list(range(201, 208)) and second['totalCandidates'] == 7 and not second['hasMore']
    assert cleanup(server, second['confirmationToken']).json['remainingCount'] == 100
    assert preview(server, keepLatest=100).json['deleteCount'] == 0


def test_explicit_selection_only_deletes_selected_versions_and_overlapping_previews_are_idempotent(server):
    seed_history(server, 5)
    first = selected(server, [3, 1])
    second = selected(server, [1, 2])
    assert first['revisions'] == [1, 3]
    assert cleanup(server, first['confirmationToken']).json['deletedCount'] == 2
    result = cleanup(server, second['confirmationToken']).json
    assert result['deletedCount'] == result['alreadyDeletedCount'] == 1
    repeated = cleanup(server, second['confirmationToken']).json
    assert repeated['deletedCount'] == repeated['targetCount'] == 0 and repeated['alreadyDeletedCount'] == 2
    assert [item['revision'] for item in server[1].get('/api/publications').json['items']] == [5, 4]


@pytest.mark.parametrize('body', [None, [], 'text', True, {}, {'confirmationToken': None}, {'confirmationToken': True},
                                 {'confirmationToken': []}, {'confirmationToken': {}},
                                 {'confirmationToken': '', 'revisions': [1]}, {'confirmationToken': 'bad'},
                                 {'confirmationToken': 'a.b.c'}, {'confirmationToken': '中文'}])
def test_cleanup_rejects_malformed_body_and_token(server, body):
    seed_history(server, 2)
    response = server[1].post('/api/publications/cleanup', data=json.dumps(body),
                              content_type='application/json', headers=server[2])
    assert response.status_code == 400
    assert server[1].get('/api/publications').json['total'] == 2


def test_token_tampering_expiration_and_overlarge_requests_do_not_delete(server, monkeypatch):
    seed_history(server, 2)
    snapshot = selected(server, [1])
    token = snapshot['confirmationToken']
    altered = token[:-1] + ('0' if token[-1] != '0' else '1')
    assert cleanup(server, altered).status_code == 400
    assert cleanup(server, 'a' * (48 * 1024 + 1)).status_code == 413
    assert cleanup(server, 'a' * (64 * 1024 + 1)).status_code == 413
    assert preview(server, revisions=[1], extra='x' * (16 * 1024)).status_code == 413
    assert server[1].get('/api/publications').json['total'] == 2
    with monkeypatch.context() as patch:
        patch.setattr(module.time, 'time', lambda: snapshot['expiresAt'])
        assert cleanup(server, token).status_code == 409
        assert server[1].get('/api/publications').json['total'] == 2


@pytest.mark.parametrize('field,value', [('recorded_at', 999), ('created_at', 888), ('source', 'changed'),
                                       ('snapshot', '[{"changed":true}]'), ('cache_policy', '{"changed":true}'),
                                       ('item_count', 9), ('page_count', 10)])
def test_stable_identity_change_rejects_entire_preview_atomically(server, field, value):
    seed_history(server, 3)
    snapshot = selected(server, [1, 2])
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute('UPDATE publications SET ' + field + '=? WHERE revision=2', (value,))
    assert cleanup(server, snapshot['confirmationToken']).status_code == 409
    assert server[1].get('/api/publications/1').status_code == 200
    assert server[1].get('/api/publications').json['total'] == 3


def test_confirmation_rechecks_current_even_if_it_changed_to_previewed_revision(server):
    seed_history(server, 2)
    snapshot = selected(server, [1])
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute("UPDATE settings SET value='1' WHERE key='revision'")
    assert cleanup(server, snapshot['confirmationToken']).status_code == 409
    assert server[1].get('/api/publications/1').status_code == 200
    # Keep mode always includes current, even if a restored synthetic setting is not the maximum id.
    assert preview(server, keepLatest=1).json['revisions'] == [2]


def test_old_receipts_may_arrive_after_preview_without_invalidating_content_identity(server):
    beat(server); publish(server); publish(server)
    snapshot = selected(server, [1])
    beat(server, revision=1)
    assert detail(server, 1)['targets'][0]['appliedAt'] is not None
    assert cleanup(server, snapshot['confirmationToken']).json['targetCount'] == 1
    beat(server, revision=1)
    assert server[1].get('/api/publications/1').status_code == 404
    with server[0].extensions['lanscreen']['db']() as c:
        assert not c.execute('SELECT 1 FROM publication_targets WHERE revision=1').fetchone()


def test_cleanup_also_removes_selected_migration_backup_rows_only(server):
    beat(server); publish(server); publish(server)
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute('CREATE TABLE publication_targets_before_binding AS SELECT * FROM publication_targets')
    snapshot = selected(server, [1])
    assert snapshot['targetCount'] == 1  # Do not double-count the migration duplicate.
    result = cleanup(server, snapshot['confirmationToken'])
    assert result.json['targetCount'] == 1
    with server[0].extensions['lanscreen']['db']() as c:
        assert [row['revision'] for row in c.execute('SELECT * FROM publication_targets_before_binding')] == [2]
        assert [row['revision'] for row in c.execute('SELECT * FROM publication_targets')] == [2]


def test_cleanup_failure_rolls_back_publications_targets_and_migration_duplicate(server):
    beat(server); publish(server); publish(server)
    with server[0].extensions['lanscreen']['db']() as c:
        c.execute('CREATE TABLE publication_targets_before_binding AS SELECT * FROM publication_targets')
        c.execute("CREATE TRIGGER abort_history_delete BEFORE DELETE ON publications BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END")
    snapshot = selected(server, [1])
    assert cleanup(server, snapshot['confirmationToken']).status_code == 500
    with server[0].extensions['lanscreen']['db']() as c:
        for table in ('publications', 'publication_targets', 'publication_targets_before_binding'):
            assert c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 2


def test_real_sql_deletion_frees_sqlite_pages_and_later_metadata_reuses_them(server):
    beat(server)
    for _ in range(4):
        publish(server)
    snapshot = json.dumps([{'mediaId': 'synthetic', 'name': 'x' * (64 * 1024),
                            'kind': 'pdf', 'pageCount': 1, 'duration': 10}])
    database = server[0].extensions['lanscreen']['db']
    with database() as c:
        c.execute('UPDATE publications SET snapshot=? WHERE revision<4', (snapshot,))
        c.execute('CREATE TABLE publication_targets_before_binding AS SELECT * FROM publication_targets')
        pages_before = c.execute('PRAGMA page_count').fetchone()[0]
        free_before = c.execute('PRAGMA freelist_count').fetchone()[0]
    token = selected(server, [1, 2, 3])['confirmationToken']
    result = cleanup(server, token)
    assert result.json['deletedCount'] == result.json['targetCount'] == 3
    with database() as c:
        for table in ('publications', 'publication_targets', 'publication_targets_before_binding'):
            assert c.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 1
        reusable = c.execute('PRAGMA freelist_count').fetchone()[0]
        assert reusable > free_before  # Actual freed pages, not a hidden UI filter.
        c.executemany('INSERT INTO publications '
                      '(revision,source,created_at,recorded_at,snapshot,cache_policy,item_count,page_count) '
                      'VALUES (?,\'publish\',?,?,?,\'{}\',1,1)',
                      [(revision, float(revision), float(revision), snapshot) for revision in (5, 6, 7)])
        assert c.execute('PRAGMA freelist_count').fetchone()[0] < reusable
        assert c.execute('PRAGMA page_count').fetchone()[0] <= pages_before
    # No VACUUM or file size assertion: SQLite may keep its existing file allocation.


def test_restart_does_not_reimport_removed_old_history_and_unused_token_remains_valid(server):
    beat(server); publish(server); publish(server); publish(server)
    used = selected(server, [1])
    remaining = selected(server, [2])
    assert cleanup(server, used['confirmationToken']).status_code == 200
    restarted = module.create_app(server[0].extensions['lanscreen']['data'], testing=True)
    try:
        client = restarted.test_client()
        login = client.post('/api/login', json={'password': 'TestOnly-Password-2026'})
        context = restarted, client, {'X-CSRF-Token': login.json['csrf']}
        assert client.get('/api/publications/1').status_code == 404
        assert client.get('/api/publications').json['total'] == 2
        assert cleanup(context, used['confirmationToken']).json['alreadyDeletedCount'] == 1
        assert cleanup(context, remaining['confirmationToken']).json['deletedCount'] == 1
        assert client.get('/api/publications').json['total'] == 1
    finally:
        restarted.extensions['lanscreen']['stop']()


def test_cleanup_clamps_removed_last_page_and_empty_list_to_valid_page(server):
    seed_history(server, 5)
    assert server[1].get('/api/publications?page=3&pageSize=2').json['items'][0]['revision'] == 1
    snapshot = selected(server, [1])
    cleanup(server, snapshot['confirmationToken'])
    after = server[1].get('/api/publications?page=3&pageSize=2').json
    assert after['page'] == 2 and [item['revision'] for item in after['items']] == [3, 2]
    assert not after['hasNext']


def test_concurrent_new_publish_old_heartbeat_and_cleanup_do_not_restore_or_expand_history(server):
    beat(server); publish(server); publish(server)
    headers = approved_headers(server, 'tv-1')
    snapshot = selected(server, [1])
    other, csrf = second_admin(server)
    barrier = threading.Barrier(3)
    def do_cleanup():
        barrier.wait()
        return cleanup(server, snapshot['confirmationToken'], client=other, headers=csrf)
    def do_publish():
        barrier.wait()
        return publish(server)
    def do_beat():
        barrier.wait()
        return server[0].test_client().post('/api/player/heartbeat', headers=headers,
                                           json={'deviceId': 'tv-1', 'protocolVersion': 3, 'appliedRevision': 1})
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(do_cleanup), pool.submit(do_publish), pool.submit(do_beat)]
        results = [future.result(timeout=40) for future in futures]
    assert results[0].status_code == results[2].status_code == 200
    assert results[1]['revision'] == 3
    assert server[1].get('/api/publications/1').status_code == 404
    assert [item['revision'] for item in server[1].get('/api/publications').json['items']] == [3, 2]
    with server[0].extensions['lanscreen']['db']() as c:
        assert not c.execute('SELECT 1 FROM publication_targets WHERE revision=1').fetchone()


def test_concurrent_same_token_cleanup_is_idempotent(server):
    seed_history(server, 2)
    token = selected(server, [1])['confirmationToken']
    other, csrf = second_admin(server)
    barrier = threading.Barrier(2)
    def request(client, headers):
        barrier.wait()
        return cleanup(server, token, client=client, headers=headers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request, server[1], server[2]), pool.submit(request, other, csrf)]
        results = [future.result(timeout=40) for future in futures]
    assert all(response.status_code == 200 for response in results)
    assert sorted(response.json['deletedCount'] for response in results) == [0, 1]
    assert sorted(response.json['alreadyDeletedCount'] for response in results) == [0, 1]

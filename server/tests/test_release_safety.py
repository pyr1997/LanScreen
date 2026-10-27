"""Release regressions with retained fixtures and bounded, owned child processes."""
from concurrent.futures import ThreadPoolExecutor
import errno
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module
import conversion_runtime as runtime_module
from conversion_runtime import ConversionLimitError, directory_bytes, run_bounded

TEST_ROOT = Path(os.environ.get('LANSCREEN_TEST_ROOT', Path.cwd() / 'work' / 'release-tests'))


@pytest.fixture
def context():
    application = module.create_app(TEST_ROOT / uuid.uuid4().hex, testing=True)
    client = application.test_client()
    with client.session_transaction() as session:
        session['admin'] = True
        session['csrf'] = 'release-test-only'
    try:
        yield application, client, {'X-CSRF-Token': 'release-test-only'}
    finally:
        application.extensions['lanscreen']['stop']()


def row(context, item_id):
    with context[0].extensions['lanscreen']['db']() as c:
        return dict(c.execute('SELECT * FROM media WHERE id=?', (item_id,)).fetchone())


def seed(context, status='ready', declared=10, name='合成素材.mp4'):
    application = context[0]
    item_id = uuid.uuid4().hex
    folder = application.extensions['lanscreen']['data'] / 'media' / item_id
    folder.mkdir()
    source = folder / 'original.mp4'
    source.write_bytes(b'synthetic original')
    target = folder / 'video.mp4'
    target.write_bytes(b'0123456789')
    pages = [{'id': item_id + ':video.mp4', 'type': 'video', 'url': '/media/' + item_id + '/video.mp4',
              'sha256': '0' * 64, 'bytes': declared}]
    with application.extensions['lanscreen']['db']() as c:
        c.execute('INSERT INTO media(id,name,kind,status,pages,created,source) VALUES(?,?,?,?,?,?,?)',
                  (item_id, name, 'video', status, json.dumps(pages), time.time(), str(source)))
    return item_id, folder, target


def test_partial_upload_is_owned_visible_and_not_retryable(context, monkeypatch):
    def partial(stream, path, check_space):
        # Ownership must already have committed when the first payload byte is written.
        assert row(context, path.parent.name)['status'] == 'uploading'
        path.write_bytes(b'partial retained bytes')
        raise OSError(errno.ENOSPC, 'synthetic no space')
    monkeypatch.setattr(module, 'save_upload', partial)
    result = context[1].post('/api/media', data={'file': (io.BytesIO(b'input'), 'partial.mp4')}, headers=context[2])
    assert result.status_code == 507
    item_id = result.json['id']
    stored = row(context, item_id)
    assert stored['status'] == 'error' and stored['retryable'] == 0
    assert Path(stored['source']).read_bytes() == b'partial retained bytes'
    visible = next(item for item in context[1].get('/api/dashboard').json['media'] if item['id'] == item_id)
    assert visible['retryAllowed'] is False
    assert context[1].post('/api/media/' + item_id + '/retry', headers=context[2]).status_code == 400
    preview = context[1].get('/api/media/' + item_id + '/deletion-preview').json
    assert preview['allowed'] and preview['fileCount'] == 1


def test_interrupted_upload_recovers_on_restart_without_erasing(context, monkeypatch):
    def interrupted(stream, path, check_space):
        path.write_bytes(b'interrupted bytes retained')
        raise SystemExit('synthetic process interruption')
    monkeypatch.setattr(module, 'save_upload', interrupted)
    with pytest.raises(SystemExit):
        context[1].post('/api/media', data={'file': (io.BytesIO(b'input'), 'partial.mp4')}, headers=context[2])
    with context[0].extensions['lanscreen']['db']() as c:
        stored = dict(c.execute('SELECT * FROM media').fetchone())
    assert stored['status'] == 'uploading'
    restarted = module.create_app(context[0].extensions['lanscreen']['data'], testing=True)
    try:
        with restarted.extensions['lanscreen']['db']() as c:
            recovered = dict(c.execute('SELECT * FROM media').fetchone())
        assert recovered['status'] == 'error' and not recovered['retryable']
        assert Path(stored['source']).read_bytes() == b'interrupted bytes retained'
    finally:
        restarted.extensions['lanscreen']['stop']()


def test_unregistered_uuid_directory_is_imported_once_and_not_retryable(context):
    directory = context[0].extensions['lanscreen']['data']
    item_id = uuid.uuid4().hex
    folder = directory / 'media' / item_id
    folder.mkdir()
    original = folder / 'original.pptx'
    original.write_bytes(b'unknown completeness retained')
    for _ in range(2):
        restarted = module.create_app(directory, testing=True)
        try:
            with restarted.extensions['lanscreen']['db']() as c:
                rows = c.execute('SELECT * FROM media WHERE id=?', (item_id,)).fetchall()
            assert len(rows) == 1 and rows[0]['status'] == 'error' and not rows[0]['retryable']
        finally:
            restarted.extensions['lanscreen']['stop']()
    assert original.read_bytes() == b'unknown completeness retained'


def test_uploading_blocks_deletion_and_uses_queue_capacity(context):
    item_id, _, _ = seed(context, status='uploading')
    assert not context[1].get('/api/media/' + item_id + '/deletion-preview').json['allowed']
    runtime = context[1].get('/api/dashboard').json['runtime']
    assert runtime['uploadingCount'] == 1 and runtime['queueLimit'] == 20


def encrypted_pptx():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('ppt/presentation.xml', '<Presentation/>')
        archive.writestr('ppt/_rels/presentation.xml.rels', '<Relationships/>')
    raw = bytearray(buffer.getvalue())
    offset = 0
    while True:
        offset = raw.find(b'PK\x01\x02', offset)
        if offset < 0:
            break
        length = struct.unpack_from('<H', raw, offset + 28)[0]
        if bytes(raw[offset + 46:offset + 46 + length]).endswith(b'.rels'):
            struct.pack_into('<H', raw, offset + 8, 1)
        offset += 46 + length
    return raw


def test_encrypted_pptx_is_visible_validation_failure(context):
    response = context[1].post('/api/media', data={'file': (io.BytesIO(encrypted_pptx()), 'encrypted.pptx')}, headers=context[2])
    assert response.status_code == 400 and '加密' in response.json['error']
    stored = row(context, response.json['id'])
    assert stored['status'] == 'error' and not stored['retryable']
    assert Path(stored['source']).is_file()


def test_retry_shares_twenty_task_limit(context):
    for _ in range(20):
        seed(context, status='queued')
    item_id, _, _ = seed(context, status='error')
    response = context[1].post('/api/media/' + item_id + '/retry', headers=context[2])
    assert response.status_code == 429 and row(context, item_id)['status'] == 'error'
    upload = context[1].post('/api/media', data={'file': (io.BytesIO(b'test'), 'new.png')}, headers=context[2])
    assert upload.status_code == 429


def test_retry_during_finishing_future_is_really_rescheduled(context):
    item_id, _, _ = seed(context, status='queued')
    after_error = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    calls = []
    def converter(key):
        calls.append(key)
        with context[0].extensions['lanscreen']['db']() as c:
            c.execute('UPDATE media SET status=? WHERE id=?', ('error' if len(calls) == 1 else 'ready', key))
        if len(calls) == 1:
            after_error.set()
            assert release_first.wait(10)
        else:
            second_finished.set()
    context[0].extensions['lanscreen']['convert'] = converter
    try:
        context[0].extensions['lanscreen']['schedule'](item_id)
        assert after_error.wait(5)
        response = context[1].post('/api/media/' + item_id + '/retry', headers=context[2])
        assert response.status_code == 200 and row(context, item_id)['status'] == 'queued'
        release_first.set()
        assert second_finished.wait(5)
        assert calls == [item_id, item_id] and row(context, item_id)['status'] == 'ready'
    finally:
        release_first.set()


def test_parallel_uploads_atomically_admit_only_one_last_slot(context):
    for _ in range(19):
        seed(context, status='queued')
    release = threading.Event()
    blocker = context[0].extensions['lanscreen']['executor'].submit(lambda: release.wait(10))
    gate = threading.Barrier(2)
    def upload():
        client = context[0].test_client()
        with client.session_transaction() as session:
            session['admin'] = True
            session['csrf'] = 'release-test-only'
        gate.wait(timeout=5)
        return client.post('/api/media', data={'file': (io.BytesIO(b'synthetic'), 'new.png')}, headers=context[2]).status_code
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix='release-admission') as pool:
            futures = [pool.submit(upload) for _ in range(2)]
            assert sorted(f.result(timeout=10) for f in futures) == [202, 429]
        with context[0].extensions['lanscreen']['db']() as c:
            assert c.execute("SELECT COUNT(*) FROM media WHERE status IN ('uploading','queued','processing')").fetchone()[0] == 20
    finally:
        release.set()
        blocker.result(timeout=10)


def test_restart_retains_overflow_as_visible_errors(context):
    for _ in range(24):
        seed(context, status='queued')
    restarted = module.create_app(context[0].extensions['lanscreen']['data'], testing=True)
    try:
        with restarted.extensions['lanscreen']['db']() as c:
            counts = dict(c.execute('SELECT status,COUNT(*) FROM media GROUP BY status').fetchall())
        assert counts == {'queued': 20, 'error': 4}
    finally:
        restarted.extensions['lanscreen']['stop']()


def test_publish_rejects_oversized_metadata_without_advancing_revision(context):
    item_id, _, _ = seed(context, declared=module.MAX_MEDIA_BYTES + 1)
    response = context[1].post('/api/publish', json={'items': [{'mediaId': item_id, 'duration': 10}]}, headers=context[2])
    assert response.status_code == 400 and '1 GiB' in response.json['error']
    assert context[0].extensions['lanscreen']['setting']('revision') == 0


def test_publish_checks_real_file_not_only_small_metadata(context, monkeypatch):
    item_id, _, target = seed(context)
    real_stat = Path.stat
    def oversized(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_size=module.MAX_MEDIA_BYTES + 1, st_mode=result.st_mode)
        return result
    monkeypatch.setattr(Path, 'stat', oversized)
    response = context[1].post('/api/publish', json={'items': [{'mediaId': item_id, 'duration': 10}]}, headers=context[2])
    assert response.status_code == 400 and '1 GiB' in response.json['error']


def test_video_conversion_oversize_cannot_become_ready(context, monkeypatch):
    item_id, _, target = seed(context, status='queued')
    def fake_run(command, **kwargs):
        target.write_bytes(b'\x00\x00\x00\x18ftyp' + b'0' * 256)
        return subprocess.CompletedProcess(command, 0, b'', b'')
    monkeypatch.setattr(module, 'run_bounded', fake_run)
    monkeypatch.setattr(module, 'find_ffmpeg', lambda: 'synthetic-ffmpeg-never-started')
    real_stat = Path.stat
    def oversized(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_size=module.MAX_MEDIA_BYTES + 1, st_mode=result.st_mode)
        return result
    monkeypatch.setattr(Path, 'stat', oversized)
    context[0].extensions['lanscreen']['convert'](item_id)
    assert row(context, item_id)['status'] == 'error'
    assert '1 GiB' in row(context, item_id)['error']


def test_exact_size_limit_allowed_without_reading_gigabyte(context, monkeypatch):
    item_id, _, target = seed(context, declared=module.MAX_MEDIA_BYTES)
    real_stat = Path.stat
    def maximum(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_size=module.MAX_MEDIA_BYTES, st_mode=result.st_mode)
        return result
    monkeypatch.setattr(Path, 'stat', maximum)
    response = context[1].post('/api/publish', json={'items': [{'mediaId': item_id, 'duration': 10}]}, headers=context[2])
    assert response.status_code == 200


@pytest.mark.parametrize('bad_pages', [[], [{'bytes': -1}], [{'bytes': '10'}], [{'bytes': True}], [{'bytes': None}], ['invalid']])
def test_publish_rejects_invalid_legacy_metadata(context, bad_pages):
    item_id, _, _ = seed(context)
    with context[0].extensions['lanscreen']['db']() as c:
        c.execute('UPDATE media SET pages=? WHERE id=?', (json.dumps(bad_pages), item_id))
    response = context[1].post('/api/publish', json={'items': [{'mediaId': item_id, 'duration': 10}]}, headers=context[2])
    assert response.status_code == 400


def test_legacy_missing_bytes_uses_actual_file(context):
    item_id, _, _ = seed(context)
    with context[0].extensions['lanscreen']['db']() as c:
        pages = json.loads(c.execute('SELECT pages FROM media WHERE id=?', (item_id,)).fetchone()[0])
        pages[0].pop('bytes')
        c.execute('UPDATE media SET pages=? WHERE id=?', (json.dumps(pages), item_id))
    response = context[1].post('/api/publish', json={'items': [{'mediaId': item_id, 'duration': 10}]}, headers=context[2])
    assert response.status_code == 200
    assert context[1].get('/api/preview/manifest').json['items'][0]['bytes'] == 10


def test_low_space_rejects_new_work_but_keeps_deletion_preview(context, monkeypatch):
    item_id, _, _ = seed(context, status='error')
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: SimpleNamespace(free=module.SERVER_RESERVE_BYTES - 1))
    assert context[1].post('/api/media', data={'file': (io.BytesIO(b'x'), 'new.png')}, headers=context[2]).status_code == 507
    assert context[1].post('/api/media/' + item_id + '/retry', headers=context[2]).status_code == 507
    preview = context[1].get('/api/media/' + item_id + '/deletion-preview')
    assert preview.status_code == 200 and preview.json['allowed']
    assert context[1].get('/api/health').status_code == 503
    assert context[1].get('/api/live').status_code == 200


def test_converter_checks_space_before_launch_and_output_budget(context, monkeypatch):
    _, folder, _ = seed(context)
    command = context[0].extensions['lanscreen']['conversion_command']
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: SimpleNamespace(free=module.SERVER_RESERVE_BYTES - 1))
    with pytest.raises(ConversionLimitError, match='1 GiB'):
        command(['unstarted-synthetic-command'], folder / 'original.mp4', folder, 1)
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10 * 1024 ** 3))
    monkeypatch.setattr(module, 'directory_bytes', lambda *args, **kwargs: module.CONVERSION_OUTPUT_BUDGET + 1)
    with pytest.raises(ConversionLimitError, match='2 GiB'):
        command(['unstarted-synthetic-command'], folder / 'original.mp4', folder, 1)


def test_database_failure_changes_health_to_503_without_leaking_error(context, monkeypatch):
    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError('SYNTHETIC_PRIVATE_DETAIL')
    monkeypatch.setattr(module.sqlite3, 'connect', fail_connect)
    health = context[1].get('/api/health')
    assert health.status_code == 503 and not health.json['checks']['database']
    assert 'SYNTHETIC_PRIVATE_DETAIL' not in health.get_data(as_text=True)
    assert context[1].get('/api/live').status_code == 200


def test_unwritable_data_is_not_ready(context, monkeypatch):
    monkeypatch.setattr(module.os, 'access', lambda *args, **kwargs: False)
    response = context[1].get('/api/health')
    assert response.status_code == 503
    assert not response.json['checks']['storage'] and not response.json['checks']['database']


def test_stuck_worker_is_not_ready(context):
    state = context[0].extensions['lanscreen']['worker_state']
    state.update(active='synthetic-stuck-job', started=time.monotonic() - module.WORKER_MAX_SECONDS - 1)
    try:
        response = context[1].get('/api/health')
        assert response.status_code == 503 and not response.json['checks']['worker']
    finally:
        state.update(active=None, started=0.0)


def test_directory_budget_excludes_original_and_does_not_follow_links(context):
    _, folder, target = seed(context)
    before = directory_bytes(folder, exclude=folder / 'original.mp4')
    assert before == target.stat().st_size
    (folder / 'unfollowed-link').symlink_to(context[0].extensions['lanscreen']['data'], target_is_directory=True)
    assert directory_bytes(folder, exclude=folder / 'original.mp4') == before


def test_budget_scan_tolerates_disappearing_temporary_entries_without_deleting(context, monkeypatch):
    _, folder, target = seed(context)
    real_stat = runtime_module.os.stat
    def vanished(path, *args, **kwargs):
        if path == target.name and kwargs.get('dir_fd') is not None:
            raise FileNotFoundError(errno.ENOENT, 'synthetic temporary file disappeared')
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(runtime_module.os, 'stat', vanished)
    assert directory_bytes(folder, exclude=folder / 'original.mp4') == 0
    assert target.read_bytes() == b'0123456789'
    with pytest.raises(FileNotFoundError):
        directory_bytes(folder / 'missing-root')


def process_gone(pid):
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def test_runner_caps_both_log_buffers_and_exits():
    result = run_bounded([sys.executable, '-B', '-c',
                          "import os; os.write(1,b'a'*262144); os.write(2,b'b'*262144)"],
                         timeout=5, check_budget=lambda: None, stdout_limit=1024, stderr_limit=2048)
    assert result.returncode == 0 and len(result.stdout) == 1024 and len(result.stderr) == 2048


def test_timeout_reaps_owned_parent_and_child():
    folder = TEST_ROOT / ('process-timeout-' + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    ledger = folder / 'owned-pids.json'
    source = ("import subprocess,sys,signal,os,time,json; from pathlib import Path\n"
              "child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)'])\n"
              "def stop(signum,frame):\n"
              " child.wait(timeout=3); sys.exit(0)\n"
              "signal.signal(signal.SIGTERM,stop)\n"
              "Path(sys.argv[1]).write_text(json.dumps({'parent':os.getpid(),'child':child.pid}))\n"
              "time.sleep(30)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, '-B', '-c', source, str(ledger)], timeout=0.75, check_budget=lambda: None)
    pids = json.loads(ledger.read_text())
    assert all(process_gone(pid) for pid in pids.values())
    (folder / 'verified-exited.json').write_text(json.dumps({'pids': pids, 'allExited': True}))


def test_inflight_budget_failure_stops_owned_process():
    folder = TEST_ROOT / ('process-budget-' + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    ledger = folder / 'owned-pid.txt'
    checks = 0
    def budget():
        nonlocal checks
        checks += 1
        if ledger.exists():
            raise ConversionLimitError('synthetic low disk while converting')
    with pytest.raises(ConversionLimitError, match='synthetic low disk'):
        run_bounded([sys.executable, '-B', '-c',
                     'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)',
                     str(ledger)], timeout=5, check_budget=budget)
    pid = int(ledger.read_text())
    assert process_gone(pid) and checks >= 2
    (folder / 'verified-exited.json').write_text(json.dumps({'pid': pid, 'exited': True}))


def test_timeout_escalates_for_child_ignoring_termination():
    folder = TEST_ROOT / ('process-force-stop-' + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    ledger = folder / 'owned-pid.txt'
    source = ("import signal,os,sys,time; from pathlib import Path; "
              "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
              "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)")
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, '-B', '-c', source, str(ledger)], timeout=0.5, check_budget=lambda: None)
    pid = int(ledger.read_text())
    assert process_gone(pid)
    (folder / 'verified-exited.json').write_text(json.dumps({'pid': pid, 'exited': True, 'ignoredTerm': True}))


def test_service_stop_cancels_active_owned_conversion(context):
    item_id, folder, _ = seed(context, status='queued')
    ledger = folder / 'owned-pid.txt'
    source = 'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)'
    command = context[0].extensions['lanscreen']['conversion_command']
    context[0].extensions['lanscreen']['convert'] = lambda key: command(
        [sys.executable, '-B', '-c', source, str(ledger)], folder / 'original.mp4', folder, 30)
    context[0].extensions['lanscreen']['schedule'](item_id)
    deadline = time.monotonic() + 5
    while not ledger.exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    assert ledger.exists()
    context[0].extensions['lanscreen']['stop']()
    pid = int(ledger.read_text())
    assert process_gone(pid)
    assert context[1].get('/api/health').status_code == 503
    (folder / 'verified-exited.json').write_text(json.dumps({'pid': pid, 'exited': True, 'serviceStopped': True}))

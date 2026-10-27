"""Shutdown-only recovery: real conversion lifecycle, retained data and owned children."""
from pathlib import Path
import json
import os
import subprocess
import sys
import threading
import time
import uuid

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as module
import conversion_runtime as runtime

TEST_ROOT = Path(os.environ.get('LANSCREEN_TEST_ROOT', Path.cwd() / 'work' / 'shutdown-tests'))


def gone(pid):
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def wait_until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    assert predicate(), 'bounded lifecycle condition did not complete'


@pytest.fixture
def lifecycle(monkeypatch):
    evidence = TEST_ROOT / ('shutdown-' + uuid.uuid4().hex)
    evidence.mkdir(parents=True)
    applications, children = [], []
    popen = runtime.subprocess.Popen

    def tracked_popen(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(runtime.subprocess, 'Popen', tracked_popen)

    def create(directory=None, testing=True):
        application = module.create_app(directory or evidence / 'data', testing=testing)
        applications.append(application)
        return application

    try:
        yield create, evidence
    finally:
        for application in reversed(applications):
            application.extensions['lanscreen']['stop']()
        states = [{'pid': child.pid, 'returncode': child.poll(), 'exited': gone(child.pid)} for child in children]
        (evidence / 'owned-processes.json').write_text(json.dumps(states, indent=2))
        assert all(item['returncode'] is not None and item['exited'] for item in states)


def seed(application, status='queued', created=None):
    item = uuid.uuid4().hex
    directory = application.extensions['lanscreen']['data'] / 'media' / item
    directory.mkdir()
    source = directory / 'original.png'
    Image.new('RGB', (32, 24), '#376ab7').save(source)
    with application.extensions['lanscreen']['db']() as c:
        c.execute('INSERT INTO media(id,name,kind,status,created,source) VALUES(?,?,?,?,?,?)',
                  (item, 'Synthetic.png', 'image', status, time.time() if created is None else created, str(source)))
    return item, source


def row(application, item):
    with application.extensions['lanscreen']['db']() as c:
        return dict(c.execute('SELECT * FROM media WHERE id=?', (item,)).fetchone())


def admin_client(application):
    client = application.test_client()
    with client.session_transaction() as session:
        session['admin'] = True
        session['csrf'] = 'synthetic-shutdown-only'
    return client, {'X-CSRF-Token': 'synthetic-shutdown-only'}


def test_real_convert_stop_restart_recovers_active_and_queued_with_owned_children(lifecycle, monkeypatch):
    create, evidence = lifecycle
    application = create()
    items = [seed(application) for _ in range(3)]
    originals = {str(source): source.read_bytes() for _, source in items}
    retained_output = items[0][1].parent / 'interrupted-output.bin'
    retained_output.write_bytes(b'partial conversion evidence is retained')
    ledger = evidence / 'interrupted-parent-and-child.json'
    source = (
        "import subprocess,sys,signal,os,time,json; from pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-B','-c','import time; time.sleep(30)'])\n"
        "def stop(signum,frame):\n"
        " child.wait(timeout=3); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "Path(sys.argv[1]).write_text(json.dumps({'parent':os.getpid(),'child':child.pid}))\n"
        "time.sleep(30)\n")
    real_runner = module.run_bounded
    calls = []

    def first_slow_then_real(command, **options):
        calls.append(list(command))
        if len(calls) == 1:
            command = [sys.executable, '-B', '-c', source, str(ledger)]
        return real_runner(command, **options)

    monkeypatch.setattr(module, 'run_bounded', first_slow_then_real)
    for item, _ in items:
        assert application.extensions['lanscreen']['schedule'](item)
    wait_until(ledger.exists)
    assert row(application, items[0][0])['status'] == 'processing'
    application.extensions['lanscreen']['stop']()
    pids = json.loads(ledger.read_text())
    assert all(gone(pid) for pid in pids.values())
    assert len(calls) == 1  # Executor cancellation must not start the two queued jobs.
    for item, _ in items:
        assert row(application, item)['status'] == 'queued'
    assert '下次启动将自动恢复' in row(application, items[0][0])['error']
    assert application.extensions['lanscreen']['worker_state']['active'] is None
    assert application.extensions['lanscreen']['worker_state']['failed'] is False
    assert application.test_client().get('/api/health').status_code == 503
    assert application.extensions['lanscreen']['schedule'](items[0][0]) is False

    restarted = create(application.extensions['lanscreen']['data'], testing=False)
    wait_until(lambda: all(row(restarted, item)['status'] == 'ready' for item, _ in items))
    assert len(calls) == 4  # Real render_worker.py executes for all three valid PNG inputs.
    for item, _ in items:
        stored = row(restarted, item)
        assert not stored['error']
        for page in json.loads(stored['pages']):
            artifact = Path(stored['source']).parent / page['url'].rsplit('/', 1)[1]
            assert artifact.is_file() and artifact.stat().st_size == page['bytes'] > 0
    assert all(Path(name).read_bytes() == content for name, content in originals.items())
    assert retained_output.read_bytes() == b'partial conversion evidence is retained'
    (evidence / 'restart-verification.json').write_text(json.dumps({
        'pids': pids, 'allInterruptedProcessesExited': True,
        'items': [item for item, _ in items], 'readyAfterRestart': True,
        'originalsAndRetainedOutputUnchanged': True}, indent=2))


@pytest.mark.parametrize('failure', ['invalid', 'limit', 'timeout', 'reaping'])
@pytest.mark.parametrize('stop_when', ['after_error', 'during_error'])
def test_ordinary_conversion_errors_never_become_auto_retry_on_stop_restart(lifecycle, monkeypatch, failure, stop_when):
    create, _ = lifecycle
    application = create()
    item, source = seed(application)
    original = source.read_bytes()
    errors = {'invalid': ValueError('synthetic invalid source'),
              'limit': runtime.ConversionLimitError('synthetic output budget exceeded'),
              'timeout': subprocess.TimeoutExpired(['synthetic'], 1),
              'reaping': RuntimeError('synthetic child group cleanup failed')}
    calls = []

    def fail(command, **options):
        calls.append(command)
        if stop_when == 'during_error':
            # Direct convert has no executor Future, so this is a real stop without
            # trying to join the current worker thread. The ordinary error still wins.
            application.extensions['lanscreen']['stop']()
        raise errors[failure]

    monkeypatch.setattr(module, 'run_bounded', fail)
    application.extensions['lanscreen']['convert'](item)
    failed = row(application, item)
    assert failed['status'] == 'error' and failed['retryable'] == 1
    application.extensions['lanscreen']['stop']()
    restarted = create(application.extensions['lanscreen']['data'], testing=False)
    assert row(restarted, item)['status'] == 'error'
    assert row(restarted, item)['error'] == failed['error']
    assert calls and len(calls) == 1 and source.read_bytes() == original
    assert restarted.test_client().get('/api/health').status_code == 200


def test_shutdown_reaping_failure_overrides_interruption_and_stays_error(lifecycle, monkeypatch):
    create, evidence = lifecycle
    application = create()
    item, source = seed(application)
    original = source.read_bytes()
    ledger = evidence / 'reaping-failure-owned-pid.txt'
    runner, stop_group = module.run_bounded, runtime._stop_group
    command = [sys.executable, '-B', '-c',
               'import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)',
               str(ledger)]

    def slow(ignored_command, **options):
        return runner(command, **options)

    def cleanup_failure(process, *args, **kwargs):
        stop_group(process, *args, **kwargs)  # Actually reap the owned child first.
        raise RuntimeError('synthetic reaping failure after safe process cleanup')

    monkeypatch.setattr(module, 'run_bounded', slow)
    monkeypatch.setattr(runtime, '_stop_group', cleanup_failure)
    assert application.extensions['lanscreen']['schedule'](item)
    wait_until(ledger.exists)
    application.extensions['lanscreen']['stop']()
    assert gone(int(ledger.read_text()))
    assert row(application, item)['status'] == 'error'
    assert source.read_bytes() == original
    restarted = create(application.extensions['lanscreen']['data'], testing=False)
    assert row(restarted, item)['status'] == 'error'
    assert restarted.extensions['lanscreen']['worker_state']['active'] is None


def test_stop_during_artifact_hash_leaves_queue_and_preserves_files(lifecycle, monkeypatch):
    create, _ = lifecycle
    application = create()
    item, source = seed(application)
    original = source.read_bytes()
    sha256 = module.hashlib.sha256
    updates = []

    class InterruptingHash:
        def __init__(self, *args, **kwargs):
            self.digest = sha256(*args, **kwargs)

        def update(self, chunk):
            updates.append(len(chunk))
            application.extensions['lanscreen']['stop']()
            self.digest.update(chunk)

        def hexdigest(self):
            return self.digest.hexdigest()

    monkeypatch.setattr(module.hashlib, 'sha256', InterruptingHash)
    application.extensions['lanscreen']['convert'](item)
    assert updates and row(application, item)['status'] == 'queued'
    assert source.read_bytes() == original
    assert any(path.suffix == '.jpg' for path in source.parent.iterdir())


def test_stop_after_completed_conversion_does_not_downgrade_ready(lifecycle):
    create, _ = lifecycle
    application = create()
    item, source = seed(application)
    application.extensions['lanscreen']['convert'](item)
    complete = row(application, item)
    assert complete['status'] == 'ready' and json.loads(complete['pages'])
    before = {path.name: path.read_bytes() for path in source.parent.iterdir() if path.is_file()}
    application.extensions['lanscreen']['stop']()
    assert row(application, item) == complete
    restarted = create(application.extensions['lanscreen']['data'], testing=False)
    assert row(restarted, item) == complete
    assert {path.name: path.read_bytes() for path in source.parent.iterdir() if path.is_file()} == before


def test_stopping_before_claim_leaves_queued_and_ordinary_errors_unchanged(lifecycle, monkeypatch):
    create, _ = lifecycle
    application = create()
    item, _ = seed(application)
    failed, _ = seed(application, 'error')
    application.extensions['lanscreen']['stop']()
    monkeypatch.setattr(module, 'run_bounded', lambda *args, **kwargs: pytest.fail('stopped service started conversion'))
    application.extensions['lanscreen']['convert'](item)
    application.extensions['lanscreen']['convert'](failed)
    assert row(application, item)['status'] == 'queued'
    assert row(application, failed)['status'] == 'error'


def test_recovered_queued_and_processing_share_twenty_slot_admission(lifecycle):
    create, _ = lifecycle
    application = create()
    items = [seed(application, 'queued' if index % 2 else 'processing', created=index) for index in range(23)]
    ordinary, _ = seed(application, 'error')
    application.extensions['lanscreen']['stop']()
    restarted = create(application.extensions['lanscreen']['data'])
    assert all(row(restarted, item)['status'] == 'queued' for item, _ in items[:20])
    assert all(row(restarted, item)['status'] == 'error' for item, _ in items[20:])
    assert row(restarted, ordinary)['status'] == 'error'
    client, headers = admin_client(restarted)
    assert client.post('/api/media/' + ordinary + '/retry', headers=headers).status_code == 429
    assert row(restarted, ordinary)['status'] == 'error'
    assert all(source.is_file() for _, source in items)


def test_stop_during_finishing_retry_future_keeps_retry_queued_until_restart(lifecycle):
    create, _ = lifecycle
    application = create()
    item, source = seed(application)
    after_error, release = threading.Event(), threading.Event()
    calls = []

    def finishing_converter(key):
        calls.append(key)
        with application.extensions['lanscreen']['db']() as c:
            c.execute("UPDATE media SET status='error' WHERE id=?", (key,))
        after_error.set()
        assert release.wait(10)

    application.extensions['lanscreen']['convert'] = finishing_converter
    application.extensions['lanscreen']['schedule'](item)
    wait_until(after_error.is_set)
    client, headers = admin_client(application)
    assert client.post('/api/media/' + item + '/retry', headers=headers).status_code == 200
    stopper = threading.Thread(target=application.extensions['lanscreen']['stop'], name='synthetic-converter-stop')
    stopper.start()
    try:
        wait_until(lambda: application.extensions['lanscreen']['worker_state']['closed'])
        release.set()
        stopper.join(timeout=10)
        assert not stopper.is_alive() and calls == [item]
        assert row(application, item)['status'] == 'queued'
    finally:
        release.set()
        stopper.join(timeout=10)
    restarted = create(application.extensions['lanscreen']['data'], testing=False)
    wait_until(lambda: row(restarted, item)['status'] == 'ready')
    assert source.is_file()

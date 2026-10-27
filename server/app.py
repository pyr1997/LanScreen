"""LanScreen: local media library, static document rendering and published playlists."""
from __future__ import annotations
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from xml.etree import ElementTree
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from urllib.parse import urlsplit

import fitz
from PIL import Image, ImageOps
from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from conversion_runtime import ConversionLimitError, directory_bytes, run_bounded

ROOT = Path(__file__).resolve().parent
Image.MAX_IMAGE_PIXELS = 50_000_000
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.pdf', '.ppt', '.pptx', '.mp4', '.m4v', '.mov', '.mkv', '.avi', '.webm'}
DEFAULT_CACHE_POLICY = {'maxBytes': 2 * 1024 ** 3, 'reserveBytes': 256 * 1024 ** 2}
MAX_MEDIA_BYTES = 1024 ** 3
SERVER_RESERVE_BYTES = 1024 ** 3
CONVERSION_OUTPUT_BUDGET = 2 * 1024 ** 3
QUEUE_LIMIT = 20
WORKER_MAX_SECONDS = 1845


class ConversionInterrupted(Exception):
    """Cooperative service shutdown, distinct from invalid input or resource limits."""


def save_upload(stream, source, check_space):
    """Write only after ownership has committed, retaining any interrupted partial file."""
    written = 0
    with source.open('xb') as output:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > 512 * 1024 * 1024:
                raise ValueError('文件超过 512 MiB 上限，请拆分后重新上传。')
            check_space(len(chunk))
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    check_space()
    return written


def _remove_owned_entry(name, directory_fd, is_directory):
    """Remove an entry only relative to its already opened, checked parent."""
    if is_directory:
        os.rmdir(name, dir_fd=directory_fd)
    else:
        os.unlink(name, dir_fd=directory_fd)


def find_office():
    configured = os.environ.get('LIBREOFFICE_BIN')
    if configured and Path(configured).is_file():
        return configured
    for candidate in ('/Applications/LibreOffice.app/Contents/MacOS/soffice', '/usr/bin/libreoffice'):
        if Path(candidate).is_file():
            return candidate
    return shutil.which('soffice') or shutil.which('libreoffice')


def find_ffmpeg():
    executable = shutil.which('ffmpeg')
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


def create_app(data_dir=None, testing=False):
    app = Flask(__name__, static_folder='web', static_url_path='/static')
    data = Path(data_dir or os.environ.get('LANSCREEN_DATA', ROOT / 'data')).resolve()
    data.mkdir(parents=True, exist_ok=True)
    media_root = data / 'media'
    media_root.mkdir(exist_ok=True)
    secret_file = data / 'session.key'
    if not secret_file.exists():
        with secret_file.open('x') as f:
            f.write(secrets.token_hex(32))
        secret_file.chmod(0o600)
    app.config.update(SECRET_KEY=secret_file.read_text().strip(), MAX_CONTENT_LENGTH=512 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      PERMANENT_SESSION_LIFETIME=3600 * 12, TESTING=testing)
    dbfile = data / 'library.sqlite3'
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='media-converter')
    rate_lock = threading.Lock()
    attempts = {}
    worker_lock = threading.Lock()
    worker_state = {'active': None, 'started': 0.0, 'failed': False, 'closed': False}
    conversion_futures = {}
    conversion_reruns = set()
    stopping = threading.Event()

    @contextmanager
    def db(timeout=30):
        conn = sqlite3.connect(dbfile, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            with conn:
                yield conn
        finally:
            conn.close()

    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
          size INTEGER, status TEXT NOT NULL, pages TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
          created REAL, archived INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT, version TEXT, status TEXT,
          current_item TEXT, last_seen REAL);
        CREATE TABLE IF NOT EXISTS publications (
          revision INTEGER PRIMARY KEY, source TEXT NOT NULL, created_at REAL, recorded_at REAL NOT NULL,
          snapshot TEXT NOT NULL, cache_policy TEXT NOT NULL,
          item_count INTEGER NOT NULL, page_count INTEGER NOT NULL,
          superseded_at REAL, superseded_by_revision INTEGER);
        CREATE TABLE IF NOT EXISTS publication_targets (
          revision INTEGER NOT NULL, device_id TEXT NOT NULL, device_name TEXT NOT NULL,
          joined_at REAL NOT NULL, joined_later INTEGER NOT NULL DEFAULT 0,
          downloaded_at REAL, applied_at REAL,
          binding_version INTEGER NOT NULL DEFAULT 0, withdrawn_at REAL, withdraw_reason TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (revision, device_id, binding_version));
        CREATE INDEX IF NOT EXISTS publication_targets_device ON publication_targets (device_id, revision);
        CREATE TABLE IF NOT EXISTS revoked_device_tokens (
          device_id TEXT NOT NULL, token_hash TEXT NOT NULL, binding_version INTEGER NOT NULL,
          revoked_at REAL NOT NULL, PRIMARY KEY(device_id,token_hash));

        ''')
        # Keep the rename/create/copy and all binding columns in one migration transaction.
        c.execute('BEGIN IMMEDIATE')
        columns = {row['name'] for row in c.execute('PRAGMA table_info(devices)')}
        additions = {
            'protocolVersion': 'INTEGER NOT NULL DEFAULT 1',
            'appliedRevision': 'INTEGER NOT NULL DEFAULT -1',
            'downloadedRevision': 'INTEGER NOT NULL DEFAULT -1',
            'cleanupRevision': 'INTEGER NOT NULL DEFAULT -1',
            'cacheBytes': 'INTEGER NOT NULL DEFAULT -1',
            'freeBytes': 'INTEGER NOT NULL DEFAULT -1',
            'cacheLimitBytes': 'INTEGER NOT NULL DEFAULT -1',
            'cacheReserveBytes': 'INTEGER NOT NULL DEFAULT -1',
            'cleanupState': "TEXT NOT NULL DEFAULT 'pending'",
            'cleanupError': "TEXT NOT NULL DEFAULT ''",
            'cleanupRequest': 'INTEGER NOT NULL DEFAULT 0',
            'cleanupCompletedRequest': 'INTEGER NOT NULL DEFAULT 0',
            'binding_state': "TEXT NOT NULL DEFAULT 'legacy'",
            'binding_version': 'INTEGER NOT NULL DEFAULT 0',
            'device_token_hash': "TEXT NOT NULL DEFAULT ''",
            'unbound_at': 'REAL',
        }
        for name, declaration in additions.items():
            if name not in columns:
                c.execute(f'ALTER TABLE devices ADD COLUMN {name} {declaration}')
        target_columns = {row['name'] for row in c.execute('PRAGMA table_info(publication_targets)')}
        if 'binding_version' not in target_columns:
            # Preserve the pre-binding table as a migration snapshot; do not discard history.
            c.execute('ALTER TABLE publication_targets RENAME TO publication_targets_before_binding')
            c.execute('CREATE TABLE publication_targets ('
                      'revision INTEGER NOT NULL,device_id TEXT NOT NULL,device_name TEXT NOT NULL,'
                      'joined_at REAL NOT NULL,joined_later INTEGER NOT NULL DEFAULT 0,'
                      'downloaded_at REAL,applied_at REAL,binding_version INTEGER NOT NULL DEFAULT 0,'
                      "withdrawn_at REAL,withdraw_reason TEXT NOT NULL DEFAULT '',"
                      'PRIMARY KEY(revision,device_id,binding_version))')
            c.execute('INSERT INTO publication_targets '
                      '(revision,device_id,device_name,joined_at,joined_later,downloaded_at,applied_at) '
                      'SELECT revision,device_id,device_name,joined_at,joined_later,downloaded_at,applied_at '
                      'FROM publication_targets_before_binding')
        c.execute('CREATE INDEX IF NOT EXISTS publication_targets_binding_device '
                  'ON publication_targets(device_id,binding_version,revision)')
        media_columns = {row['name'] for row in c.execute('PRAGMA table_info(media)')}
        if 'retryable' not in media_columns:
            c.execute('ALTER TABLE media ADD COLUMN retryable INTEGER NOT NULL DEFAULT 1')
        for k, v in {'display_token': secrets.token_urlsafe(24), 'revision': 0,
                     'draft': [], 'published': [], 'cache_policy': DEFAULT_CACHE_POLICY,
                     'setup_code': secrets.token_hex(4).upper()}.items():
            c.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (k, json.dumps(v)))
        c.execute("UPDATE media SET status='delete_error', error=? WHERE status='deleting'",
                  ('上次删除被中断；请重新预览剩余文件并确认删除。',))
        c.execute("UPDATE media SET status='error',retryable=0,error=? WHERE status='uploading'",
                  ('上次上传被中断，保留文件尚未通过完整性检查。请重新上传，或预览后确认删除。',))

    # Restore management ownership, never erase unknown/partially uploaded directories.
    # Only ordinary UUID directories inside this application's media root are eligible.
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        known = {row['id'] for row in c.execute('SELECT id FROM media')}
        with os.scandir(media_root) as folders:
            for entry in folders:
                if (entry.name in known or re.fullmatch(r'[0-9a-f]{32}', entry.name) is None or
                        entry.is_symlink() or not entry.is_dir(follow_symlinks=False)):
                    continue
                folder = media_root / entry.name
                c.execute('INSERT INTO media(id,name,kind,size,status,error,created,source,retryable) '
                          'VALUES (?,?,?,?,?,?,?,?,0)',
                          (entry.name, '恢复的未完成上传 · ' + entry.name[:12], 'unknown', 0, 'error',
                           '发现未登记的素材目录，所有文件已保留。原件完整性未知，请重新上传，或预览后确认删除。',
                           time.time(), str(folder / 'original.recovered')))
        # A pre-upgrade queue can exceed the new atomic admission limit. Retain overflow
        # as visible retryable failures rather than enqueue unbounded work after restart.
        pending = c.execute("SELECT id FROM media WHERE status IN ('queued','processing') ORDER BY created,id").fetchall()
        for row in pending[QUEUE_LIMIT:]:
            c.execute("UPDATE media SET status='error',error=? WHERE id=?",
                      ('重启恢复时等待任务超过 20 个，原文件保留；请在队列有空位后重试。', row['id']))
        c.execute("UPDATE media SET status='queued' WHERE status='processing'")

    def setting(key, default=None, connection=None):
        if connection is not None:
            row = connection.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        else:
            with db() as c:
                row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def put_setting(c, key, value):
        c.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def publication_snapshot(c, entries):
        # Names and timing survive later media deletion; no live media references or files are retained.
        snapshot = []
        for entry in entries:
            row = c.execute('SELECT name,kind,pages FROM media WHERE id=?', (entry['mediaId'],)).fetchone()
            snapshot.append({'mediaId': entry['mediaId'], 'name': row['name'] if row else '已不可用素材',
                             'kind': row['kind'] if row else 'unknown',
                             'pageCount': len(json.loads(row['pages'])) if row else 0,
                             'duration': entry['duration']})
        return snapshot

    def record_publication(c, revision, entries, source, now):
        snapshot = publication_snapshot(c, entries)
        c.execute('UPDATE publications SET superseded_at=?,superseded_by_revision=? WHERE superseded_at IS NULL',
                  (now, revision))
        c.execute('INSERT INTO publications '
                  '(revision,source,created_at,recorded_at,snapshot,cache_policy,item_count,page_count) '
                  'VALUES (?,?,?,?,?,?,?,?)',
                  (revision, source, None if source == 'migrated' else now, now,
                   json.dumps(snapshot, ensure_ascii=False),
                   json.dumps(setting('cache_policy', DEFAULT_CACHE_POLICY, c)),
                   len(snapshot), sum(item['pageCount'] for item in snapshot)))
        target_filter = '' if source == 'migrated' else " WHERE binding_state='active'"
        c.execute('INSERT INTO publication_targets (revision,device_id,device_name,joined_at,binding_version) '
                  'SELECT ?,id,COALESCE(name,?),?,binding_version FROM devices' + target_filter,
                  (revision, '电视', now))

    # This is a one-time import of the only playlist preserved by pre-1.2 releases.
    # Do not invent publish times or acknowledgement times from an old device snapshot.
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing_revision = setting('revision', 0, c)
        if existing_revision > 0 and not c.execute('SELECT 1 FROM publications LIMIT 1').fetchone():
            record_publication(c, existing_revision, setting('published', [], c), 'migrated', time.time())

    def target_stage_sql():
        # Later exact receipts remain facts, but do not undo an incomplete publication's supersession.
        return """CASE WHEN t.withdrawn_at IS NOT NULL THEN 'withdrawn'
               WHEN t.applied_at IS NOT NULL AND
                    (p.superseded_at IS NULL OR t.applied_at <= p.superseded_at) THEN 'applied'
               WHEN p.superseded_at IS NOT NULL THEN 'superseded'
               WHEN d.last_seen IS NULL OR ? - d.last_seen >= 95 THEN 'offline'
               WHEN d.protocolVersion < 2 THEN 'unsupported'
               WHEN t.downloaded_at IS NOT NULL THEN 'downloaded'
               ELSE 'pending' END"""

    def publication_summary(c, row, now, current_revision):
        result = {'revision': row['revision'], 'source': row['source'], 'createdAt': row['created_at'],
                  'recordedAt': row['recorded_at'], 'supersededAt': row['superseded_at'],
                  'supersededByRevision': row['superseded_by_revision'],
                  'isCurrent': row['revision'] == current_revision,
                  'itemCount': row['item_count'], 'pageCount': row['page_count']}
        counts = {stage: 0 for stage in ('offline', 'unsupported', 'pending', 'downloaded', 'applied', 'superseded', 'withdrawn')}
        query = ('SELECT ' + target_stage_sql() + ' AS stage,COUNT(*) AS count '
                 'FROM publication_targets t JOIN publications p ON p.revision=t.revision '
                 'LEFT JOIN devices d ON d.id=t.device_id AND d.binding_version=t.binding_version WHERE t.revision=? GROUP BY stage')
        for count in c.execute(query, (now, row['revision'])):
            counts[count['stage']] = count['count']
        receipt_counts = c.execute('SELECT COUNT(CASE WHEN t.withdrawn_at IS NULL THEN t.downloaded_at END) AS downloaded, '
                                   'COALESCE(SUM(CASE WHEN t.withdrawn_at IS NULL AND d.last_seen IS NOT NULL AND ? - d.last_seen < 95 THEN 1 ELSE 0 END),0) AS online '
                                   'FROM publication_targets t LEFT JOIN devices d ON d.id=t.device_id AND d.binding_version=t.binding_version WHERE t.revision=?',
                                   (now, row['revision'])).fetchone()
        recorded_count = sum(counts.values())
        target_count = recorded_count - counts['withdrawn']
        return {**result, 'counts': counts, 'targetCount': target_count,
                'withdrawnCount': counts['withdrawn'], 'recordedTargetCount': recorded_count,
                'onlineCount': receipt_counts['online'], 'offlineCount': target_count - receipt_counts['online'],
                'appliedCount': counts['applied'], 'downloadedCount': receipt_counts['downloaded']}

    publication_columns = 'revision,source,created_at,recorded_at,item_count,page_count,superseded_at,superseded_by_revision'

    def current_publication(c, now):
        revision = setting('revision', 0, c)
        row = c.execute('SELECT ' + publication_columns + ' FROM publications WHERE revision=?', (revision,)).fetchone()
        return publication_summary(c, row, now, revision) if row else None

    def fail(message, code=400, error_code=None):
        return jsonify(error=message, **({'code': error_code} if error_code else {})), code

    def admin(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not session.get('admin'):
                return fail('请先登录管理后台。', 401)
            if request.method not in ('GET', 'HEAD'):
                supplied = request.headers.get('X-CSRF-Token', '')
                if (not isinstance(supplied, str) or len(supplied) > 48 or not supplied.isascii()
                        or not hmac.compare_digest(supplied, session.get('csrf', '_'))):
                    return fail('页面已过期，请刷新后重试。', 403)
            return f(*args, **kwargs)
        return wrapped

    def is_player(c):
        supplied = request.headers.get('Authorization', '')
        if not isinstance(supplied, str) or len(supplied) > 135 or not supplied.isascii():
            return False
        return hmac.compare_digest(supplied, 'Bearer ' + setting('display_token', connection=c))

    def device_identity(c):
        device_id = request.headers.get('X-LanScreen-Device-Id', '')
        token = request.headers.get('X-LanScreen-Device-Token', '')
        valid = (re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', device_id) is not None and
                 re.fullmatch(r'[0-9a-fA-F]{64}', token) is not None)
        digest = hashlib.sha256(token.lower().encode('ascii')).hexdigest() if valid else None
        # Revocation carries no access privilege. Report it even after the shared key
        # rotates so an online revoked TV can stop its old offline playlist promptly.
        if valid and c.execute('SELECT 1 FROM revoked_device_tokens WHERE device_id=? AND token_hash=?',
                               (device_id, digest)).fetchone():
            return None, fail('此设备凭证已解绑，请在电视上重新申请绑定。', 403, 'device_revoked')
        if not is_player(c):
            return None, fail('播放密钥无效，请重新设置电视连接。', 401, 'key_invalid')
        if not device_id or not token:
            return None, fail('请安装支持设备绑定的新版电视应用。', 426, 'upgrade_required')
        if not valid:
            return None, fail('设备凭证格式不正确。', 403, 'device_token_invalid')
        return (device_id, digest), None

    @contextmanager
    def request_db():
        if getattr(g, 'player_connection', None) is not None:
            yield g.player_connection
        else:
            with db() as c:
                c.execute('BEGIN')
                yield c

    def player(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            # Authorization and write operations share the same transaction. Unbind,
            # publish and heartbeat therefore cannot interleave into a revived target.
            with db() as c:
                c.execute('BEGIN' if request.method in ('GET', 'HEAD') else 'BEGIN IMMEDIATE')
                identity, error = device_identity(c)
                if error is not None:
                    return error
                device_id, digest = identity
                device = c.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
                if device is None:
                    return fail('设备尚未申请绑定。', 403, 'device_unknown')
                if device['binding_state'] == 'legacy':
                    return fail('请升级电视应用并申请绑定。', 426, 'upgrade_required')
                if not hmac.compare_digest(digest, device['device_token_hash']):
                    return fail('设备凭证不匹配，请检查电视绑定。', 403, 'device_token_invalid')
                if device['binding_state'] == 'unbound':
                    return fail('此电视已解绑。', 403, 'device_revoked')
                if device['binding_state'] != 'active':
                    return fail('设备正在等待管理员批准。', 403, 'device_pending')
                g.player_connection, g.player_device = c, device
                try:
                    return f(*args, **kwargs)
                finally:
                    g.pop('player_connection', None)
                    g.pop('player_device', None)
        return wrapped

    def withdraw_current_target(c, device, reason):
        c.execute('UPDATE publication_targets SET withdrawn_at=?,withdraw_reason=? '
                  'WHERE revision=? AND device_id=? AND binding_version=? AND withdrawn_at IS NULL',
                  (time.time(), reason, setting('revision', 0, c), device['id'], device['binding_version']))

    def join_current_target(c, device):
        revision = setting('revision', 0, c)
        current = c.execute('SELECT 1 FROM publications WHERE revision=? AND superseded_at IS NULL',
                            (revision,)).fetchone()
        if current:
            c.execute('INSERT OR IGNORE INTO publication_targets '
                      '(revision,device_id,device_name,joined_at,joined_later,binding_version) VALUES (?,?,?,?,1,?)',
                      (revision, device['id'], device['name'] or '电视', time.time(), device['binding_version']))

    @app.post('/api/player/register')
    def register_player():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return fail('请求内容必须为 JSON 对象。')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            identity, error = device_identity(c)
            if error is not None:
                return error
            device_id, digest = identity
            if type(body.get('protocolVersion')) is not int or body['protocolVersion'] != 3:
                return fail('请安装支持设备绑定的新版电视应用。', 426, 'upgrade_required')
            if body.get('deviceId') != device_id:
                return fail('请求中的设备编号不一致。', 403, 'device_token_invalid')
            name, version = body.get('name', '电视'), body.get('version', '')
            if not isinstance(name, str) or not isinstance(version, str) or len(name) > 100 or len(version) > 32:
                return fail('设备名称或版本格式不正确。')
            previous = c.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
            if previous and previous['binding_state'] in ('active', 'pending'):
                if not hmac.compare_digest(digest, previous['device_token_hash']):
                    return fail('设备已有绑定凭证，不能用其他凭证覆盖。', 403, 'device_token_invalid')
                # The TV polls registration every 30 seconds. Unchanged requests are read-only.
                if previous['name'] != name or previous['version'] != version:
                    c.execute('UPDATE devices SET name=?,version=? WHERE id=?', (name, version, device_id))
                return jsonify(ok=True, deviceId=device_id, bindingState=previous['binding_state'],
                               bindingVersion=previous['binding_version'], refreshSeconds=30)
            if previous and previous['device_token_hash'] and hmac.compare_digest(digest, previous['device_token_hash']):
                return fail('此设备凭证已解绑，请在电视上重新申请绑定。', 403, 'device_revoked')
            if c.execute("SELECT COUNT(*) FROM devices WHERE binding_state='pending'").fetchone()[0] >= 100:
                return fail('待审批设备已达 100 台，请管理员处理后再申请。', 429, 'pending_limit')
            generation = previous['binding_version'] + 1 if previous else 1
            if previous:
                withdraw_current_target(c, previous, 'binding_replaced')
            values = {'id': device_id, 'name': name, 'version': version, 'protocolVersion': 3,
                      'binding_state': 'pending', 'binding_version': generation, 'device_token_hash': digest,
                      'unbound_at': None, 'last_seen': None, 'status': '', 'current_item': '',
                      'cleanupState': 'pending', 'cleanupError': '', 'cleanupRequest': 0, 'cleanupCompletedRequest': 0}
            for key in ('appliedRevision', 'downloadedRevision', 'cleanupRevision', 'cacheBytes', 'freeBytes',
                        'cacheLimitBytes', 'cacheReserveBytes'):
                values[key] = -1
            names = list(values)
            c.execute('INSERT INTO devices (' + ','.join(names) + ') VALUES (' + ','.join('?' for _ in names) +
                      ') ON CONFLICT(id) DO UPDATE SET ' + ','.join(k + '=excluded.' + k for k in names if k != 'id'),
                      [values[k] for k in names])
        return jsonify(ok=True, deviceId=device_id, bindingState='pending', bindingVersion=generation, refreshSeconds=30)

    @app.post('/api/devices/<device_id>/approve')
    @admin
    def approve_device(device_id):
        return change_binding(device_id, True)

    @app.post('/api/devices/<device_id>/unbind')
    @admin
    def unbind_device(device_id):
        return change_binding(device_id, False)

    def change_binding(device_id, approve):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return fail('请求内容必须为 JSON 对象。')
        expected = body.get('bindingVersion')
        if type(expected) is not int or not 0 <= expected <= 2 ** 63 - 1:
            return fail('设备绑定版本不正确，请刷新页面。')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            device = c.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
            if device is None:
                return fail('设备不存在。', 404)
            if device['binding_version'] != expected:
                return fail('设备绑定已变化，请刷新页面后重试。', 409)
            state = device['binding_state']
            if approve:
                if state not in ('pending', 'active'):
                    return fail('请先从电视重新申请绑定，再批准设备。', 409)
                if state == 'pending':
                    c.execute("UPDATE devices SET binding_state='active' WHERE id=?", (device_id,))
                    join_current_target(c, device)
            elif state != 'unbound':
                now = time.time()
                if device['device_token_hash']:
                    c.execute('INSERT OR IGNORE INTO revoked_device_tokens VALUES (?,?,?,?)',
                              (device_id, device['device_token_hash'], expected, now))
                c.execute("UPDATE devices SET binding_state='unbound',unbound_at=? WHERE id=?", (now, device_id))
                withdraw_current_target(c, device, 'device_unbound')
        return jsonify(ok=True, deviceId=device_id, bindingState='active' if approve else 'unbound', bindingVersion=expected)

    @app.before_request
    def guard_origin():
        if request.path.startswith('/api/') and request.method not in ('GET', 'HEAD'):
            limit = 512 * 1024 * 1024 if request.path == '/api/media' else 256 * 1024 if request.path in ('/api/playlist', '/api/publish') else 8192
            if request.path == '/api/publications/cleanup':
                limit = 64 * 1024
            elif request.path == '/api/publications/cleanup-preview':
                limit = 16 * 1024
            request.max_content_length = limit
            if request.content_length and request.content_length > limit:
                return fail('请求体积超过此操作允许的上限。', 413)
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and request.headers.get('Origin'):
            try:
                origin = urlsplit(request.headers['Origin'])
            except ValueError:
                return fail('不接受其他网站发起的操作。', 403)
            if origin.netloc != request.host or origin.scheme != request.scheme:
                return fail('不接受其他网站发起的操作。', 403)
        if request.is_json and request.method not in ('GET', 'HEAD'):
            if not isinstance(request.get_json(silent=True), dict):
                return fail('请求内容必须为 JSON 对象。')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        if request.path.startswith('/api/player/') or request.path.startswith('/media/'):
            response.headers['Vary'] = 'Cookie, Authorization, X-LanScreen-Device-Id, X-LanScreen-Device-Token'
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith('/api/'):
            return fail('文件超过 512 MB 上限。' if error.code == 413 else error.description, error.code)
        return error

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception('Request failed')
        return fail('服务处理失败，请检查服务日志。', 500)

    @app.get('/')
    def home():
        return send_from_directory(app.static_folder, 'index.html')

    @app.get('/player')
    def browser_player():
        return send_from_directory(app.static_folder, 'player.html')

    def require_server_space(incoming=0):
        if shutil.disk_usage(data).free < SERVER_RESERVE_BYTES + incoming:
            raise ConversionLimitError('服务端剩余空间不足，需保留至少 1 GiB。已保留现有文件，请释放空间后重试。')

    def runtime_info(c):
        counts = dict(c.execute('SELECT status,COUNT(*) FROM media GROUP BY status').fetchall())
        with worker_lock:
            healthy = (not worker_state['failed'] and not worker_state['closed'] and
                       (worker_state['active'] is None or time.monotonic() - worker_state['started'] < WORKER_MAX_SECONDS))
            if (counts.get('queued', 0) or counts.get('processing', 0)) and not conversion_futures:
                healthy = False
        return {'queueLimit': QUEUE_LIMIT, 'uploadingCount': counts.get('uploading', 0),
                'queuedCount': counts.get('queued', 0), 'processingCount': counts.get('processing', 0),
                'workerReady': healthy, 'storageReserveBytes': SERVER_RESERVE_BYTES,
                'conversionOutputBudgetBytes': CONVERSION_OUTPUT_BUDGET, 'maxMediaBytes': MAX_MEDIA_BYTES}

    @app.get('/api/live')
    def live():
        return jsonify(ok=True, app='LanScreen', version='1.3.2')

    @app.get('/api/health')
    def health():
        checks = {'database': False, 'storage': False, 'worker': False}
        try:
            with db(timeout=2) as c:
                value = setting('revision', None, c)
                checks['database'] = type(value) is int and value >= 0 and os.access(dbfile, os.W_OK)
                checks['worker'] = runtime_info(c)['workerReady']
        except Exception:
            pass
        try:
            checks['storage'] = (shutil.disk_usage(data).free >= SERVER_RESERVE_BYTES and
                                 os.access(data, os.W_OK | os.X_OK))
        except OSError:
            pass
        ready = all(checks.values())
        return jsonify(ok=ready, app='LanScreen', version='1.3.2', checks=checks), 200 if ready else 503

    @app.get('/api/session')
    def session_info():
        return jsonify(initialized=bool(setting('password')), loggedIn=bool(session.get('admin')),
                       csrf=session.get('csrf', ''))

    def limited():
        key = request.remote_addr
        now = time.time()
        with rate_lock:
            if len(attempts) > 2048:
                for stale in list(attempts):
                    if not attempts[stale] or now - attempts[stale][-1] > 600:
                        del attempts[stale]
            attempts[key] = [t for t in attempts.get(key, []) if now - t < 600]
            if len(attempts[key]) >= 10:
                return True
            attempts[key].append(now)
        return False

    def start_session():
        session.clear()
        session['admin'] = True
        session['csrf'] = secrets.token_hex(24)
        session.permanent = True
        return jsonify(ok=True, csrf=session['csrf'])

    @app.post('/api/setup')
    def setup():
        if limited():
            return fail('尝试次数过多，请 10 分钟后重试。', 429)
        body = request.get_json(silent=True) or {}
        password = body.get('password', '')
        if not isinstance(password, str) or not 10 <= len(password) <= 128:
            return fail('管理员密码请使用 10～128 个字符。')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if setting('password', connection=c):
                return fail('管理员已设置，请登录。', 409)
            supplied = body.get('setupCode', '')
            if (not isinstance(supplied, str) or len(supplied) > 8 or not supplied.isascii()
                    or not hmac.compare_digest(supplied, setting('setup_code', connection=c))):
                return fail('初始化代码不正确，请查看服务启动窗口或 NAS 容器日志。', 403)
            put_setting(c, 'password', generate_password_hash(password))
            put_setting(c, 'setup_code', '')
        return start_session()

    @app.post('/api/login')
    def login():
        if limited():
            return fail('尝试次数过多，请 10 分钟后重试。', 429)
        body = request.get_json(silent=True) or {}
        password = body.get('password', '')
        hashed = setting('password')
        if not hashed or not isinstance(password, str) or len(password) > 128 or not check_password_hash(hashed, password):
            return fail('管理员密码不正确。', 401)
        return start_session()

    @app.post('/api/logout')
    @admin
    def logout():
        session.clear()
        return jsonify(ok=True)

    def serialize_media(row):
        value = dict(row)
        value['pages'] = json.loads(value['pages'])
        value.pop('source', None)
        value['retryAllowed'] = value['status'] == 'error' and bool(value.pop('retryable', 1))
        return value

    @app.get('/api/dashboard')
    @admin
    def dashboard():
        with db() as c:
            c.execute('BEGIN')
            media = [serialize_media(r) for r in c.execute('SELECT * FROM media WHERE archived=0 ORDER BY created DESC')]
            archived_media = [serialize_media(r) for r in c.execute('SELECT * FROM media WHERE archived=1 ORDER BY created DESC')]
            devices = [dict(r) for r in c.execute('SELECT * FROM devices ORDER BY last_seen DESC')]
            draft = setting('draft', [], c)
            published = setting('published', [], c)
            revision = setting('revision', 0, c)
            policy = setting('cache_policy', DEFAULT_CACHE_POLICY, c)
            now = time.time()
            publication = current_publication(c, now)
            runtime = runtime_info(c)
        for device in devices:
            device['bindingState'] = device.pop('binding_state')
            device['bindingVersion'] = device.pop('binding_version')
            device['unboundAt'] = device.pop('unbound_at')
            device.pop('device_token_hash', None)
            device['online'] = (device['bindingState'] == 'active' and device['last_seen'] is not None and
                                now - device['last_seen'] < 95)
            device['cleanupSupported'] = device['bindingState'] == 'active' and device['protocolVersion'] == 3
            if not device['online']:
                device['updateState'] = device['cleanupDisplayState'] = 'offline'
            elif not device['cleanupSupported']:
                device['updateState'] = device['cleanupDisplayState'] = 'unsupported'
            else:
                device['updateState'] = 'applied' if device['appliedRevision'] == revision else 'pending'
                complete = (device['cleanupRevision'] == revision and device['appliedRevision'] == revision and
                            device['cleanupCompletedRequest'] == device['cleanupRequest'])
                reported = device['cleanupState']
                device['cleanupDisplayState'] = reported if reported != 'complete' or complete else 'pending'
        return jsonify(media=media, archivedMedia=archived_media, devices=devices, draft=draft, published=published,
                       revision=revision, cachePolicy=policy, currentPublication=publication, displayToken=setting('display_token'),
                       capabilities={'office': bool(find_office()), 'ffmpeg': bool(find_ffmpeg())},
                       diskFree=shutil.disk_usage(data).free, serverTime=now, runtime=runtime)

    @app.put('/api/display-token')
    @admin
    def update_display_token():
        body = request.get_json(silent=True) or {}
        token = body.get('displayToken')
        if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9._~-]{12,128}', token) is None:
            return fail('播放密钥需为 12～128 个字符，仅支持英文字母、数字和 . _ ~ -，不能包含空格。')
        expected = body.get('currentDisplayToken')
        if not isinstance(expected, str):
            return fail('未取得当前播放密钥，请刷新页面后重试。')
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            current = setting('display_token', connection=c)
            if not expected.isascii() or not hmac.compare_digest(expected, current):
                return fail('播放密钥已发生变化，请刷新页面后重新确认。', 409)
            changed = not hmac.compare_digest(token, current)
            if changed:
                put_setting(c, 'display_token', token)
        return jsonify(ok=True, changed=changed)

    def check_conversion_running():
        if stopping.is_set():
            raise ConversionInterrupted('服务停止中断了转换，原文件已保留；下次启动将自动恢复。')

    def asset_info(item_id, file, kind='image', duration=None):
        check_conversion_running()
        size = file.stat().st_size
        if size > MAX_MEDIA_BYTES:
            raise ValueError('转换后的单个播放文件超过电视支持的 1 GiB，请拆分或降低码率后重新上传。')
        sha = hashlib.sha256()
        with file.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                check_conversion_running()
                sha.update(chunk)
        return {'id': item_id + ':' + file.name, 'type': kind, 'url': f'/media/{item_id}/{file.name}',
                'sha256': sha.hexdigest(), 'bytes': size, 'videoDuration': duration}

    def render_pdf(item_id, source, folder):
        return render_isolated(item_id, source, folder, 'pdf')

    def render_isolated(item_id, source, folder, kind):
        with db() as c:
            owner = c.execute('SELECT source FROM media WHERE id=?', (item_id,)).fetchone()
        budget_source = Path(owner['source']) if owner else source
        result = conversion_command([sys.executable, str(ROOT / 'render_worker.py'), kind, str(source), str(folder)],
                                    budget_source, folder, timeout=180)
        try:
            report = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError):
            raise ValueError('素材渲染进程异常，请缩小或重新导出素材后重试。')
        if result.returncode or 'error' in report:
            raise ValueError(report.get('error', '素材渲染失败。'))
        return [asset_info(item_id, folder / name) for name in report['files']]

    def conversion_command(command, source, folder, timeout):
        def check_budget():
            check_conversion_running()
            require_server_space()
            if directory_bytes(folder, exclude=source) > CONVERSION_OUTPUT_BUDGET:
                raise ConversionLimitError('转换生成文件累计超过 2 GiB，任务已停止并保留文件，请拆分素材后重新上传。')
            video = folder / 'video.mp4'
            if video.is_file() and video.stat().st_size > MAX_MEDIA_BYTES:
                raise ConversionLimitError('转换后的视频超过电视支持的 1 GiB，任务已停止，请拆分或降低码率后重新上传。')
        return run_bounded(command, timeout=timeout, check_budget=check_budget)

    def convert(item_id):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM media WHERE id=?', (item_id,)).fetchone()
            if stopping.is_set() or not row or row['status'] != 'queued' or not row['retryable']:
                return
            c.execute("UPDATE media SET status='processing', error='' WHERE id=?", (item_id,))
        source = Path(row['source'])
        folder = source.parent
        try:
            check_conversion_running()
            require_server_space()
            if row['kind'] == 'image':
                pages = render_isolated(item_id, source, folder, 'image')
            elif row['kind'] in ('pdf', 'ppt'):
                pdf = source
                if row['kind'] == 'ppt':
                    office = find_office()
                    if not office:
                        raise ValueError('未找到 LibreOffice。请安装后重试，或将 PPT 导出为 PDF 后上传。')
                    profile = folder / ('office-profile-' + uuid.uuid4().hex[:8])
                    # Isolated highest macro-security profile; never reuse the desktop user's profile.
                    (profile / 'user').mkdir(parents=True)
                    (profile / 'user' / 'registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Common/Load"><prop oor:name="UpdateDocMode" oor:op="fuse"><value>0</value></prop></item></oor:items>''', encoding='utf-8')
                    result = conversion_command([office, '-env:UserInstallation=' + profile.as_uri(), '--headless',
                                             '--norestore', '--convert-to', 'pdf:impress_pdf_Export', '--outdir', str(folder), str(source)],
                                            source, folder, timeout=180)
                    pdf = folder / (source.stem + '.pdf')
                    if result.returncode != 0 or not pdf.is_file():
                        raise ValueError('PPT 转换失败，请检查文件能否正常打开，或先导出为 PDF。')
                pages = render_pdf(item_id, pdf, folder)
            else:
                ffmpeg = find_ffmpeg()
                if not ffmpeg:
                    raise ValueError('未找到 FFmpeg，请检查服务端依赖安装后重试。')
                target = folder / 'video.mp4'
                # Decode and normalize all video inputs; an extension/ftyp marker is not validation.
                result = conversion_command([ffmpeg, '-nostdin', '-v', 'error', '-y', '-protocol_whitelist', 'file,pipe,crypto,data', '-i', str(source), '-map', '0:v:0',
                        '-map', '0:a:0?', '-vf', "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p",
                        '-c:v', 'libx264', '-preset', 'fast', '-crf', '22', '-c:a', 'aac',
                        '-threads', '2', '-movflags', '+faststart', str(target)], source, folder, timeout=1800)
                if result.returncode or not target.is_file() or target.stat().st_size < 128:
                    raise ValueError('视频无法解码，请确认文件完整，或先导出为 H.264 / AAC 编码的 MP4。')
                with target.open('rb') as f:
                    if b'ftyp' not in f.read(128):
                        raise ValueError('文件不是有效的 MP4 视频。')
                pages = [asset_info(item_id, target, 'video')]
            check_conversion_running()
            require_server_space()
            with db() as c:
                c.execute("UPDATE media SET status='ready', pages=?, error='' WHERE id=?", (json.dumps(pages), item_id))
        except ConversionInterrupted as error:
            # Keep the existing queue reservation. Startup already resumes queued jobs,
            # including after rollback to an older release; ordinary errors stay errors.
            with db() as c:
                c.execute("UPDATE media SET status='queued',error=? WHERE id=? AND status='processing' AND retryable=1",
                          (str(error), item_id))
        except subprocess.TimeoutExpired:
            with db() as c:
                c.execute("UPDATE media SET status='error', error=? WHERE id=?", ('转换超时，请缩小或拆分素材后重新上传。', item_id))
        except Exception as error:
            app.logger.warning('Conversion failed for %s: %s', item_id, type(error).__name__)
            message = str(error) if isinstance(error, ValueError) else '无法读取素材，请确认文件完整且格式正确。'
            with db() as c:
                c.execute("UPDATE media SET status='error', error=? WHERE id=?", (message[:500], item_id))

    def schedule_conversion(item_id):
        def work():
            with worker_lock:
                worker_state.update(active=item_id, started=time.monotonic())
            try:
                app.extensions['lanscreen']['convert'](item_id)
                with worker_lock:
                    worker_state['failed'] = False
            except Exception:
                with worker_lock:
                    worker_state['failed'] = True
                app.logger.exception('Conversion worker failed for %s', item_id)
                try:
                    with db() as c:
                        c.execute("UPDATE media SET status='error',error=? WHERE id=? AND status IN ('queued','processing')",
                                  ('转换工作线程异常，原文件已保留；请检查服务状态后重试。', item_id))
                except Exception:
                    pass
            finally:
                with worker_lock:
                    worker_state.update(active=None, started=0.0)

        def finished(future):
            with worker_lock:
                if conversion_futures.get(item_id) is not future:
                    return
                conversion_futures.pop(item_id)
                rerun = item_id in conversion_reruns and not stopping.is_set()
                conversion_reruns.discard(item_id)
            if rerun:
                schedule_conversion(item_id)

        with worker_lock:
            if stopping.is_set():
                return False
            if item_id in conversion_futures:
                conversion_reruns.add(item_id)
                return True
            try:
                future = executor.submit(work)
                conversion_futures[item_id] = future
            except RuntimeError:
                worker_state['closed'] = True
                future = None
        if future is None:
            with db() as c:
                c.execute("UPDATE media SET status='error',error=? WHERE id=?",
                          ('转换工作线程不可用，原文件已保留；请恢复服务后重试。', item_id))
            return False
        future.add_done_callback(finished)
        return True

    @app.post('/api/media')
    @admin
    def upload():
        # Fast rejection before multipart parsing; the later transaction is the actual
        # atomic reservation. HTTP server receive/spool buffers have their own limits.
        with db() as c:
            if c.execute("SELECT COUNT(*) FROM media WHERE status IN ('uploading','queued','processing')").fetchone()[0] >= QUEUE_LIMIT:
                return fail('已有 20 个素材正在上传或等待处理，请稍后继续上传。', 429)
        try:
            require_server_space()
        except ConversionLimitError as error:
            return fail(str(error), 507)
        file = request.files.get('file')
        if not file or not file.filename:
            return fail('请选择一个素材文件。')
        name = file.filename.replace('\\', '/').split('/')[-1][:180]
        ext = Path(name).suffix.lower()
        if ext not in EXTENSIONS:
            return fail('支持图片、PDF、PPT / PPTX 和常见视频格式。')
        try:
            require_server_space()
        except ConversionLimitError as error:
            return fail(str(error), 507)
        item_id = uuid.uuid4().hex
        folder = media_root / item_id
        source = folder / ('original' + ext)
        kind = 'pdf' if ext == '.pdf' else 'ppt' if ext in ('.ppt', '.pptx') else 'video' if ext in ('.mp4', '.m4v', '.mov', '.mkv', '.avi', '.webm') else 'image'
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT COUNT(*) FROM media WHERE status IN ('uploading','queued','processing')").fetchone()[0] >= QUEUE_LIMIT:
                return fail('已有 20 个素材正在上传或等待处理，请稍后继续上传。', 429)
            c.execute('INSERT INTO media (id,name,kind,size,status,created,source,retryable) VALUES (?,?,?,?,?,?,?,0)',
                      (item_id, name, kind, 0, 'uploading', time.time(), str(source)))

        def invalid_upload(message, code=400):
            try:
                size = source.stat().st_size
            except OSError:
                size = 0
            with db() as c:
                c.execute("UPDATE media SET status='error',retryable=0,size=?,error=? WHERE id=?",
                          (size, message[:500], item_id))
            return jsonify(error=message, id=item_id, status='error', retryAllowed=False), code

        try:
            folder.mkdir()
            size = save_upload(file.stream, source, require_server_space)
            if size == 0:
                raise ValueError('上传文件为空，请选择完整文件后重新上传。')
            # Bound ZIP expansion before handing presentation files to the Office parser.
            if ext == '.pptx':
                with zipfile.ZipFile(source) as archive:
                    members = archive.infolist()
                    names = set(archive.namelist())
                    if not {'[Content_Types].xml', 'ppt/presentation.xml'} <= names:
                        raise ValueError('文件不是有效的 PPTX 演示文稿。')
                    if len(members) > 20000 or sum(x.file_size for x in members) > 1024 * 1024 * 1024 or any(x.file_size > 256 * 1024 * 1024 for x in members):
                        raise ValueError('PPTX 解压后内容过大，请拆分或先导出为 PDF。')
                    if any(part.flag_bits & 1 for part in members):
                        raise ValueError('PPTX 含加密内容，请导出无密码版本后重新上传。')
                    for part in members:
                        if part.filename.endswith('.rels'):
                            if part.file_size > 4 * 1024 * 1024:
                                raise ValueError('PPTX 关系文件过大，请重新导出。')
                            links = ElementTree.fromstring(archive.read(part))
                            if any(x.get('TargetMode') == 'External' and not x.get('Type', '').endswith('/hyperlink') for x in links):
                                raise ValueError('PPTX 含外部链接素材，请嵌入素材或先导出为 PDF。')
            elif ext == '.ppt':
                with source.open('rb') as f:
                    if f.read(8) != b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
                        raise ValueError('文件不是有效的 PPT 演示文稿。')
            with db() as c:
                c.execute("UPDATE media SET size=?,status='queued',retryable=1 WHERE id=? AND status='uploading'",
                          (size, item_id))
        except ConversionLimitError as error:
            return invalid_upload(str(error), 507)
        except (zipfile.BadZipFile, ElementTree.ParseError, RuntimeError, NotImplementedError):
            return invalid_upload('素材内容损坏、加密或格式不受支持，文件已保留，请重新导出后上传。')
        except ValueError as error:
            return invalid_upload(str(error))
        except OSError:
            return invalid_upload('上传写入中断，已保留当前文件。请检查可用空间后重新上传。', 507)
        except Exception:
            app.logger.exception('Upload failed for %s', item_id)
            return invalid_upload('上传未完成，文件已保留。请检查服务状态后重新上传。', 500)
        if not schedule_conversion(item_id):
            return jsonify(error='转换工作线程不可用，文件已保留，请恢复服务后重试。', id=item_id), 503
        return jsonify(id=item_id, status='queued'), 202

    @app.post('/api/media/<item_id>/retry')
    @admin
    def retry(item_id):
        try:
            require_server_space()
        except ConversionLimitError as error:
            return fail(str(error), 507)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT COUNT(*) FROM media WHERE status IN ('uploading','queued','processing')").fetchone()[0] >= QUEUE_LIMIT:
                return fail('已有 20 个素材正在上传或等待处理，请稍后重试。', 429)
            result = c.execute("UPDATE media SET status='queued',error='' WHERE id=? AND status='error' AND retryable=1", (item_id,))
            if result.rowcount != 1:
                return fail('只有原文件完整的转换失败素材可以重试；上传中断或恢复目录请重新上传。')
        if not schedule_conversion(item_id):
            return fail('转换工作线程不可用，原文件已保留，请恢复服务后重试。', 503)
        return jsonify(ok=True)

    @app.post('/api/media/<item_id>/archive')
    @admin
    def archive(item_id):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT status FROM media WHERE id=?', (item_id,)).fetchone()
            if not row:
                return fail('素材不存在。', 404)
            if row['status'] == 'deleting':
                return fail('素材正在删除。', 409)
            used = setting('draft', [], c) + setting('published', [], c)
            if any(x['mediaId'] == item_id for x in used):
                return fail('请先从播放清单移除并发布，再归档此素材。', 409)
            c.execute('UPDATE media SET archived=1 WHERE id=?', (item_id,))
        return jsonify(ok=True)

    def deletion_reason(row, c):
        if row['status'] in ('uploading', 'queued', 'processing', 'deleting'):
            return '素材正在上传、转换或删除，请等待处理结束。'
        if any(entry['mediaId'] == row['id'] for entry in setting('draft', [], c) + setting('published', [], c)):
            return '请先从草稿和已发布播放清单中移除此素材，再执行删除。'
        return ''

    def directory_flags():
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def identity(info):
        return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns]

    def deletion_inventory(row):
        item_id = row['id']
        if len(item_id) != 32 or any(ch not in '0123456789abcdef' for ch in item_id):
            raise ValueError('素材目录编号不符合安全要求。')
        if Path(row['source']).parent != media_root / item_id:
            raise ValueError('素材路径不属于此素材的独立目录，已拒绝删除。')
        entries = []
        with_directory = directory_flags()
        root_fd = os.open(media_root, with_directory)
        try:
            try:
                folder_fd = os.open(item_id, with_directory, dir_fd=root_fd)
            except FileNotFoundError:
                return {'root': None, 'entries': []}
            try:
                root_identity = identity(os.fstat(folder_fd))
                def walk(fd, prefix=''):
                    with os.scandir(fd) as iterator:
                        names = sorted(entry.name for entry in iterator)
                    for name in names:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        relative = prefix + name
                        is_dir = stat.S_ISDIR(info.st_mode)
                        if not is_dir and not stat.S_ISREG(info.st_mode):
                            raise ValueError('素材目录包含符号链接或特殊文件，已拒绝删除。')
                        entry = {'path': relative, 'directory': is_dir, 'identity': identity(info)}
                        entries.append(entry)
                        if len(entries) > 30000:
                            raise ValueError('素材目录条目过多，无法安全预览删除。')
                        if is_dir:
                            child = os.open(name, with_directory, dir_fd=fd)
                            try:
                                if identity(os.fstat(child)) != entry['identity']:
                                    raise ValueError('素材目录已变化，请重新预览。')
                                walk(child, relative + '/')
                            finally:
                                os.close(child)
                walk(folder_fd)
                return {'root': root_identity, 'entries': entries}
            finally:
                os.close(folder_fd)
        finally:
            os.close(root_fd)

    def deletion_digest(row, inventory, expires):
        payload = json.dumps([row['id'], row['name'], row['source'], expires, inventory], sort_keys=True,
                             separators=(',', ':'), ensure_ascii=False).encode()
        return hmac.new(app.secret_key.encode(), payload, hashlib.sha256).hexdigest()

    def delete_inventory(row, inventory):
        root_fd = os.open(media_root, directory_flags())
        try:
            if inventory['root'] is None:
                try:
                    os.stat(row['id'], dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                raise ValueError('素材目录已变化，请重新预览。')
            folder_fd = os.open(row['id'], directory_flags(), dir_fd=root_fd)
            try:
                if identity(os.fstat(folder_fd)) != inventory['root']:
                    raise ValueError('素材目录已变化，请重新预览。')
                by_parent = {}
                for entry in inventory['entries']:
                    parent, _, name = entry['path'].rpartition('/')
                    by_parent.setdefault(parent, []).append((name, entry))
                def remove_tree(fd, prefix=''):
                    expected = by_parent.get(prefix, [])
                    with os.scandir(fd) as iterator:
                        actual = {entry.name for entry in iterator}
                    if actual != {name for name, _ in expected}:
                        raise ValueError('素材目录内容已变化，请重新预览。')
                    for name, entry in expected:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if identity(info) != entry['identity']:
                            raise ValueError('素材文件已变化，请重新预览。')
                        if entry['directory']:
                            child = os.open(name, directory_flags(), dir_fd=fd)
                            try:
                                if identity(os.fstat(child)) != entry['identity']:
                                    raise ValueError('素材目录已变化，请重新预览。')
                                remove_tree(child, entry['path'])
                            finally:
                                os.close(child)
                            # The directory's size/mtime changes as its children are removed.
                            remaining = os.stat(name, dir_fd=fd, follow_symlinks=False)
                            if (remaining.st_dev, remaining.st_ino, remaining.st_mode) != tuple(entry['identity'][:3]):
                                raise ValueError('素材目录已变化，请重新预览。')
                        _remove_owned_entry(name, fd, entry['directory'])
                remove_tree(folder_fd)
                remaining = os.stat(row['id'], dir_fd=root_fd, follow_symlinks=False)
                if (remaining.st_dev, remaining.st_ino, remaining.st_mode) != tuple(inventory['root'][:3]):
                    raise ValueError('素材目录已变化，请重新预览。')
                _remove_owned_entry(row['id'], root_fd, True)
            finally:
                os.close(folder_fd)
        finally:
            os.close(root_fd)

    @app.get('/api/media/<item_id>/deletion-preview')
    @admin
    def deletion_preview(item_id):
        with db() as c:
            c.execute('BEGIN')
            row = c.execute('SELECT * FROM media WHERE id=?', (item_id,)).fetchone()
            if not row:
                return fail('素材不存在。', 404)
            reason = deletion_reason(row, c)
            inventory = {'root': None, 'entries': []}
            if not reason:
                try:
                    inventory = deletion_inventory(row)
                except (ValueError, OSError) as error:
                    reason = str(error) if isinstance(error, ValueError) else '无法安全检查素材目录，请检查目录权限及符号链接。'
            files = [{'path': item_id + '/' + entry['path'], 'bytes': entry['identity'][3]}
                     for entry in inventory['entries'] if not entry['directory']]
            expires = int(time.time()) + 300
            token = str(expires) + '.' + deletion_digest(row, inventory, expires) if not reason else ''
            return jsonify(id=item_id, name=row['name'], allowed=not reason, reason=reason, files=files,
                           fileCount=len(files), totalBytes=sum(file['bytes'] for file in files), deletionToken=token)

    @app.post('/api/media/<item_id>/delete')
    @admin
    def delete_media(item_id):
        body = request.get_json(silent=True) or {}
        # Commit the non-playable state BEFORE any removal. A crash leaves a retryable record.
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM media WHERE id=?', (item_id,)).fetchone()
            if not row:
                return fail('素材不存在。', 404)
            reason = deletion_reason(row, c)
            if reason:
                return fail(reason, 409)
            if body.get('confirmName') != row['name']:
                return fail('请输入完整素材名称确认删除。')
            supplied = body.get('deletionToken')
            try:
                if not isinstance(supplied, str) or len(supplied) > 100:
                    raise ValueError()
                expiry_text, digest = supplied.split('.')
                if len(digest) != 64 or any(ch not in '0123456789abcdef' for ch in digest):
                    raise ValueError()
                expires = int(expiry_text)
                if not int(time.time()) <= expires <= int(time.time()) + 300:
                    raise ValueError()
                inventory = deletion_inventory(row)
                if not hmac.compare_digest(digest, deletion_digest(row, inventory, expires)):
                    raise ValueError()
            except (ValueError, OSError):
                return fail('删除预览已过期或文件发生变化，请重新核对后确认。', 409)
            c.execute("UPDATE media SET status='deleting', error='' WHERE id=?", (item_id,))
        try:
            delete_inventory(row, inventory)
        except (OSError, ValueError):
            app.logger.exception('Media deletion incomplete for %s', item_id)
            with db() as c:
                c.execute("UPDATE media SET status='delete_error', error=? WHERE id=?",
                          ('删除未完成，部分文件可能已删除。请重新预览剩余文件后重试。', item_id))
            return fail('删除未完成，部分文件可能已删除。请重新预览剩余文件后重试。', 500)
        with db() as c:
            c.execute('DELETE FROM media WHERE id=? AND status=?', (item_id, 'deleting'))
        files = [entry for entry in inventory['entries'] if not entry['directory']]
        return jsonify(ok=True, id=item_id, fileCount=len(files), totalBytes=sum(entry['identity'][3] for entry in files))

    @app.put('/api/cache-policy')
    @admin
    def cache_policy():
        body = request.get_json(silent=True) or {}
        maximum, reserve = body.get('maxBytes'), body.get('reserveBytes')
        if type(maximum) is not int or not 256 * 1024 ** 2 <= maximum <= 32 * 1024 ** 3:
            return fail('缓存上限需为 256 MB～32 GB。')
        if type(reserve) is not int or not 128 * 1024 ** 2 <= reserve <= 4 * 1024 ** 3:
            return fail('最低剩余空间需为 128 MB～4 GB。')
        policy = {'maxBytes': maximum, 'reserveBytes': reserve}
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            revision = setting('revision', 0, c)
            if policy != setting('cache_policy', DEFAULT_CACHE_POLICY, c):
                revision += 1
                put_setting(c, 'cache_policy', policy)
                put_setting(c, 'revision', revision)
                record_publication(c, revision, setting('published', [], c), 'cache_policy', time.time())
        return jsonify(ok=True, revision=revision, cachePolicy=policy)

    @app.post('/api/devices/<device_id>/cleanup')
    @admin
    def request_cleanup(device_id):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            device = c.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
            if not device:
                return fail('设备不存在。', 404)
            if device['binding_state'] != 'active':
                return fail('仅已批准且未解绑的电视可以请求清理缓存。', 409)
            if device['protocolVersion'] < 2:
                return fail('此电视版本不支持缓存清理，请先更新电视安装包。', 409)
            command = device['cleanupRequest'] + 1
            c.execute('UPDATE devices SET cleanupRequest=? WHERE id=?', (command, device_id))
        return jsonify(ok=True, cleanupRequest=command)

    def validate_playlist(value, c):
        if not isinstance(value, list) or len(value) > 500:
            raise ValueError('播放清单最多包含 500 个素材。')
        result = []
        page_count = 0
        for entry in value:
            if not isinstance(entry, dict):
                raise ValueError('播放清单格式不正确。')
            media_id = entry.get('mediaId')
            if not isinstance(media_id, str):
                raise ValueError('素材编号不正确。')
            row = c.execute('SELECT status,archived,pages FROM media WHERE id=?', (media_id,)).fetchone()
            if not row or row['status'] != 'ready' or row['archived']:
                raise ValueError('清单包含未就绪或已归档素材，请刷新后检查。')
            pages = json.loads(row['pages'])
            if not isinstance(pages, list) or not pages:
                raise ValueError('清单包含没有有效播放文件的素材，请重新转换后发布。')
            page_count += len(pages)
            if page_count > 2000:
                raise ValueError('展开后的播放页面不能超过 2000 项，请精简清单。')
            for page in pages:
                if not isinstance(page, dict):
                    raise ValueError('素材播放文件记录不完整，请重新转换后发布。')
                declared = page.get('bytes')
                if 'bytes' in page and (type(declared) is not int or declared <= 0):
                    raise ValueError('素材播放文件大小记录不正确，请重新转换后发布。')
                if type(declared) is int and declared > MAX_MEDIA_BYTES:
                    raise ValueError('清单包含超过电视 1 GiB 上限的播放文件，请拆分或重新转换后发布。')
                url = page.get('url', '')
                filename = url.rsplit('/', 1)[-1] if isinstance(url, str) else ''
                target = media_root / media_id / filename
                try:
                    if (not filename or url != f'/media/{media_id}/{filename}' or filename in ('.', '..') or
                            target.is_symlink() or not target.is_file()):
                        raise OSError('unavailable playback asset')
                    actual = target.stat().st_size
                except OSError:
                    raise ValueError('清单包含缺失或不安全的播放文件，请检查素材后重新发布。')
                if not 0 < actual <= MAX_MEDIA_BYTES:
                    raise ValueError('播放文件为空或超过电视 1 GiB 上限，请拆分或重新转换后发布。')
                if type(declared) is int and declared > 0 and actual != declared:
                    raise ValueError('播放文件大小与转换记录不一致，请重新转换后发布。')
            duration = entry.get('duration', 10)
            if type(duration) is not int or not 1 <= duration <= 3600:
                raise ValueError('每页停留时间需为 1～3600 秒的整数。')
            result.append({'mediaId': media_id, 'duration': duration})
        return result

    @app.put('/api/playlist')
    @admin
    def save_playlist():
        body = request.get_json(silent=True) or {}
        try:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                entries = validate_playlist(body.get('items'), c)
                put_setting(c, 'draft', entries)
        except ValueError as e:
            return fail(str(e))
        return jsonify(ok=True)

    @app.post('/api/publish')
    @admin
    def publish():
        body = request.get_json(silent=True) or {}
        try:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                entries = validate_playlist(body.get('items', setting('draft', [], c)), c)
                revision = setting('revision', 0, c) + 1
                put_setting(c, 'draft', entries)
                put_setting(c, 'published', entries)
                put_setting(c, 'revision', revision)
                now = time.time()
                record_publication(c, revision, entries, 'publish', now)
                publication = current_publication(c, now)
        except ValueError as e:
            return fail(str(e))
        return jsonify(ok=True, revision=revision, publication=publication)

    def publication_identity(row):
        # Receipts and supersession are mutable facts. The publication's original
        # content and creation identity remain fixed even when another publish occurs.
        keys = ('revision', 'source', 'created_at', 'recorded_at', 'snapshot',
                'cache_policy', 'item_count', 'page_count')
        canonical = json.dumps([row[key] for key in keys], ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    def sign_history_cleanup(payload):
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')
        signature = hmac.new(app.secret_key.encode(), b'publication-cleanup\0' + encoded.encode(), hashlib.sha256).hexdigest()
        return encoded + '.' + signature

    def read_history_cleanup_token(token):
        if not isinstance(token, str) or not token or not token.isascii():
            raise ValueError('清理确认凭证不正确，请重新预览。')
        if len(token) > 48 * 1024:
            raise OverflowError('清理确认凭证超过允许的上限。')
        try:
            encoded, signature = token.split('.')
            expected = hmac.new(app.secret_key.encode(), b'publication-cleanup\0' + encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError('invalid signature')
            raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
            payload = json.loads(raw)
            if (not isinstance(payload, dict) or payload.get('kind') != 'publication-cleanup-v1' or
                    type(payload.get('issuedAt')) is not int or type(payload.get('expiresAt')) is not int or
                    payload['expiresAt'] != payload['issuedAt'] + 300 or
                    not isinstance(payload.get('records'), list) or len(payload['records']) > 200):
                raise ValueError('invalid payload')
            revisions = set()
            for record in payload['records']:
                if (not isinstance(record, dict) or type(record.get('revision')) is not int or
                        not 1 <= record['revision'] <= 2 ** 63 - 1 or record['revision'] in revisions or
                        not isinstance(record.get('identity'), str) or
                        re.fullmatch(r'[0-9a-f]{64}', record['identity']) is None):
                    raise ValueError('invalid record')
                revisions.add(record['revision'])
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            raise ValueError('清理确认凭证不正确或已被修改，请重新预览。')
        now = time.time()
        if payload['issuedAt'] > now or payload['expiresAt'] <= now:
            raise TimeoutError('清理预览已过期，请重新预览后确认。')
        return payload

    @app.post('/api/publications/cleanup-preview')
    @admin
    def publication_cleanup_preview():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) not in ({'revisions'}, {'keepLatest'}):
            return fail('请选择指定历史版本或保留最近记录其中一种清理方式。')
        explicit = 'revisions' in body
        if explicit:
            revisions = body['revisions']
            if (not isinstance(revisions, list) or not 1 <= len(revisions) <= 200 or
                    any(type(revision) is not int or not 1 <= revision <= 2 ** 63 - 1 for revision in revisions) or
                    len(set(revisions)) != len(revisions)):
                return fail('请选择 1～200 个不同的历史版本编号。')
        elif type(body['keepLatest']) is not int or not 1 <= body['keepLatest'] <= 5000:
            return fail('保留记录数需为 1～5000 的整数，包含当前发布记录。')
        with db() as c:
            c.execute('BEGIN')
            current = setting('revision', 0, c)
            if explicit:
                if current in revisions:
                    return fail('当前发布记录受保护，不能清理。', 409)
                placeholders = ','.join('?' for _ in revisions)
                rows = c.execute('SELECT * FROM publications WHERE revision IN (' + placeholders + ') ORDER BY revision',
                                 revisions).fetchall()
                if len(rows) != len(revisions):
                    return fail('所选历史记录已变化或已被清理，请刷新后重新预览。', 409)
                total_candidates = len(rows)
            else:
                current_exists = bool(c.execute('SELECT 1 FROM publications WHERE revision=?', (current,)).fetchone())
                keep_other = body['keepLatest'] - int(current_exists)
                # Keep the current publication plus the newest remaining records.
                # LIMIT bounds both the UI preview and confirmation token to 200 rows.
                cutoff = c.execute('SELECT revision FROM publications WHERE revision<>? '
                                   'ORDER BY revision DESC LIMIT 1 OFFSET ?', (current, keep_other)).fetchone()
                if cutoff is None:
                    total_candidates, rows = 0, []
                else:
                    boundary = (current, cutoff['revision'])
                    total_candidates = c.execute('SELECT COUNT(*) FROM publications WHERE revision<>? AND revision<=?',
                                                 boundary).fetchone()[0]
                    rows = c.execute('SELECT * FROM publications WHERE revision<>? AND revision<=? '
                                     'ORDER BY revision LIMIT 200', boundary).fetchall()
            revisions = [row['revision'] for row in rows]
            target_count = 0
            if revisions:
                placeholders = ','.join('?' for _ in revisions)
                target_count = c.execute('SELECT COUNT(*) FROM publication_targets WHERE revision IN (' + placeholders + ')',
                                         revisions).fetchone()[0]
            now = int(time.time())
            payload = {'kind': 'publication-cleanup-v1', 'issuedAt': now, 'expiresAt': now + 300,
                       'currentRevision': current,
                       'records': [{'revision': row['revision'], 'identity': publication_identity(row)} for row in rows]}
        return jsonify(revisions=revisions, deleteCount=len(rows), targetCount=target_count,
                       totalCandidates=total_candidates, hasMore=total_candidates > len(rows), currentRevision=current,
                       confirmationToken=sign_history_cleanup(payload), expiresAt=payload['expiresAt'],
                       expiresIn=max(0, min(300, payload['expiresAt'] - time.time())))

    @app.post('/api/publications/cleanup')
    @admin
    def publication_cleanup():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'confirmationToken'}:
            return fail('请先预览要清理的历史记录，再提交确认凭证。')
        try:
            with db() as c:
                c.execute('BEGIN IMMEDIATE')
                payload = read_history_cleanup_token(body['confirmationToken'])
                revisions = [record['revision'] for record in payload['records']]
                current = setting('revision', 0, c)
                if current in revisions:
                    return fail('当前发布记录受保护，不能清理；请重新预览。', 409)
                deleted_count = target_count = already_deleted = 0
                if revisions:
                    placeholders = ','.join('?' for _ in revisions)
                    rows = {row['revision']: row for row in c.execute(
                        'SELECT * FROM publications WHERE revision IN (' + placeholders + ')', revisions)}
                    for record in payload['records']:
                        row = rows.get(record['revision'])
                        if row is not None and not hmac.compare_digest(record['identity'], publication_identity(row)):
                            return fail('所选历史记录内容已变化，请重新预览后确认。', 409)
                    # The signed list is the complete scope. Missing rows are prior
                    # successful cleanup, never permission to select replacement rows.
                    already_deleted = len(revisions) - len(rows)
                    target_count = c.execute('DELETE FROM publication_targets WHERE revision IN (' + placeholders + ')',
                                             revisions).rowcount
                    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_targets_before_binding'").fetchone():
                        c.execute('DELETE FROM publication_targets_before_binding WHERE revision IN (' + placeholders + ')', revisions)
                    deleted_count = c.execute('DELETE FROM publications WHERE revision IN (' + placeholders + ')', revisions).rowcount
                remaining = c.execute('SELECT COUNT(*) FROM publications').fetchone()[0]
        except OverflowError as error:
            return fail(str(error), 413)
        except TimeoutError as error:
            return fail(str(error), 409)
        except ValueError as error:
            return fail(str(error), 400)
        return jsonify(ok=True, deletedCount=deleted_count, targetCount=target_count, remainingCount=remaining,
                       currentRevision=current, alreadyDeletedCount=already_deleted)

    @app.get('/api/publications')
    @admin
    def publications():
        try:
            page = int(request.args.get('page', '1'))
            page_size = int(request.args.get('pageSize', '10'))
        except (ValueError, TypeError):
            return fail('发布记录分页参数不正确。')
        if not 1 <= page <= 1000000 or not 1 <= page_size <= 50:
            return fail('页码需为 1～1000000，每页需为 1～50 条。')
        with db() as c:
            c.execute('BEGIN')
            revision = setting('revision', 0, c)
            total = c.execute('SELECT COUNT(*) AS count FROM publications').fetchone()['count']
            page = min(page, max(1, (total + page_size - 1) // page_size))
            rows = c.execute('SELECT ' + publication_columns + ' FROM publications ORDER BY revision DESC LIMIT ? OFFSET ?',
                             (page_size, (page - 1) * page_size)).fetchall()
            now = time.time()
            items = [publication_summary(c, row, now, revision) for row in rows]
        return jsonify(items=items, total=total, page=page, pageSize=page_size,
                       hasNext=page * page_size < total, cleanupSupported=True)

    @app.get('/api/publications/<int:revision>')
    @admin
    def publication_detail(revision):
        if not 1 <= revision <= 2 ** 63 - 1:
            return fail('发布记录不存在。', 404)
        with db() as c:
            c.execute('BEGIN')
            row = c.execute('SELECT * FROM publications WHERE revision=?', (revision,)).fetchone()
            if not row:
                return fail('发布记录不存在。', 404)
            now = time.time()
            result = publication_summary(c, row, now, setting('revision', 0, c))
            query = ('SELECT t.*,d.last_seen,d.protocolVersion,' + target_stage_sql() + ' AS stage '
                     'FROM publication_targets t JOIN publications p ON p.revision=t.revision '
                     'LEFT JOIN devices d ON d.id=t.device_id AND d.binding_version=t.binding_version '
                     'WHERE t.revision=? ORDER BY t.joined_at,t.device_id,t.binding_version')
            targets = []
            for target in c.execute(query, (now, revision)):
                targets.append({'deviceId': target['device_id'], 'name': target['device_name'],
                                'joinedAt': target['joined_at'], 'joinedLater': bool(target['joined_later']),
                                'downloadedAt': target['downloaded_at'], 'appliedAt': target['applied_at'],
                                'bindingVersion': target['binding_version'], 'withdrawnAt': target['withdrawn_at'],
                                'withdrawReason': target['withdraw_reason'],
                                'stage': target['stage'],
                                'online': target['withdrawn_at'] is None and target['last_seen'] is not None and now - target['last_seen'] < 95,
                                'lastSeen': target['last_seen'], 'protocolVersion': target['protocolVersion']})
            result.update(items=json.loads(row['snapshot']), targets=targets,
                          cachePolicy=json.loads(row['cache_policy']))
        return jsonify(result)

    def manifest():
        items = []
        with request_db() as c:
            revision = setting('revision', 0, c)
            policy = setting('cache_policy', DEFAULT_CACHE_POLICY, c)
            for entry in setting('published', [], c):
                row = c.execute('SELECT * FROM media WHERE id=?', (entry['mediaId'],)).fetchone()
                if row and row['status'] == 'ready':
                    pages = json.loads(row['pages'])
                    for i, page in enumerate(pages):
                        if type(page.get('bytes')) is not int or page['bytes'] < 0:
                            filename = page.get('url', '').rsplit('/', 1)[-1]
                            try:
                                page['bytes'] = (media_root / row['id'] / filename).stat().st_size
                            except OSError:
                                page['bytes'] = 0
                        items.append({**page, 'name': row['name'] + (f' · 第 {i + 1} 页' if len(pages) > 1 else ''),
                                      'duration': entry['duration']})
        return {'revision': revision, 'refreshSeconds': 30, 'items': items, 'cachePolicy': policy, 'serverTime': time.time()}

    @app.get('/api/player/manifest')
    @player
    def player_manifest():
        return jsonify(**manifest(), bindingVersion=g.player_device['binding_version'])

    @app.get('/api/preview/manifest')
    @admin
    def preview_manifest():
        return jsonify(manifest())

    @app.post('/api/player/heartbeat')
    @player
    def heartbeat():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return fail('请求内容必须为 JSON 对象。')
        device_id = body.get('deviceId', '')
        if device_id != g.player_device['id']:
            return fail('请求中的设备编号不一致。', 403, 'device_token_invalid')
        protocol = body.get('protocolVersion', 1)
        if type(protocol) is not int or protocol != 3:
            return fail('请安装支持设备绑定的新版电视应用。', 426, 'upgrade_required')
        numeric = {}
        for key in ('appliedRevision', 'downloadedRevision', 'cleanupRevision', 'cacheBytes', 'freeBytes',
                    'cacheLimitBytes', 'cacheReserveBytes', 'cleanupCompletedRequest'):
            default = 0 if key == 'cleanupCompletedRequest' else -1
            value = body.get(key, default) if protocol >= 2 else default
            if type(value) is not int or not default <= value <= 2 ** 63 - 1:
                return fail('设备状态数值不正确。')
            numeric[key] = value
        state = body.get('cleanupState', 'pending') if protocol >= 2 else 'pending'
        if state not in ('pending', 'running', 'complete', 'error', 'blocked'):
            return fail('缓存清理状态不正确。')
        values = {'id': device_id, 'name': str(body.get('name', '电视'))[:100],
                  'version': str(body.get('version', ''))[:32], 'status': str(body.get('status', ''))[:100],
                  'current_item': str(body.get('currentItem', ''))[:200], 'last_seen': time.time(),
                  'protocolVersion': protocol, **numeric, 'cleanupState': state,
                  'cleanupError': str(body.get('cleanupError', ''))[:500] if protocol >= 2 else ''}
        with request_db() as c:
            previous = g.player_device
            binding_version = previous['binding_version']
            command = previous['cleanupRequest']
            # A client cannot acknowledge a command the server has never issued.
            if values['cleanupCompletedRequest'] > command:
                values['cleanupCompletedRequest'] = previous['cleanupCompletedRequest'] if previous else 0
            names = list(values)
            c.execute('UPDATE devices SET ' + ','.join(name + '=?' for name in names if name != 'id') +
                      ' WHERE id=? AND binding_version=?',
                      [values[name] for name in names if name != 'id'] + [device_id, binding_version])
            join_current_target(c, {'id': device_id, 'name': values['name'], 'binding_version': binding_version})
            # A late receipt can acknowledge only an already registered target of that exact version.
            # It never adds the device to older publications or revives their superseded state.
            now = time.time()
            if protocol >= 2:
                for field, column in (('downloadedRevision', 'downloaded_at'), ('appliedRevision', 'applied_at')):
                    reported_revision = numeric[field]
                    if reported_revision > 0:
                        c.execute(f'UPDATE publication_targets SET {column}=COALESCE({column},?) '
                                  'WHERE revision=? AND device_id=? AND binding_version=? AND withdrawn_at IS NULL',
                                  (now, reported_revision, device_id, binding_version))
        return jsonify(ok=True, cleanupRequest=command, bindingVersion=binding_version)

    @app.get('/media/<item_id>/<filename>')
    def media_file(item_id, filename):
        if session.get('admin'):
            return serve_media(item_id, filename)
        return player_media(item_id, filename)

    @player
    def player_media(item_id, filename):
        return serve_media(item_id, filename)

    def serve_media(item_id, filename):
        with request_db() as c:
            row = c.execute("SELECT pages FROM media WHERE id=? AND status='ready'", (item_id,)).fetchone()
        if not row or not any(x['url'] == f'/media/{item_id}/{filename}' for x in json.loads(row['pages'])):
            return fail('素材不存在或尚未完成转换。', 404)
        response = send_from_directory(media_root / item_id, filename, conditional=True, max_age=3600)
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    def stop_conversions():
        stopping.set()
        with worker_lock:
            worker_state['closed'] = True
        executor.shutdown(wait=True, cancel_futures=True)

    app.extensions['lanscreen'] = {'db': db, 'setting': setting, 'executor': executor, 'convert': convert, 'data': data,
                                  'stop': stop_conversions, 'worker_state': worker_state,
                                  'schedule': schedule_conversion, 'conversion_command': conversion_command}
    if not testing:
        with db() as c:
            pending = [r['id'] for r in c.execute("SELECT id FROM media WHERE status IN ('queued','processing')")]
        for item_id in pending:
            schedule_conversion(item_id)
    return app


if __name__ == '__main__':
    from waitress import serve
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    application = create_app()
    code = application.extensions['lanscreen']['setting']('setup_code')
    if code:
        print('\n首次设置代码：' + code + '（仅用于创建管理员）', flush=True)
    port = int(os.environ.get('PORT', '8765'))
    print(f'局域宣传屏已启动：http://127.0.0.1:{port}', flush=True)
    print(f'同一局域网设备请访问：http://本机局域网IP:{port}', flush=True)
    def shutdown_signal(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, shutdown_signal)
    try:
        serve(application, host=os.environ.get('HOST', '0.0.0.0'), port=port, threads=8,
              max_request_body_size=512 * 1024 * 1024, channel_timeout=300)
    finally:
        application.extensions['lanscreen']['stop']()

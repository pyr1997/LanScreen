"""Bounded local conversion subprocesses. Never removes retained input/output files."""
from __future__ import annotations
import os
import errno
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time


class ConversionLimitError(ValueError):
    pass


def directory_bytes(directory: Path, exclude: Path | None = None, max_entries=30000):
    """Count regular files without following links into other directories."""
    total, count = 0, 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root = os.open(directory, flags)  # A missing root is an error, not an empty job.
    def walk(fd, folder):
        nonlocal total, count
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > max_entries:
                    raise ConversionLimitError('转换目录条目过多，请拆分素材后重试。')
                try:
                    info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # The converter is allowed to replace its own temporary files.
                if stat.S_ISDIR(info.st_mode):
                    try:
                        child = os.open(entry.name, flags, dir_fd=fd)
                    except OSError as error:
                        if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                            continue
                        raise
                    try:
                        walk(child, folder / entry.name)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and folder / entry.name != exclude:
                    total += info.st_size
    try:
        walk(root, directory)
        return total
    finally:
        os.close(root)


def _group_exists(group):
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False


def _stop_group(process, grace_seconds=2.0):
    """The process group was created by this invocation, never an existing user group."""
    group = process.pid
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        process.poll()  # Reap the direct child while waiting for remaining descendants.
        if not _group_exists(group):
            break
        time.sleep(0.05)
    if _group_exists(group):
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)
    deadline = time.monotonic() + 2.0
    while _group_exists(group) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _group_exists(group):
        raise RuntimeError('转换进程组未能在时限内回收，请检查容器进程状态。')


def run_bounded(command, *, timeout, check_budget, stdout_limit=1024 * 1024,
                stderr_limit=64 * 1024, check_interval=0.25):
    """Drain both pipes with bounded RAM and supervise owned descendants and disk budget.

    A task may overshoot a disk/output threshold by writes made between checks; limits
    are admission/monitoring thresholds, not a filesystem quota. No files are erased.
    """
    check_budget()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, start_new_session=True)
    selector = selectors.DefaultSelector()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    limits = {'stdout': stdout_limit, 'stderr': stderr_limit}
    started = time.monotonic()
    next_budget_check = started
    try:
        for name, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while True:
            if time.monotonic() - started > timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            now = time.monotonic()
            if now >= next_budget_check:
                check_budget()
                next_budget_check = now + check_interval
            for key, _ in selector.select(check_interval):
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = limits[key.data] - len(buffers[key.data])
                if remaining > 0:
                    buffers[key.data].extend(chunk[:remaining])
            if process.poll() is not None:
                # A launcher may have exited while one of its children still owns a pipe.
                if _group_exists(process.pid):
                    _stop_group(process)
                if not selector.get_map():
                    break
        check_budget()
        return subprocess.CompletedProcess(command, process.returncode,
                                           bytes(buffers['stdout']), bytes(buffers['stderr']))
    finally:
        try:
            if process.poll() is None or _group_exists(process.pid):
                _stop_group(process)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()

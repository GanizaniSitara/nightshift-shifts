"""Small process, lock, mailbox and path helpers the Go test suite uses as fixtures.

Copied unchanged from the retired Python launcher (now in the nightshift-shifts-python-archive repo),
so the tests no longer need the launcher itself. COMMANDS is the launcher's subcommand list, used to
enumerate the --help cases.
"""
import contextlib
import ctypes
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

SUITE_PYTHON = 'C:/miniconda3/envs/python312/python.exe'
MAILBOX_SCHEMA = '''CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, sender TEXT NOT NULL, sender_incarnation REAL,
    recipient TEXT NOT NULL, recipient_incarnation REAL, asg TEXT, body TEXT NOT NULL, created TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'stored')'''
COMMANDS = ('start', 'status', 'down', 'spawn', 'consult', 'loop', 'resume', 'land', 'claim', 'release', 'claims', 'msg', 'inbox', 'ack', 'build', 'log', 'floor', 'cleaner', 'warden', 'gaffer', 'statusline')


def terminal_windows():
    """Visible Windows Terminal top-level windows as {hwnd: title}."""
    user32 = ctypes.windll.user32
    found = {}

    def inspect(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == 'CASCADIA_HOSTING_WINDOW_CLASS':
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            found[int(hwnd)] = title.value
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(inspect), 0)
    return found


def process_start_epoch(pid):
    """Creation time of a process as epoch seconds (local clock), or None if it does not exist.

    None also when wmic does not answer in time: the loop died on 16 Oct from an uncaught
    TimeoutExpired here, and an unknown start time must mean "cannot verify", not "crash".
    """
    try:
        out = subprocess.run(['wmic', 'process', 'where', f'processid={pid}', 'get', 'creationdate'],
                             capture_output=True, timeout=15).stdout.decode('utf-8', 'replace').replace('\x00', '')
    except subprocess.TimeoutExpired:
        return None
    match = re.search(r'(\d{14})\.\d+', out)
    if not match:
        return None
    return time.mktime(time.strptime(match.group(1), '%Y%m%d%H%M%S'))


def pid_alive(pid, started=None):
    """True if this PID is a crew CLI process (claude.exe, node.exe, agy.exe, codex.exe or copilot.exe)
    started when the crew member was (CSV PID field compared whole).

    Windows reuses PIDs, so a dead crew member's PID can turn up on another process - even another
    Claude session - and make a finished shift look alive; the start time settles it.
    """
    out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                         capture_output=True, timeout=15).stdout.decode('utf-8', 'replace')
    for line in out.splitlines():
        if line.startswith('"'):
            fields = line.strip('"').split('","')
            if fields[1:2] == [str(pid)] and fields[0].lower() in ('claude.exe', 'node.exe', 'agy.exe',
                                                                   'codex.exe', 'copilot.exe'):
                if started is None:
                    return True
                created = process_start_epoch(pid)
                return created is None or abs(created - started) <= 300
    return False


def normalise_path(p):
    """Claims key for a file: absolute and case-folded, so C:/scripts/x.py and C:\\Scripts\\x.py are one key."""
    return os.path.normcase(str(Path(p).resolve()))


@contextlib.contextmanager
def claims_locked(state_dir, wait=5.0, stale=30.0):
    """Hold <state_dir>/claims.lock (O_EXCL) around a read-modify-write of claims.json.

    Two Coders claiming at once would otherwise both read the old file and one claim would be lost.
    """
    lock = Path(state_dir) / 'claims.lock'
    try:
        take_file_lock(lock, wait, stale)
    except TimeoutError:
        raise SystemExit(f'{lock} is still held after {wait:.0f}s; try again')
    try:
        yield
    finally:
        release_file_lock(lock)


def take_file_lock(lock, wait, stale):
    """Create `lock` with O_EXCL, retrying for `wait` seconds (then TimeoutError); the caller releases it with
    release_file_lock.

    A lock older than `stale` seconds is left over from a crashed holder and removed. On Windows a contender's
    create can fail with PermissionError instead of FileExistsError (the lock is delete-pending after its holder's
    unlink, or another contender's stat has it open): that is contention too, retried until `wait` runs out.
    """
    deadline = time.time() + wait
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return
        except FileExistsError:
            try:
                is_stale = time.time() - lock.stat().st_mtime > stale
            except OSError:
                continue   # released between open and stat: try again
            if is_stale:
                with contextlib.suppress(OSError):
                    lock.unlink()
                continue
            if time.time() > deadline:
                raise TimeoutError(f'{lock} is still held after {wait:.0f}s')
            time.sleep(0.1)
        except PermissionError:
            if time.time() > deadline:
                raise
            time.sleep(0.01)


def release_file_lock(lock, wait=2.0):
    """Unlink `lock`, retrying while Windows refuses with PermissionError (a contender's stat or create has it
    open for a moment). Giving up silently would leave the lock until it goes stale and time out every waiter."""
    deadline = time.time() + wait
    while True:
        try:
            lock.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.time() > deadline:
                return
            time.sleep(0.005)
        except OSError:
            return


@contextlib.contextmanager
def mailbox_guard(path, timeout_ms=10000):
    """Coordinate central-helper readers/writers without creating a lock file beside the mailbox."""
    key = hashlib.sha256(os.path.normcase(str(path.resolve())).encode('utf-8')).hexdigest()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, 'Global\\NightshiftMailbox-' + key)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    owned = False
    try:
        outcome = kernel.WaitForSingleObject(handle, timeout_ms)
        if outcome == 258:   # WAIT_TIMEOUT
            raise TimeoutError(f'mailbox {path} is still held after {timeout_ms / 1000:g}s')
        if outcome not in (0, 0x80):   # WAIT_OBJECT_0 or WAIT_ABANDONED: both grant ownership
            raise ctypes.WinError(ctypes.get_last_error())
        owned = True
        yield
    finally:
        if owned:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def mailbox(state_dir, create=True):
    """Open <state_dir>/mailbox.sqlite3 (WAL, busy timeout); yields None when create=False and it does not exist.

    The connection is always closed, so nothing keeps the file open (Windows cannot delete an open file).
    """
    path = Path(state_dir) / 'mailbox.sqlite3'
    with contextlib.ExitStack() as stack:
        if create:
            stack.enter_context(mailbox_guard(path))   # held through connection close/checkpoint
            target = path
        else:
            with mailbox_guard(path):
                if not path.exists():
                    yield None
                    return
                scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='nightshift-mailbox-')))
                target = scratch / path.name
                shutil.copy2(path, target)
                wal = path.with_name(path.name + '-wal')
                if wal.exists():
                    shutil.copy2(wal, target.with_name(target.name + '-wal'))
            # SQLite may rebuild WAL/SHM here; only the disposable copy is ever opened.
        conn = stack.enter_context(contextlib.closing(sqlite3.connect(target, timeout=10)))
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=10000')
        if create:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute(MAILBOX_SCHEMA)
            columns = {r[1] for r in conn.execute('PRAGMA table_info(messages)')}
            for name, sql_type in (('delivered_epoch', 'REAL'), ('coding_asg', 'TEXT'),
                                   ('source_dispatch_id', 'INTEGER'), ('reason', 'TEXT'), ('evidence', 'TEXT')):
                if name not in columns:
                    conn.execute(f'ALTER TABLE messages ADD COLUMN {name} {sql_type}')
            conn.commit()
        yield conn


def process_running(pid, started_epoch=None):
    """True if any process has this PID (any image name) and, given started_epoch, was created then (within 2s)."""
    if not pid:
        return False
    out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                         capture_output=True, timeout=15).stdout.decode('utf-8', 'replace')
    if not any(line.startswith('"') and line.strip('"').split('","')[1:2] == [str(pid)] for line in out.splitlines()):
        return False
    if started_epoch is None:
        return True
    created = process_start_epoch(pid)
    return created is None or abs(created - started_epoch) <= 2

"""Standalone Go CLI parity against the frozen Python read-only command oracle.

Run with NIGHTSHIFT_GO_EXE pointing at the built executable. The oracle's outputs are
recorded golden files (NGH-055): no oracle CLI runs at test time. Regeneration is
explicit and serial: testdata/record_goldens.py (NIGHTSHIFT_RECORD_GOLDENS=1) runs the
oracle, stores what it printed, and still compares the native binary against it.
"""
import importlib.util
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

import pytest


REPO = Path(__file__).resolve().parent
GOLDEN = REPO / 'testdata' / 'go_readonly' / 'golden.json'
GOLDENS = REPO / 'testdata' / 'goldens'
RECORD = os.environ.get('NIGHTSHIFT_RECORD_GOLDENS') == '1'
# In-process helpers only (named mutex, claims lock, path normaliser, schema, process probes):
# none of them runs the oracle's commands. The oracle CLI itself runs only while recording.
spec = importlib.util.spec_from_file_location('golden_oracle', REPO / 'nightshift_shifts.py')
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


# ---------------------------------------------------------------- frozen oracle outputs (NGH-055)

_frozen_calls = {}


def _golden_key():
    current = os.environ['PYTEST_CURRENT_TEST'].rsplit(' (', 1)[0]
    n = _frozen_calls.get(current, 0)
    _frozen_calls[current] = n + 1
    module, _, test = current.partition('::')
    test = re.sub(r'@[\w-]+$', '', test)   # xdist loadgroup appends @<group> to the node id
    return GOLDENS / (Path(module).stem + '.json'), f'{test}#{n}'


def _variants(name, path):
    """Every spelling of a fixture root the CLIs print: raw, posix, lock-key; cased; JSON-escaped 0-3 times."""
    p = str(path)
    forms = {'raw': p, 'posix': p.replace('\\', '/'), 'key': re.sub(r'[:\\/]', '_', p.lower())}
    out = []
    for form, text in forms.items():
        for case, cased in (('', text), ('lower', text.lower()), ('upper', text.upper()),
                            ('drive', text[:1].lower() + text[1:])):
            for level in range(4):
                escaped = cased
                for _ in range(level):
                    escaped = json.dumps(escaped, ensure_ascii=False)[1:-1]
                out.append((f'<{name}:{form}:{case}:{level}>', escaped))
    return out


def _substitutions(tmp_path, extra, more_roots=None):
    roots = [('TMP', tmp_path), ('BASE', tmp_path.parent), ('REPO', REPO)]
    # The worker's pytest base temp holds session fixtures (fake bin, helper builds).
    for ancestor in tmp_path.parents:
        if re.fullmatch(r'pytest-\d+|popen-gw\d+', ancestor.name):
            roots.append(('TEMPROOT', ancestor))
            break
    roots += list((more_roots or {}).items())
    seen, pairs = set(), []
    for name, root in roots:
        for path in dict.fromkeys([str(root), str(Path(root).resolve())]):
            for token, text in _variants(name, path):
                if text not in seen:
                    seen.add(text)
                    pairs.append((token, text, None))
    for name, value in (extra or {}).items():
        # Volatile numbers (live PIDs, start epochs) only where they stand alone.
        text = str(value)
        pairs.append((f'<{name}>', text, re.compile(r'(?<![\w.])' + re.escape(text) + r'(?!\w|\.\d)')))
    return sorted(pairs, key=lambda pair: -len(pair[1]))


def _rewrite(value, func):
    if isinstance(value, (str, bytes)):
        return func(value)
    if isinstance(value, tuple):
        return tuple(_rewrite(v, func) for v in value)
    if isinstance(value, list):
        return [_rewrite(v, func) for v in value]
    if isinstance(value, dict):
        return {_rewrite(k, func): _rewrite(v, func) for k, v in value.items()}
    return value


def _normalise(value, pairs):
    def func(data):
        for token, text, pattern in pairs:
            if isinstance(data, bytes):
                if pattern is None:
                    data = data.replace(text.encode('utf-8'), token.encode())
                else:
                    data = re.sub(pattern.pattern.encode('utf-8'), token.encode(), data)
            else:
                data = data.replace(text, token) if pattern is None else pattern.sub(token, data)
        return data
    return _rewrite(value, func)


def _denormalise(value, pairs):
    def func(data):
        for token, text, _ in pairs:
            if isinstance(data, bytes):
                data = data.replace(token.encode(), text.encode('utf-8'))
            else:
                data = data.replace(token, text)
        return data
    return _rewrite(value, func)


def _freeze(value):
    if isinstance(value, bytes):
        try:
            return {'bytes': value.decode('utf-8')}
        except UnicodeDecodeError:
            return {'hex': value.hex()}
    if isinstance(value, tuple):
        return {'tuple': [_freeze(v) for v in value]}
    if isinstance(value, list):
        return [_freeze(v) for v in value]
    if isinstance(value, dict):
        return {'dict': [[_freeze(k), _freeze(v)] for k, v in value.items()]}
    return value


def _thaw(value):
    if isinstance(value, list):
        return [_thaw(v) for v in value]
    if isinstance(value, dict):
        (kind, inner), = value.items()
        if kind == 'bytes':
            return inner.encode('utf-8')
        if kind == 'hex':
            return bytes.fromhex(inner)
        if kind == 'tuple':
            return tuple(_thaw(v) for v in inner)
        return {_thaw(k): _thaw(v) for k, v in inner}
    return value


class _Run:
    """What a frozen subprocess.CompletedProcess still offers: stdout, stderr, returncode."""
    def __init__(self, stdout, stderr, returncode):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def frozen(compute, tmp_path, extra=None, roots=None):
    """The oracle's value for this call site. Recording runs compute() (the oracle) and stores it with
    fixture roots (tmp_path, its parent, the worker's base temp, the repository, any named roots) and the
    named volatile values tokenised; otherwise the stored value is returned with today's roots and values
    put back. A CompletedProcess is frozen as stdout, stderr and returncode."""
    path, key = _golden_key()
    pairs = _substitutions(Path(tmp_path), extra, roots)
    if RECORD:
        value = compute()
        stored = ({'run': [value.stdout, value.stderr, value.returncode]}
                  if isinstance(value, subprocess.CompletedProcess) else {'value': value})
        goldens = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        goldens[key] = _freeze(_normalise(stored, pairs))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(goldens, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
        return value
    goldens = _golden_cache.get(path)
    if goldens is None:
        if not path.exists():
            pytest.fail(f'no golden file {path}; record with testdata/record_goldens.py')
        goldens = _golden_cache[path] = json.loads(path.read_text(encoding='utf-8'))
    if key not in goldens:
        pytest.fail(f'no golden {key} in {path.name}; record with testdata/record_goldens.py')
    stored = _denormalise(_thaw(goldens[key]), pairs)
    return _Run(*stored['run']) if 'run' in stored else stored['value']


_golden_cache = {}

# Change fixture locations only; command implementations remain the Python oracle.
# Calling discovery separately for each root preserves the default dual-root contract
# even though Python disables legacy discovery when its global SHIFTS_DIR is patched.
ORACLE_BOOT = r'''
import os, sys
from pathlib import Path
import nightshift_shifts as n
n.SOLID_DIR = Path(os.environ['NIGHTSHIFT_STATE_ROOT'])
n.SHIFTS_DIR = n.SOLID_DIR / 'shifts'
n.LOCKS_DIR = n.SOLID_DIR / 'locks'
n.LEGACY_DIR = Path(os.environ['NIGHTSHIFT_LEGACY_ROOT'])
Path.home = classmethod(lambda cls: Path(os.environ['NIGHTSHIFT_HOME']))
original_dirs, original_locks = n.shift_dirs, n.read_locks
def dirs():
    saved = n.SHIFTS_DIR
    try:
        first = original_dirs()
        n.SHIFTS_DIR = n.LEGACY_DIR / 'shifts'
        return sorted(first + original_dirs(), key=lambda p: p.stat().st_mtime)
    finally:
        n.SHIFTS_DIR = saved
def locks():
    saved = n.LOCKS_DIR
    try:
        first = original_locks()
        n.LOCKS_DIR = n.LEGACY_DIR / 'locks'
        return sorted(first + original_locks(), key=lambda item: item[0])
    finally:
        n.LOCKS_DIR = saved
n.shift_dirs, n.read_locks = dirs, locks
sys.argv = ['nightshift', *sys.argv[1:]]
n.main()
'''


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def make_world(base):
    root, legacy, home = (base / name for name in ('state space', 'legacy space', 'home'))
    a = root / 'shifts' / 'Nightshift-shift-alpha'
    b = legacy / 'shifts' / 'Nightshift-shift-beta'
    empty = root / 'shifts' / 'Nightshift-shift-empty'
    for d in (a, b, empty):
        d.mkdir(parents=True)
    members = [{'label': label, 'identity': 'mesh-' + label.lower(),
                'role': role, 'human_contact': label == 'Boss'}
               for label, role in [('Boss', 'Foreman'), ('Junior', 'Apprentice'),
                                   ('Senior', 'Coder'), ('QA', 'Tester')]]
    write_json(a / 'crew.json', {'crew': 'golden', 'members': members, 'slots': 3})
    for label, epoch in [('Boss', 100.25), ('Junior', 200.5), ('Senior', 300.75), ('QA', 400.0)]:
        write_json(a / (label + '.json'), {'pid': 2147483000, 'started': epoch,
                   'wrapper_pid': 2147483001, 'name': 'session-' + label.lower(),
                   'cwd': r'C:\work space\café', 'tool': 'codex'})
    write_json(a / 'slots' / '1.json', {'role': None, 'cmd_pid': 2147483000})
    write_json(a / 'slots' / '2.json', {'role': 'Junior', 'spawned': 201.0, 'cmd_pid': 2147483000})
    (a / 'prompt.txt').write_text('  Café 🚀 "quote" \\path\nsecond line  ', encoding='utf-8')
    (a / 'requires.txt').write_text('DEPLOYED\n', encoding='utf-8')
    write_json(a / 'result.json', {'status': 'done', 'stage': 'tested'})
    write_json(b / 'result.json', ['invalid result'])
    write_json(a / 'claims.json', {r'C:\z path\Ω.py': {'role': 'Junior', 'time': 'first'},
                                  r'C:\a\".py': {'role': 'QA', 'time': 'second'}})
    (b / 'claims.json').write_text('["not an object"]', encoding='utf-8')
    write_json(root / 'locks' / 'z.json', {'shift': a.name, 'cwd': r'C:\locked folder'})
    write_json(legacy / 'locks' / 'a.json', {'shift': b.name, 'cwd': r'C:\old folder'})
    (root / 'locks' / 'bad.json').write_text('{broken', encoding='utf-8')
    events = [
        {'seq': 1, 'ts': '2026-10-16T01:02:03', 'event': 'start', 'ticket': None,
         'crew': 'golden', 'members': ['Boss', 'QA'], 'cwd': r'C:\a b'},
        {'seq': 2, 'ts': '2026-10-16T01:02:04', 'event': 'msg', 'id': 8,
         'sender': 'Boss', 'recipient': 'Junior', 'kind': 'assignment', 'asg': None,
         'body': 'Café 🚀\n tab\t spaced\u00a0words " \\ end'},
        {'seq': 3, 'ts': '2026-10-16T01:02:05', 'event': 'ack', 'id': 8,
         'by': 'QA', 'outcome': 'bad', 'reason': 'why Ω'},
        {'seq': 4, 'ts': '2026-10-16T01:02:06', 'event': 'sent', 'id': 8},
        {'seq': 5, 'ts': '2026-10-16T01:02:07', 'event': 'spawn', 'role': 'QA',
         'slot': 2, 'tool': 'codex', 'identity': 'mesh-qa'},
        {'seq': 6, 'ts': '2026-10-16T01:02:08', 'event': 'custom',
         'z': {'snow': '雪', 'ok': True}, 'a': [None, 1.0, '<>&']},
        {'seq': 7, 'ts': '2026-10-16T01:02:09', 'event': 'down',
         'stopped': ['Junior'], 'lock_released': []},
    ]
    raw = b'\r\n'.join(json.dumps(e, ensure_ascii=False).encode('utf-8') for e in events)
    (a / 'bus.jsonl').write_bytes(raw + b'\r\nnot-json\r\n[]\r\n{}\r\n\xff\n\n{"seq":99')
    make_mailbox(a)
    # Explicit distinct mtimes make latest/order independent of filesystem timing.
    for i, d in enumerate((a, b, empty)):
        os.utime(d, (1700000000 + i, 1700000000 + i))
    return {'NIGHTSHIFT_STATE_ROOT': str(root), 'NIGHTSHIFT_LEGACY_ROOT': str(legacy),
            'NIGHTSHIFT_HOME': str(home), 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'}


def make_mailbox(d, live_wal=False):
    conn = sqlite3.connect(d / 'mailbox.sqlite3')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(oracle.MAILBOX_SCHEMA)
    for name, kind in [('delivered_epoch', 'REAL'), ('coding_asg', 'TEXT'),
                       ('source_dispatch_id', 'INTEGER'), ('reason', 'TEXT'), ('evidence', 'TEXT')]:
        conn.execute(f'ALTER TABLE messages ADD COLUMN {name} {kind}')
    epochs = {'Boss': 100.25, 'Junior': 200.5, 'Senior': 300.75, 'QA': 400.0}

    def row(kind, sender, recipient, asg, body='body', state='sent', **extra):
        values = dict(kind=kind, sender=sender, sender_incarnation=epochs[sender],
                      recipient=recipient, recipient_incarnation=epochs[recipient], asg=asg,
                      body=body, created='2026-10-16 00:00:00', state=state)
        values.update(extra)
        conn.execute('INSERT INTO messages (' + ','.join(values) + ') VALUES (' +
                     ','.join('?' for _ in values) + ')', list(values.values()))

    row('assignment', 'Boss', 'Junior', 'code-a', 'Café 🚀 "x" \\path', delivered_epoch=1.25)
    row('assignment', 'Boss', 'QA', 'verify-a', coding_asg='code-a')
    row('result', 'QA', 'Boss', 'verify-a', 'TESTED-FAIL first')
    row('assignment', 'Boss', 'QA', 'verify-b', coding_asg='code-a')
    row('result', 'QA', 'Boss', 'verify-b', 'TESTED-FAIL second')
    row('note', 'Junior', 'Boss', 'code-a', 'APPRENTICE-NEEDS-SENIOR detail', state='stored')
    row('note', 'Boss', 'Junior', None, 'stale input', recipient_incarnation=199.0)
    row('note', 'Boss', 'Junior', None, 'acked input', state='acknowledged')
    row('note', 'Junior', 'Senior', None, 'unsent output', state='stored')
    row('assignment', 'Boss', 'Junior', 'old-code', recipient_incarnation=199.0)
    row('result', 'Junior', 'Boss', 'old-code', 'done', sender_incarnation=199.0)
    row('assignment', 'Boss', 'Junior', 'moved')
    row('uplift', 'Boss', 'Senior', 'moved', source_dispatch_id=12, reason='needs-senior',
        evidence='[{"id": 12}]')
    row('note', 'Boss', 'Junior', None, 'stored input', state='stored')
    conn.commit()
    if live_wal:
        return conn
    conn.close()


def snapshot(root):
    return {str(p.relative_to(root)): None if p.is_dir() else (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob('*')}


def run_cli(args, env, go=False, timeout=30):
    if go:
        exe = Path(os.environ.get('NIGHTSHIFT_GO_EXE', REPO / 'build' / 'nightshift.exe'))
        if not exe.is_file():
            pytest.fail(f'standalone executable missing: {exe}; build it before parity verification')
        command = [str(exe), *args]
    else:
        require_recording()
        command = [sys.executable, '-c', ORACLE_BOOT, *args]
    return subprocess.run(command, cwd=REPO, env={**os.environ, **env},
                          capture_output=True, timeout=timeout)


def require_recording():
    if not RECORD:
        pytest.fail('the Python oracle runs only while recording goldens (testdata/record_goldens.py)')


def result(run):
    return {'stdout': run.stdout.decode('utf-8'), 'stderr': run.stderr.decode('utf-8'),
            'exit': run.returncode}


def normalized(value, env):
    text = json.dumps(value, ensure_ascii=False)
    for name, path in env.items():
        if not name.startswith('NIGHTSHIFT_'):
            continue
        # stdout JSON embeds JSON escapes inside the golden record's own JSON.
        for candidate in (json.dumps(path)[1:-1], path):
            text = text.replace(json.dumps(candidate)[1:-1], '<' + name + '>')
    return json.loads(text)


CASES = {
    'status-live-filter': ['status'],
    'status-all-roots': ['status', '--all'],
    'claims-unicode-order': ['claims', 'alpha'],
    'claims-legacy-invalid': ['claims', 'beta'],
    'claims-latest-missing': ['claims', 'latest'],
    'inbox-junior': ['inbox', 'alpha', 'junior'],
    'inbox-session-alias': ['inbox', 'alpha', 'session-junior'],
    'inbox-mesh-alias': ['inbox', 'alpha', 'mesh-junior'],
    'inbox-uplift-foreman': ['inbox', 'alpha', 'Boss'],
    'inbox-no-mailbox': ['inbox', 'empty', 'Foreman'],
    'inbox-unknown-role': ['inbox', 'alpha', 'outsider'],
    'missing-shift': ['claims', 'missing'],
    'log-human': ['log', 'alpha'],
    'log-json': ['log', 'alpha', '--json'],
    'log-since': ['log', 'alpha', '--since', '3'],
    'log-json-since': ['log', 'alpha', '--json', '--since=3'],
    'log-follow-down-filtered': ['log', 'alpha', '-f', '--since', '999'],
    'log-no-bus': ['log', 'beta'],
    'error-status-extra': ['status', '--bogus'],
    'error-claims-required': ['claims'],
    'error-inbox-role-required': ['inbox', 'alpha'],
    'error-log-since-type': ['log', 'alpha', '--since', 'bad'],
    'error-log-unknown': ['log', 'alpha', '--bogus'],
    'help-status': ['status', '--help'],
    'help-claims': ['claims', '--help'],
    'help-inbox': ['inbox', '--help'],
    'help-log': ['log', '--help'],
}


@pytest.mark.parametrize('name', CASES)
def test_go_matches_python_golden(tmp_path, name):
    env = make_world(tmp_path)
    before = snapshot(tmp_path)
    if RECORD:
        expected = result(run_cli(CASES[name], env))
        assert snapshot(tmp_path) == before, 'Python oracle mutated read-only fixtures'
        goldens = json.loads(GOLDEN.read_text(encoding='utf-8')) if GOLDEN.exists() else {}
        goldens[name] = normalized(expected, env)
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(goldens, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    actual = result(run_cli(CASES[name], env, go=True))
    assert normalized(actual, env) == json.loads(GOLDEN.read_text(encoding='utf-8'))[name]
    assert snapshot(tmp_path) == before, 'Go command mutated read-only fixtures'


@pytest.mark.parametrize('ref', ['latest', 'same', 'absent'])
def test_go_shift_resolution_errors_and_ambiguity(tmp_path, ref):
    env = make_world(tmp_path)
    for name in ('one-same', 'two-same'):
        (Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / name).mkdir()
    if ref == 'latest':
        # Test the no-shifts error, rather than whichever temp directory is latest.
        env['NIGHTSHIFT_STATE_ROOT'] = str(tmp_path / 'not present')
        env['NIGHTSHIFT_LEGACY_ROOT'] = str(tmp_path / 'also absent')
    before = snapshot(tmp_path)
    expected = frozen(lambda: result(run_cli(['claims', ref], env)), tmp_path)
    assert result(run_cli(['claims', ref], env, go=True)) == expected
    assert snapshot(tmp_path) == before


def test_go_reads_committed_wal_without_mutating_source(tmp_path):
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-empty'
    shutil.copyfile(d.parent / 'Nightshift-shift-alpha' / 'crew.json', d / 'crew.json')
    shutil.copyfile(d.parent / 'Nightshift-shift-alpha' / 'Boss.json', d / 'Boss.json')
    writer = make_mailbox(d, live_wal=True)
    try:
        assert (d / 'mailbox.sqlite3-wal').stat().st_size > 0
        before = snapshot(tmp_path)
        expected = frozen(lambda: result(run_cli(['inbox', 'empty', 'Boss'], env)), tmp_path)
        assert json.loads(expected['stdout'])['uplift_counts']['total'] == 1
        expected_status = frozen(lambda: result(run_cli(['status', '--all'], env)), tmp_path)
        assert result(run_cli(['inbox', 'empty', 'Boss'], env, go=True)) == expected
        assert result(run_cli(['status', '--all'], env, go=True)) == expected_status
        assert snapshot(tmp_path) == before
    finally:
        writer.close()


@pytest.mark.parametrize('raw_json', [False, True])
def test_go_follow_reads_appended_complete_lines_and_stops_at_down(tmp_path, raw_json):
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-empty'
    bus = d / 'bus.jsonl'
    args = ['log', 'empty', '--follow'] + (['--json'] if raw_json else [])
    def observe(go):
        bus.unlink(missing_ok=True)
        exe = Path(os.environ.get('NIGHTSHIFT_GO_EXE', REPO / 'build' / 'nightshift.exe'))
        command = [str(exe), *args] if go else [sys.executable, '-c', ORACLE_BOOT, *args]
        proc = subprocess.Popen(command, cwd=REPO, env={**os.environ, **env},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # Wait for the flushed missing-file warning before creating the bus.
            received = queue.Queue()
            threading.Thread(target=lambda: received.put(proc.stderr.readline()), daemon=True).start()
            warning = received.get(timeout=8)
            assert b'no bus yet:' in warning
            bus.write_bytes(b'{"seq":1,"event":"msg","body":"part')
            time.sleep(1.2)
            with bus.open('ab') as f:
                f.write(b'ial"}\n{"seq":2,"event":"down"}\n')
            stdout, stderr = proc.communicate(timeout=8)
            return stdout, warning + stderr, proc.returncode
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    expected = frozen(lambda: observe(False), tmp_path)
    assert observe(True) == expected


def test_go_standalone_from_foreign_cwd_without_python_on_path(tmp_path):
    env = make_world(tmp_path)
    exe = Path(os.environ.get('NIGHTSHIFT_GO_EXE', REPO / 'build' / 'nightshift.exe'))
    isolated = tmp_path / 'foreign working directory'
    isolated.mkdir()
    copied = isolated / 'read-only executable.exe'
    shutil.copyfile(exe, copied)
    before = snapshot(tmp_path)
    restricted = {**os.environ, **env, 'PATH': str(Path(os.environ['SystemRoot']) / 'System32'),
                  'PYTHONPATH': '', 'PYTHONHOME': ''}
    for args in (['claims', 'alpha'], ['inbox', 'alpha', 'Boss'], ['log', 'alpha', '--json'],
                 ['status', '--all']):
        actual = subprocess.run([str(copied), *args], cwd=isolated, env=restricted,
                                capture_output=True, timeout=30)
        assert result(actual) == frozen(lambda: result(run_cli(args, env)), tmp_path)
    assert snapshot(tmp_path) == before


def test_go_log_respects_python_console_encoding_replacement(tmp_path):
    env = {**make_world(tmp_path), 'PYTHONIOENCODING': 'cp1252', 'PYTHONUTF8': '0'}
    expected = frozen(lambda: run_cli(['log', 'alpha'], env), tmp_path)
    actual = run_cli(['log', 'alpha'], env, go=True)
    assert expected.returncode == 0
    assert (actual.stdout, actual.stderr, actual.returncode) == (
        expected.stdout, expected.stderr, expected.returncode)


@contextlib.contextmanager
def junction_alias(alias, target):
    made = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(target)],
                          capture_output=True, timeout=10)
    assert made.returncode == 0, (made.stdout, made.stderr)
    try:
        assert alias.is_junction() and alias.resolve() == target.resolve()
        yield alias
    finally:
        # Remove only the verified junction, never recursively remove its target.
        if alias.is_junction():
            os.rmdir(alias)


@pytest.mark.parametrize('through_junction', [False, True])
def test_go_mailbox_waits_for_python_mutex_before_copying_live_wal(tmp_path, through_junction):
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-empty'
    shutil.copyfile(d.parent / 'Nightshift-shift-alpha' / 'crew.json', d / 'crew.json')
    shutil.copyfile(d.parent / 'Nightshift-shift-alpha' / 'Boss.json', d / 'Boss.json')
    writer = make_mailbox(d, live_wal=True)
    exe = Path(os.environ.get('NIGHTSHIFT_GO_EXE', REPO / 'build' / 'nightshift.exe'))
    proc = None
    try:
        alias = tmp_path / 'junction state alias'
        routed = (junction_alias(alias, Path(env['NIGHTSHIFT_STATE_ROOT'])) if through_junction
                  else contextlib.nullcontext(Path(env['NIGHTSHIFT_STATE_ROOT'])))
        with routed as go_root:
            go_env = {**env, 'NIGHTSHIFT_STATE_ROOT': str(go_root)}
            with oracle.mailbox_guard(d / 'mailbox.sqlite3'):
                proc = subprocess.Popen([str(exe), 'inbox', 'empty', 'Boss'], cwd=REPO,
                                        env={**os.environ, **go_env}, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                # The writer commits while retaining the same named mutex as Python writers.
                time.sleep(0.7)
                early_exit = proc.poll()
                assert early_exit is None, (
                    f'Go reader did not wait for Python mailbox mutex: exit={early_exit}, '
                    f'output={proc.communicate(timeout=2)}')
                writer.execute("UPDATE messages SET body = 'committed while mutex held' WHERE id = 3")
                writer.commit()
                before = snapshot(tmp_path)
            stdout, stderr = proc.communicate(timeout=15)
            expected = frozen(lambda: run_cli(['inbox', 'empty', 'Boss'], env), tmp_path)
            assert b'committed while mutex held' in expected.stdout, 'oracle must see committed fixture row'
            assert b'committed while mutex held' in stdout
            assert (stdout, stderr, proc.returncode) == (expected.stdout, expected.stderr, expected.returncode)
            assert snapshot(tmp_path) == before
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        writer.close()


def test_go_mailbox_mutex_timeout_is_nonzero_and_preserves_source(tmp_path):
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-alpha'
    before = snapshot(tmp_path)
    with oracle.mailbox_guard(d / 'mailbox.sqlite3'):
        started = time.monotonic()
        actual = run_cli(['inbox', 'alpha', 'Boss'], env, go=True, timeout=16)
        elapsed = time.monotonic() - started
        assert actual.returncode == 1, 'mutex timeout was swallowed as a successful empty mailbox'
        assert actual.stdout == b'' and actual.stderr
        assert elapsed >= 9, (
            f'Go did not wait for the shared 10-second mutex timeout: elapsed={elapsed}, '
            f'stderr={actual.stderr!r}')
    assert snapshot(tmp_path) == before


def existing_pid(image):
    output = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ' + image, '/FO', 'CSV', '/NH'],
                            capture_output=True, timeout=15).stdout.decode('utf-8', 'replace')
    return next((int(fields[1]) for fields in csv.reader(io.StringIO(output))
                 if len(fields) > 1 and fields[0].lower() == image.lower()), None)


def test_go_status_rejects_python_image_and_recognizes_live_cmd_slots(tmp_path):
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-alpha'
    cmd_pid = existing_pid('cmd.exe')
    assert cmd_pid is not None, 'verification requires an existing cmd shell; do not launch one'
    assert oracle.process_running(cmd_pid)
    write_json(d / 'Boss.json', {'pid': os.getpid(), 'tool': 'codex'})
    write_json(d / 'slots' / '1.json', {'role': None, 'cmd_pid': cmd_pid})
    write_json(d / 'slots' / '2.json', {'role': 'Junior', 'cmd_pid': cmd_pid})
    expected = frozen(lambda: run_cli(['status', '--all'], env), tmp_path,
                      {'PYTEST_PID': os.getpid(), 'CMD_PID': cmd_pid})
    alpha = next(r for r in json.loads(expected.stdout) if r['shift'] == d.name)
    assert alpha['roles']['Boss']['alive'] is False
    assert [s['state'] for s in alpha['slots']] == ['empty', 'dead', 'gone']
    assert result(run_cli(['status', '--all'], env, go=True)) == result(expected)


def test_go_status_rejects_existing_codex_with_stale_start_epoch(tmp_path):
    pid = existing_pid('codex.exe')
    if pid is None or oracle.process_start_epoch(pid) is None:
        pytest.skip('no existing codex PID with independently readable creation time')
    assert oracle.pid_alive(pid, 1.0) is False
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-alpha'
    write_json(d / 'Boss.json', {'pid': pid, 'started': 1.0, 'tool': 'codex'})
    expected = frozen(lambda: run_cli(['status', '--all'], env), tmp_path, {'CODEX_PID': pid})
    alpha = next(r for r in json.loads(expected.stdout) if r['shift'] == d.name)
    assert alpha['roles']['Boss']['alive'] is False
    assert result(run_cli(['status', '--all'], env, go=True)) == result(expected)


@pytest.mark.parametrize('history,reasons', [
    ('same-owner-retry', ['tester-failed-twice']),
    ('handback', []),
    ('new-incarnation', []),
    ('duplicate-verdict', []),
])
def test_go_uplift_failures_obey_ownership_and_distinct_verification_boundaries(tmp_path, history, reasons):
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-alpha'
    with sqlite3.connect(d / 'mailbox.sqlite3') as conn:
        def dispatch(recipient='Junior', incarnation=200.5):
            conn.execute('INSERT INTO messages (kind,sender,sender_incarnation,recipient,'
                         'recipient_incarnation,asg,body,created,state) '
                         'VALUES (?,?,?,?,?,?,?,?,?)',
                         ('assignment', 'Boss', 100.25, recipient, incarnation, 'code-a',
                          'retry', '2026-10-16 00:00:00', 'sent'))
        if history == 'handback':
            dispatch('Senior', 300.75)
        if history == 'new-incarnation':
            info = json.loads((d / 'Junior.json').read_text(encoding='utf-8'))
            info['started'] = 201.0
            write_json(d / 'Junior.json', info)
            dispatch(incarnation=201.0)
        else:
            dispatch()
        if history == 'duplicate-verdict':
            # Two reports on verify-a count once; verify-b no longer has a failing report.
            conn.execute("UPDATE messages SET asg='verify-a' WHERE id=5")
    before = snapshot(tmp_path)
    expected = frozen(lambda: run_cli(['inbox', 'alpha', 'Boss'], env), tmp_path)
    due = [r for r in json.loads(expected.stdout)['uplift_due'] if r['asg'] == 'code-a']
    assert (due[0]['reasons'] if due else []) == reasons
    assert result(run_cli(['inbox', 'alpha', 'Boss'], env, go=True)) == result(expected)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('broken', ['not-sqlite', 'missing-messages-table'])
@pytest.mark.parametrize('args', [['inbox', 'empty', 'Foreman'], ['status', '--all']])
def test_go_sqlite_read_errors_cannot_be_silent_empty_mailboxes(tmp_path, broken, args):
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-empty'
    db = d / 'mailbox.sqlite3'
    if broken == 'not-sqlite':
        db.write_bytes(b'this is not a SQLite database\n')
    else:
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE unrelated (id INTEGER)')
    before = snapshot(tmp_path)
    expected = frozen(lambda: run_cli(args, env), tmp_path)
    assert expected.returncode == 1 and expected.stdout == b'' and expected.stderr
    actual = run_cli(args, env, go=True)
    assert actual.returncode == expected.returncode, 'SQLite read failure was swallowed'
    assert actual.stdout == b'' and actual.stderr, 'failure must not print an empty mailbox/status'
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('args', [
    ['inbox'],
    ['claims', '--', 'alpha'],
    ['inbox', 'alpha', '--bogus'],
    ['log', 'alpha', '--si', '3'],
])
def test_go_cli_parser_matches_required_arguments_terminator_and_abbreviations(tmp_path, args):
    env = make_world(tmp_path)
    before = snapshot(tmp_path)
    expected = frozen(lambda: run_cli(args, env), tmp_path)
    assert result(run_cli(args, env, go=True)) == result(expected)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('sequence', ['2.5', 'true'])
def test_go_log_since_matches_python_integer_type_filter(tmp_path, sequence):
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-empty'
    (d / 'bus.jsonl').write_text('{"seq":' + sequence + ',"event":"custom"}\n', encoding='utf-8')
    args = ['log', 'empty', '--json', '--since', '3']
    expected = frozen(lambda: run_cli(args, env), tmp_path)
    assert (expected.stdout != b'') is (sequence == '2.5')
    assert result(run_cli(args, env, go=True)) == result(expected)


@pytest.mark.parametrize('raw', [
    '{"duplicate":{"role":"old"},"duplicate":{"role":"new"}}',
    '{"exponent":1e0}',
])
def test_go_claims_matches_python_duplicate_keys_and_float_serialization(tmp_path, raw):
    env = make_world(tmp_path)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-empty'
    (d / 'claims.json').write_text(raw, encoding='utf-8')
    before = snapshot(tmp_path)
    args = ['claims', 'empty']
    assert result(run_cli(args, env, go=True)) == frozen(lambda: result(run_cli(args, env)), tmp_path)
    assert snapshot(tmp_path) == before


if __name__ == '__main__':
    raise SystemExit('record goldens with testdata/record_goldens.py')

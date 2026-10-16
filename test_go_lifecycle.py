"""Lifecycle parity against Python, confined to disposable roots and Git repos.

The oracle and native binary receive the SAME restored paths and deterministic
Git identities/dates. Compare CLI bytes, state-file bytes, and logical Git state;
only freshly generated wall-clock timestamps are normalized. Never put a real
crew PID or terminal HWND in a teardown fixture. The codex.exe fixture below is
our own copied nightshift binary, not the installed coding agent.
"""
import concurrent.futures
import contextlib
import ctypes
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time

import pytest

from test_go_readonly import ORACLE_BOOT, REPO, oracle, snapshot, write_json
from test_go_write_commands import stable_bytes, world_state


SHIFT = 'Nightshift-shift-lifecycle-fixture'
DEAD_PID = 2147483000
INVALID_HWND = 0x7FFFFFFF
BOOT = ORACLE_BOOT.replace('n.main()', "n.FLOOR_FILE = n.SOLID_DIR / 'floor.json'\nn.main()")
NOW = re.compile(rb'202[0-9]-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2})?')
ORACLE_ONLY = os.environ.get('NIGHTSHIFT_LIFECYCLE_ORACLE_ONLY') == '1'


def stable(data):
    def stamp(match):
        value = datetime.datetime.fromisoformat(match[0].decode())
        return b'<wall-clock>' if abs(time.time() - value.timestamp()) < 180 else match[0]
    return stable_bytes(NOW.sub(stamp, data))


def exe_path():
    return Path(os.environ.get('NIGHTSHIFT_GO_EXE', REPO / 'build' / 'nightshift.exe'))


def cli(args, env, native, timeout=45):
    command = [str(exe_path()), *args] if native else [sys.executable, '-c', BOOT, *args]
    assert not native or exe_path().is_file(), 'compile native binary before lifecycle verification'
    return subprocess.run(command, cwd=REPO, env={**os.environ, **env},
                          capture_output=True, timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW)


def git(repo, *args, check=True):
    proc = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=30,
                          env={**os.environ, **git_env()}, creationflags=subprocess.CREATE_NO_WINDOW)
    if check:
        assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc.stdout.decode('utf-8', 'replace').strip() if check else proc


def git_env():
    return {'GIT_AUTHOR_NAME': 'Lifecycle Tester', 'GIT_AUTHOR_EMAIL': 'tester@example.invalid',
            'GIT_COMMITTER_NAME': 'Lifecycle Tester', 'GIT_COMMITTER_EMAIL': 'tester@example.invalid',
            'GIT_AUTHOR_DATE': '2026-01-02T03:04:05+0000',
            'GIT_COMMITTER_DATE': '2026-01-02T03:04:05+0000', 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}


def commit(repo, name, content, message='fixture change'):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode())
    git(repo, 'add', '--', name)
    git(repo, 'commit', '-qm', message)
    return git(repo, 'rev-parse', 'HEAD')


def world(base, worktree=True):
    root, legacy, home = (base / p for p in ('state space', 'legacy space', 'home'))
    d = root / 'shifts' / SHIFT
    d.mkdir(parents=True)
    legacy.mkdir()
    env = {'NIGHTSHIFT_STATE_ROOT': str(root), 'NIGHTSHIFT_LEGACY_ROOT': str(legacy),
           'NIGHTSHIFT_HOME': str(home), 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1', **git_env()}
    repo = base / 'canonical café repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    for key, value in [('core.autocrlf', 'false'), ('commit.gpgsign', 'false'),
                       ('core.hooksPath', str(base / 'no hooks'))]:
        git(repo, 'config', key, value)
    commit(repo, 'shared.txt', 'original\n', 'initial')
    base_sha = commit(repo, 'notes.txt', 'notes\n', 'notes')
    work = base / 'worktree space Ω'
    info = {'repo_root': str(repo), 'worktree_dir': str(work), 'branch': 'shift/lifecycle',
            'target_branch': 'main', 'base_sha': base_sha, 'created_at': '2026-01-01 00:00:00'}
    if worktree:
        git(repo, 'worktree', 'add', '-qb', info['branch'], str(work), 'main')
        write_json(d / 'worktree.json', info)
    write_json(d / 'crew.json', {'members': [{'label': 'Foreman', 'role': 'Foreman',
               'identity': 'fixture-foreman', 'human_contact': True}], 'cwd': str(work if worktree else repo)})
    write_json(d / 'Foreman.json', {'pid': DEAD_PID, 'wrapper_pid': DEAD_PID + 1,
               'name': 'fixture-foreman', 'cwd': str(work if worktree else repo), 'tool': 'codex'})
    (d / 'prompt.txt').write_text('Lifecycle café fixture\nsecond line', encoding='utf-8')
    write_json(d / 'result.json', {'status': 'done', 'stage': 'tested', 'summary': 'fixture complete',
               'changed': ['shared.txt'], 'evidence': [{'stage': 'tested', 'what': 'fixture', 'outcome': 'passed'}]})
    (d / 'requires.txt').write_text('tested\n')
    # Suppress close_target's legacy bare-Foreman/Coder title fallback. Those
    # titles could belong to an unrelated real window on the testing desktop.
    assert not ctypes.windll.user32.IsWindow(ctypes.c_void_p(INVALID_HWND))
    write_json(d / 'launch.json', {'hwnd': INVALID_HWND})
    return env, d, repo, work, info


def evidence(d, work, **overrides):
    record = {'n': 1, 'head': git(work, 'rev-parse', 'HEAD'), 'cwd': str(work),
              'status': 'passed', 'exit': 0, 'dirty': False, 'cmd': 'fixture suite'}
    record.update(overrides)
    write_json(d / 'builds' / f"{record['n']}.json", record)


def repo_lock(env, repo):
    key = re.sub(r'[:\\/]', '_', str(repo.resolve()).lower()) + '.json'
    return Path(env['NIGHTSHIFT_STATE_ROOT']) / 'locks' / key


def observed_state(base, env, repo, work):
    files = {}
    for name in ('NIGHTSHIFT_STATE_ROOT', 'NIGHTSHIFT_LEGACY_ROOT'):
        root = Path(env[name])
        state_files, rows = world_state(root)
        files[name] = ({p: stable(data) for p, data in state_files.items()}, rows)
    logical = {}
    for folder in (repo, work):
        key = folder.name
        logical[key] = {'exists': folder.exists(), 'files': {}}
        if folder.exists():
            logical[key]['files'] = {str(p.relative_to(folder)): p.read_bytes()
                                     for p in folder.rglob('*')
                                     if p.is_file() and '.git' not in p.relative_to(folder).parts}
        if (folder / '.git').exists():
            logical[key]['git'] = [git(folder, *args) for args in (
                ('show-ref',), ('status', '--porcelain=v1', '--untracked-files=all'),
                ('diff', '--binary'), ('diff', '--cached', '--binary'),
                ('worktree', 'list', '--porcelain'), ('log', '--all', '--format=%H %s'))]
            logical[key]['rebase'] = any(Path(git(folder, 'rev-parse', '--git-path', p)).exists()
                                        for p in ('rebase-merge', 'rebase-apply'))
    return files, logical


def parity(tmp_path, prepare, commands, codes, inspect=None, worktree=True):
    base = tmp_path / 'private lifecycle fixture'
    observed = []
    for native in ([False] if ORACLE_ONLY else [False, True]):
        if base.exists():
            assert base.resolve().is_relative_to(tmp_path.resolve())
            remove_fixture(base, tmp_path)
        env, d, repo, work, info = world(base, worktree)
        prepare(env, d, repo, work, info)
        actual = []
        steps = commands(env, d, repo, work, info) if callable(commands) else commands
        for args in steps:
            proc = cli(args, env, native)
            actual.append((stable(proc.stdout), stable(proc.stderr), proc.returncode))
        assert [p[2] for p in actual] == codes, actual
        if inspect:
            inspect(env, d, repo, work, info, actual)
        observed.append((actual, observed_state(base, env, repo, work)))
    if not ORACLE_ONLY:
        for i, (actual, expected) in enumerate(zip(observed[1][0], observed[0][0])):
            for label, a, e in zip(('stdout', 'stderr', 'exit'), actual, expected):
                assert_equal_bytes(a, e, f'command {i} {label}')
        for root in observed[0][1][0]:
            actual_files, actual_rows = observed[1][1][0][root]
            expected_files, expected_rows = observed[0][1][0][root]
            assert actual_files.keys() == expected_files.keys()
            for name in expected_files:
                assert_equal_bytes(actual_files[name], expected_files[name], f'{root}/{name}')
            assert actual_rows == expected_rows
        assert observed[1][1][1] == observed[0][1][1]
    return observed[0]


def noop(*args):
    pass


def assert_equal_bytes(actual, expected, label):
    if actual == expected:
        return
    if isinstance(actual, bytes) and isinstance(expected, bytes):
        offset = next((i for i, (a, e) in enumerate(zip(actual, expected)) if a != e),
                      min(len(actual), len(expected)))
        pytest.fail(f'{label} byte {offset}: expected={expected[max(0, offset-50):offset+150]!r}; '
                    f'actual={actual[max(0, offset-50):offset+150]!r}')
    assert actual == expected, label


def remove_fixture(path, root):
    """Git object files are read-only on Windows; never remove outside our temp root."""
    assert path.resolve().is_relative_to(root.resolve()) and path.resolve() != root.resolve()
    def readonly(function, failed_path, exception):
        os.chmod(failed_path, stat.S_IWRITE)
        function(failed_path)
    shutil.rmtree(path, onexc=readonly)


@pytest.mark.parametrize('command', [
    ['land'], ['land', '--help'], ['land', 'fixture', '--bogus'],
    ['land', 'fixture', '--target-branch'], ['land', '--ke', 'fixture'],
    ['down', '--help'], ['down', 'fixture', '--hold', '--close'],
    ['down', 'fixture', '--close', '--hold'], ['down', '--bogus'],
    ['cleaner', '--help'], ['cleaner', '--bogus'], ['cleaner', 'fixture'],
])
def test_go_lifecycle_parser_bytes(tmp_path, command):
    # Codes as well as diagnostic/help bytes are taken from the unchanged oracle.
    base = tmp_path / 'parser'
    env, d, repo, work, info = world(base, False)
    expected = cli(command, env, False)
    before = snapshot(base)
    if not ORACLE_ONLY:
        actual = cli(command, env, True)
        assert (actual.stdout, actual.stderr, actual.returncode) == (
            expected.stdout, expected.stderr, expected.returncode)
    assert snapshot(base) == before


@pytest.mark.parametrize('empty', [False, True])
def test_go_land_requires_isolated_worktree(tmp_path, empty):
    def prepare(env, d, repo, work, info):
        if empty:
            write_json(d / 'worktree.json', {})
    parity(tmp_path, prepare, [['land', 'fixture']], [2], worktree=False)


@pytest.mark.parametrize('notes', ['none', 'staged', 'unstaged', 'untracked', 'local-config'])
def test_go_land_fast_forward_preserves_unrelated_canonical_files(tmp_path, notes):
    def prepare(env, d, repo, work, info):
        commit(work, 'shared.txt', 'landed café\n')
        evidence(d, work)
        if notes in ('staged', 'unstaged'):
            (repo / 'notes.txt').write_bytes(b'local notes\n')
            if notes == 'staged':
                git(repo, 'add', 'notes.txt')
        elif notes == 'untracked':
            (repo / 'untracked note.txt').write_bytes(b'local untracked\n')
        elif notes == 'local-config':
            write_json(repo / '.nightshift' / 'local-only.json', {'local': True})
    def inspect(env, d, repo, work, info, actual):
        assert json.loads(actual[0][0])['status'] == 'landed'
        assert git(repo, 'rev-parse', 'main') == git(work, 'rev-parse', 'HEAD')
        assert (repo / 'shared.txt').read_bytes() == 'landed café\n'.encode()
        assert json.loads((d / 'landed.json').read_bytes())['target_branch'] == 'main'
        assert json.loads((d / 'bus.jsonl').read_bytes())['event'] == 'land'
        assert not repo_lock(env, repo).exists()
    parity(tmp_path, prepare, [['land', 'fixture']], [0], inspect)


@pytest.mark.parametrize('source', ['explicit', 'metadata', 'config', 'default'])
def test_go_land_target_branch_precedence(tmp_path, source):
    def prepare(env, d, repo, work, info):
        for branch in ('explicit', 'metadata', 'configured'):
            git(repo, 'branch', branch)
        chosen = {'explicit': 'explicit', 'metadata': 'metadata', 'config': 'configured', 'default': 'main'}[source]
        git(repo, 'checkout', '-q', chosen)
        if source != 'default':
            p = repo / '.nightshift' / 'config.yaml'
            p.parent.mkdir()
            p.write_text('target_branch: configured\n')
        if source in ('explicit', 'metadata'):
            info['target_branch'] = 'metadata'
        else:
            info.pop('target_branch')
        write_json(d / 'worktree.json', info)
        commit(work, 'shared.txt', 'target chosen\n')
        evidence(d, work)
    def inspect(env, d, repo, work, info, actual):
        landed = json.loads((d / 'landed.json').read_bytes())
        assert landed['target_branch'] == {'explicit': 'explicit', 'metadata': 'metadata',
               'config': 'configured', 'default': 'main'}[source]
    parity(tmp_path, prepare, [['land', 'fixture', *(['--target-branch', 'explicit'] if source == 'explicit' else [])]], [0], inspect)


@pytest.mark.parametrize('problem', [
    'dirty', 'untracked', 'no-build', 'old-head', 'wrong-cwd', 'failed', 'exit-seven',
    'dirty-build', 'unknown-dirty', 'latest-failed', 'recorded-branch', 'detached',
    'canonical-branch', 'overlap-unstaged', 'overlap-staged', 'overlap-untracked',
    'overlap-parent', 'overlap-child',
])
def test_go_land_rejects_unsafe_or_untested_changes(tmp_path, problem):
    def prepare(env, d, repo, work, info):
        name = 'new.txt' if problem == 'overlap-untracked' else 'shared.txt'
        if problem == 'overlap-parent':
            name = 'folder/new.txt'
        if problem == 'overlap-child':
            name = 'folder'
        commit(work, name, 'crew change\n')
        evidence(d, work)
        path = d / 'builds' / '1.json'
        changes = {'old-head': {'head': info['base_sha']}, 'wrong-cwd': {'cwd': str(repo)},
                   'failed': {'status': 'failed'}, 'exit-seven': {'exit': 7},
                   'dirty-build': {'dirty': True}, 'unknown-dirty': {'dirty': None}}
        if problem in changes:
            evidence(d, work, **changes[problem])
        elif problem == 'no-build':
            path.unlink()
        elif problem == 'latest-failed':
            evidence(d, work, n=2, status='failed', exit=1)
        elif problem == 'dirty':
            (work / 'shared.txt').write_bytes(b'not committed\n')
        elif problem == 'untracked':
            (work / 'not committed.txt').write_bytes(b'new\n')
        elif problem == 'recorded-branch':
            info['branch'] = 'shift/other'
            write_json(d / 'worktree.json', info)
        elif problem == 'detached':
            git(work, 'checkout', '-q', '--detach')
        elif problem == 'canonical-branch':
            git(repo, 'checkout', '-qb', 'other')
        elif problem.startswith('overlap'):
            target = repo / ('folder/child.txt' if problem == 'overlap-child' else
                             'folder' if problem == 'overlap-parent' else name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'local collision\n')
            if problem == 'overlap-staged':
                git(repo, 'add', name)
    def inspect(env, d, repo, work, info, actual):
        assert not (d / 'landed.json').exists()
        assert not repo_lock(env, repo).exists()
        assert git(repo, 'rev-parse', 'main') == info['base_sha']
    parity(tmp_path, prepare, [['land', 'fixture']], [1 if problem == 'detached' else 2], inspect)


@pytest.mark.parametrize('keep', [False, True])
def test_go_land_conflict_abort_or_keep(tmp_path, keep):
    def prepare(env, d, repo, work, info):
        commit(work, 'shared.txt', 'crew conflict\n')
        commit(repo, 'shared.txt', 'canonical conflict\n')
        evidence(d, work)
    def inspect(env, d, repo, work, info, actual):
        result = json.loads(actual[0][0])
        assert result['status'] == 'conflict' and result['files'] == ['shared.txt']
        assert bool(git(work, 'diff', '--name-only', '--diff-filter=U')) is keep
        assert not repo_lock(env, repo).exists() and not (d / 'landed.json').exists()
    parity(tmp_path, prepare, [['land', 'fixture', *(['--keep-conflicts'] if keep else [])]], [4], inspect)


def test_go_land_rebase_requires_new_build_then_lands(tmp_path):
    def prepare(env, d, repo, work, info):
        commit(work, 'shared.txt', 'crew rebase\n')
        commit(repo, 'canonical.txt', 'advanced target\n')
        evidence(d, work)
    def commands(env, d, repo, work, info):
        return [['land', 'fixture'], ['land', 'fixture'],
                ['build', 'fixture', '--cmd', 'echo retested'], ['land', 'fixture']]
    def inspect(env, d, repo, work, info, actual):
        assert json.loads(actual[0][0])['status'] == 'rebased_needs_test'
        assert b'passed nightshift build' in actual[1][1]
        assert json.loads(actual[3][0])['status'] == 'landed'
    parity(tmp_path, prepare, commands, [2, 2, 0, 0], inspect)


@pytest.mark.parametrize('holder', ['dead-landing', 'reused-pid', 'dead-shift', 'live-landing', 'live-old', 'empty'])
def test_go_land_stale_lock_and_contention(tmp_path, holder):
    def prepare(env, d, repo, work, info):
        commit(work, 'shared.txt', 'lock check\n')
        evidence(d, work)
        path = repo_lock(env, repo)
        path.parent.mkdir(parents=True)
        if holder == 'empty':
            path.touch()
        else:
            record = {'shift': 'other', 'kind': 'landing',
                      'pid': os.getpid() if holder in ('live-landing', 'live-old', 'reused-pid') else DEAD_PID}
            if holder == 'reused-pid':
                record['started_epoch'] = 1.0
            if holder == 'dead-shift':
                record = {'shift': 'missing'}
            write_json(path, record)
        if holder == 'live-old':
            os.utime(path, (time.time() - 60,) * 2)
    code = 3 if holder in ('live-landing', 'live-old', 'empty') else 0
    def inspect(env, d, repo, work, info, actual):
        assert repo_lock(env, repo).exists() is (code == 3)
        if code == 3:
            # FolderLocked carries .message but the oracle CLI never prints it.
            assert actual[0][0] == actual[0][1] == b''
    parity(tmp_path, prepare, [['land', 'fixture']], [code], inspect)


def test_go_land_waits_for_shared_python_named_mutex(tmp_path):
    env, d, repo, work, info = world(tmp_path)
    commit(work, 'shared.txt', 'mutex check\n')
    evidence(d, work)
    if ORACLE_ONLY:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with oracle.mailbox_guard(repo_lock(env, repo), timeout_ms=5000):
            pending = pool.submit(cli, ['land', 'fixture'], env, True)
            time.sleep(0.7)
            assert not pending.done(), 'native landing bypassed Python named mutex'
        proc = pending.result(timeout=20)
    assert proc.returncode == 0 and not proc.stderr, (proc.stdout, proc.stderr)
    assert (d / 'landed.json').exists()


@pytest.mark.parametrize('native', [False, True])
def test_go_land_shared_named_mutex_timeout_has_exit_three_and_empty_stderr(tmp_path, native):
    if native and ORACLE_ONLY:
        pytest.skip('native binary pending review')
    env, d, repo, work, info = world(tmp_path)
    evidence(d, work)
    before = observed_state(tmp_path, env, repo, work)
    with oracle.mailbox_guard(repo_lock(env, repo), timeout_ms=5000):
        started = time.monotonic()
        proc = cli(['land', 'fixture'], env, native, timeout=15)
        assert time.monotonic() - started >= 4.5
    assert proc.returncode == 3 and proc.stdout == proc.stderr == b''
    assert observed_state(tmp_path, env, repo, work) == before


@pytest.mark.parametrize('scenario', ['bare', 'result', 'invalid-result', 'claims', 'mailbox',
                                      'builds', 'denials', 'launch', 'no-launch', 'cr-log',
                                      'prior-handover', 'repeat'])
def test_go_down_handover_receipt_and_bus(tmp_path, scenario):
    def prepare(env, d, repo, work, info):
        if scenario == 'bare':
            (d / 'result.json').unlink()
            (d / 'Foreman.json').unlink()
        if scenario == 'invalid-result':
            (d / 'result.json').write_bytes(b'{broken')
        if scenario == 'claims':
            write_json(d / 'claims.json', {'C:\\space café\\file.py': {'role': 'Tester', 'time': 'first'}})
        if scenario == 'mailbox':
            with oracle.mailbox(d):
                pass
            with contextlib.closing(sqlite3.connect(d / 'mailbox.sqlite3')) as conn:
                conn.execute("INSERT INTO messages(kind,sender,recipient,asg,body,created,state) VALUES(?,?,?,?,?,?,?)",
                             ('note', 'Foreman', 'Foreman', None, 'pending', 'fixed', 'stored'))
                conn.commit()
        if scenario == 'builds':
            write_json(d / 'builds' / '1.json', {'n': 1, 'status': 'passed', 'exit': 0,
                       'head': '1234567890', 'dirty': False, 'cmd': 'echo café'})
            write_json(d / 'builds' / '2.json', {'n': 2, 'status': 'failed', 'exit': 7,
                       'head': None, 'dirty': True, 'cmd': 'exit /b 7'})
        if scenario == 'denials':
            write_json(d / 'denials.json', [{'time': 'first', 'role': 'Coder', 'line': 'Write DENIED'}])
        if scenario in ('launch', 'cr-log'):
            if scenario == 'cr-log':
                commit(repo, 'cr.txt', 'log entry\n', 'first\rsecond')
            os.utime(d / 'launch.json', (1.0, 1.0))
        if scenario == 'no-launch':
            (d / 'launch.json').unlink()
        if scenario == 'prior-handover':
            (d / 'handover.md').write_bytes(b'previous live pane evidence\n')
        write_json(repo_lock(env, repo), {'shift': d.name, 'cwd': str(repo)})
        write_json(Path(env['NIGHTSHIFT_STATE_ROOT']) / 'locks' / 'unrelated.json',
                   {'shift': 'other', 'cwd': 'unrelated'})
    def inspect(env, d, repo, work, info, actual):
        receipt = json.loads(actual[0][0])
        assert receipt['window_closed'] is False and receipt['stopped'] == []
        assert not repo_lock(env, repo).exists()
        assert (Path(env['NIGHTSHIFT_STATE_ROOT']) / 'locks' / 'unrelated.json').exists()
        text = (d / 'handover.md').read_text(encoding='utf-8')
        if scenario == 'prior-handover':
            assert text == 'previous live pane evidence\n'
        else:
            sections = ['## Result', '## Auto-mode denials', '## Outstanding claims',
                        '## Unacknowledged messages', '## Builds', '## Commits since shift start', '## Test evidence']
            assert [text.index(s) for s in sections] == sorted(text.index(s) for s in sections)
            assert 'teardown does not run tests' in text
        events = [json.loads(line) for line in (d / 'bus.jsonl').read_bytes().splitlines()]
        assert events[-1]['event'] == 'down'
        assert events[0]['event'] == ('down' if scenario == 'bare' else 'result')
        if scenario == 'claims':
            assert json.loads((d / 'claims.json').read_bytes()), 'down must retain claims'
    commands = [['down', 'fixture', '--hold']] + ([['down', 'fixture', '--close']] if scenario == 'repeat' else [])
    parity(tmp_path, prepare, commands, [0] * len(commands), inspect, worktree=False)


@pytest.mark.parametrize('disposition', ['landed', 'preserved', 'held', 'dirty-after-land',
                                       'config-only', 'tracked-config', 'wrong-branch', 'detached'])
def test_go_down_worktree_recovery_dispositions(tmp_path, disposition):
    def prepare(env, d, repo, work, info):
        if disposition in ('landed', 'dirty-after-land'):
            head = commit(work, 'shared.txt', 'landed commit\n')
            git(repo, 'merge', '--ff-only', head)
            write_json(d / 'landed.json', {'landed_sha': head})
        if disposition in ('preserved', 'held', 'dirty-after-land', 'tracked-config'):
            (work / 'shared.txt').write_bytes(b'dirty recovery\n')
            (work / 'new recovery.txt').write_bytes(b'untracked recovery\n')
        if disposition in ('config-only', 'tracked-config', 'preserved'):
            p = work / '.nightshift' / 'config.yaml'
            p.parent.mkdir(exist_ok=True)
            p.write_text('target_branch: main\n')
            if disposition == 'tracked-config':
                git(work, 'add', '.nightshift/config.yaml')
                git(work, 'commit', '-qm', 'track config')
                p.write_text('target_branch: changed\n')
        if disposition == 'wrong-branch':
            git(work, 'checkout', '-qb', 'shift/unexpected')
        if disposition == 'detached':
            git(work, 'checkout', '-q', '--detach')
    def inspect(env, d, repo, work, info, actual):
        recovery = json.loads((d / 'recovery.json').read_bytes())
        floor = json.loads((Path(env['NIGHTSHIFT_STATE_ROOT']) / 'floor.json').read_bytes())
        assert floor['worktrees'][d.name] == recovery
        assert '## Worktree recovery' in (d / 'handover.md').read_text(encoding='utf-8')
        if disposition in ('wrong-branch', 'detached'):
            assert work.exists() and recovery['cleanup_pending'] and recovery['error']
        elif disposition == 'held':
            assert work.exists() and recovery['disposition'] == 'held'
            assert not recovery['cleanup_pending']
            assert (work / 'shared.txt').read_bytes() == b'dirty recovery\n'
        else:
            assert not work.exists() and not recovery['cleanup_pending'] and recovery['error'] is None
            exists = git(repo, 'show-ref', '--verify', '--quiet', 'refs/heads/' + info['branch'], check=False).returncode == 0
            assert exists is (disposition != 'landed')
            if disposition in ('preserved', 'dirty-after-land', 'tracked-config'):
                assert git(repo, 'show', info['branch'] + ':shared.txt') == 'dirty recovery'
            if disposition in ('preserved', 'config-only'):
                assert git(repo, 'cat-file', '-e', info['branch'] + ':.nightshift/config.yaml', check=False).returncode != 0
    parity(tmp_path, prepare, [['down', 'fixture', '--hold' if disposition == 'held' else '--close']], [0], inspect)


@pytest.mark.parametrize('removal_started,remaining', [(False, False), (True, False), (True, True)])
def test_go_down_partial_removal_never_deletes_unexpected_files(tmp_path, removal_started, remaining):
    def prepare(env, d, repo, work, info):
        git(repo, 'worktree', 'remove', '--force', str(work))
        work.mkdir()
        if remaining:
            (work / 'precious.txt').write_bytes(b'must survive\n')
        write_json(d / 'recovery.json', {'removal_started': removal_started, 'preserved_branch': info['branch']})
    def inspect(env, d, repo, work, info, actual):
        recovery = json.loads((d / 'recovery.json').read_bytes())
        assert recovery['cleanup_pending'] is (not removal_started or remaining)
        assert work.exists() is (not removal_started or remaining)
        if remaining:
            assert (work / 'precious.txt').read_bytes() == b'must survive\n'
    parity(tmp_path, prepare, [['down', 'fixture']], [0], inspect)


@pytest.mark.parametrize('dry', [False, True])
@pytest.mark.parametrize('floor_lock', ['none', 'dead', 'old-empty', 'live', 'fresh-empty'])
def test_go_cleaner_floor_locks_and_dry_run(tmp_path, dry, floor_lock):
    def prepare(env, d, repo, work, info):
        p = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'floor.lock'
        if floor_lock in ('dead', 'live'):
            write_json(p, {'pid': os.getpid() if floor_lock == 'live' else DEAD_PID})
        if floor_lock in ('old-empty', 'fresh-empty'):
            p.touch()
            if floor_lock == 'old-empty':
                os.utime(p, (time.time() - 60,) * 2)
    def inspect(env, d, repo, work, info, actual):
        root = Path(env['NIGHTSHIFT_STATE_ROOT'])
        if dry:
            assert not (root / 'floor.json').exists() and not (d / 'handover.md').exists()
        elif floor_lock not in ('live', 'fresh-empty'):
            assert (root / 'floor.json').exists() and not (root / 'floor.lock').exists()
    parity(tmp_path, prepare, [['cleaner', *(['--dry-run'] if dry else [])]],
           [1 if not dry and floor_lock in ('live', 'fresh-empty') else 0], inspect, worktree=False)


@pytest.mark.parametrize('dry', [False, True])
def test_go_cleaner_reaps_locks_claims_legacy_and_repeat_is_noop(tmp_path, dry):
    def prepare(env, d, repo, work, info):
        write_json(d / 'claims.json', {'fixture.py': {'role': 'Tester', 'time': 'held'}})
        for name in ('claims.lock', 'bus.lock'):
            (d / name).touch()
            os.utime(d / name, (time.time() - 60,) * 2)
        write_json(repo_lock(env, repo), {'shift': d.name, 'cwd': str(repo)})
        legacy = Path(env['NIGHTSHIFT_LEGACY_ROOT']) / 'shifts' / 'Nightshift-shift-legacy-fixture'
        legacy.mkdir(parents=True)
        write_json(legacy / 'crew.json', {'members': []})
        write_json(legacy / 'launch.json', {'hwnd': INVALID_HWND})
        write_json(Path(env['NIGHTSHIFT_LEGACY_ROOT']) / 'locks' / 'legacy.json',
                   {'shift': legacy.name, 'cwd': 'legacy'})
        os.utime(d, (1700000000, 1700000000))
        os.utime(legacy, (1700000001, 1700000001))
    def inspect(env, d, repo, work, info, actual):
        first, second = (json.loads(p[0]) for p in actual)
        assert len(first['reaped']) == 2
        assert len(first['locks_released']) == 5
        if dry:
            assert first == second
            assert not (d / 'handover.md').exists()
            assert (d / 'claims.lock').exists() and repo_lock(env, repo).exists()
        else:
            assert second['reaped'] == []
            assert json.loads((d / 'claims.json').read_bytes()) == {}
            assert not (d / 'claims.lock').exists() and not repo_lock(env, repo).exists()
    command = ['cleaner', *(['--dry-run'] if dry else [])]
    parity(tmp_path, prepare, [command, command], [0, 0], inspect, worktree=False)


def test_go_cleaner_preserves_dirty_worktree_and_recovery_floor(tmp_path):
    def prepare(env, d, repo, work, info):
        (work / 'shared.txt').write_bytes(b'cleaner preserved\n')
    def inspect(env, d, repo, work, info, actual):
        assert not work.exists()
        assert git(repo, 'show', info['branch'] + ':shared.txt') == 'cleaner preserved'
        report = json.loads(actual[0][0])
        assert report['reaped'][0]['disposition'] == 'preserved'
        assert report['reaped'][0]['cleanup_pending'] is False
        floor = json.loads((Path(env['NIGHTSHIFT_STATE_ROOT']) / 'floor.json').read_bytes())
        assert floor['worktrees'][d.name]['recovery_sha'] == report['reaped'][0]['recovery_sha']
        assert json.loads(actual[1][0])['reaped'] == []
    parity(tmp_path, prepare, [['cleaner'], ['cleaner']], [0, 0], inspect)


@contextlib.contextmanager
def owned_fixture_process(base):
    """Only our copied CLI, hidden, on its own root; no agent/console is started."""
    if ORACLE_ONLY:
        pytest.skip('requires compiled native CLI as the harmless process fixture')
    base.mkdir()
    copied = base / 'codex.exe'
    shutil.copyfile(exe_path(), copied)
    root = base / 'private process state'
    d = root / 'shifts' / 'Nightshift-shift-process-only'
    d.mkdir(parents=True)
    env = {**os.environ, 'NIGHTSHIFT_STATE_ROOT': str(root),
           'NIGHTSHIFT_LEGACY_ROOT': str(base / 'absent legacy'),
           'NIGHTSHIFT_HOME': str(base / 'home')}
    sleeper = subprocess.list2cmdline([sys.executable, '-c', "__import__('time').sleep(120)"])
    with (base / 'stdout').open('wb') as out, (base / 'stderr').open('wb') as err:
        proc = subprocess.Popen([str(copied), 'build', 'process-only', '--cwd', str(base), '--cmd', sleeper],
                                env=env, stdout=out, stderr=err,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and proc.poll() is None:
                p = d / 'builds' / '1.json'
                if p.exists() and json.loads(p.read_bytes()).get('status') == 'running':
                    break
                time.sleep(0.05)
            else:
                pytest.fail('owned fixture process never reached running state')
            assert oracle.pid_alive(proc.pid), 'our renamed binary must be recognized as a live role'
            yield proc
        finally:
            if proc.poll() is None:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                               capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            proc.wait(timeout=15)


@pytest.mark.parametrize('result,stage,reap', [
    ('working', 'implemented', False), ('done', 'implemented', False),
    ('done', 'tested', True), ('halted', 'implemented', True),
])
@pytest.mark.parametrize('dry', [False, True])
def test_go_cleaner_protects_live_active_and_short_shifts(tmp_path, result, stage, reap, dry):
    observed = []
    for native in ([False] if ORACLE_ONLY else [False, True]):
        base = tmp_path / str(native)
        env, d, repo, work, info = world(base, False)
        with owned_fixture_process(base / 'owned process') as holder:
            write_json(d / 'Foreman.json', {'pid': holder.pid, 'name': 'fixture', 'tool': 'codex'})
            write_json(d / 'result.json', {'status': result, 'stage': stage})
            before = snapshot(Path(env['NIGHTSHIFT_STATE_ROOT']))
            proc = cli(['cleaner', *(['--dry-run'] if dry else [])], env, native)
            assert proc.returncode == 0 and not proc.stderr, (proc.stdout, proc.stderr)
            report = json.loads(proc.stdout)
            assert bool(report['reaped']) is reap
            if dry or not reap:
                assert holder.poll() is None
                assert snapshot(Path(env['NIGHTSHIFT_STATE_ROOT'])) == before
            else:
                holder.wait(timeout=15)
                assert (d / 'handover.md').exists()
            # Paths differ between the two independent live-process fixtures;
            # compare the receipt fields after asserting actual state above.
            observed.append(([x['result'] for x in report['reaped']], report['dry_run']))
    assert observed[0] == observed[-1]


@pytest.mark.parametrize('native', [False, True])
def test_go_down_own_ancestor_receipt_bus_recovery_and_release_precede_kill(tmp_path, native):
    if ORACLE_ONLY:
        pytest.skip('requires compiled native CLI as the harmless ancestor fixture')
    env, d, repo, work, info = world(tmp_path)
    (work / 'shared.txt').write_bytes(b'ancestry recovery survives\n')
    lock = repo_lock(env, repo)
    write_json(lock, {'shift': d.name, 'cwd': str(repo)})
    harness = tmp_path / 'codex.exe'
    shutil.copyfile(exe_path(), harness)
    gate, receipt = tmp_path / 'go gate.json', tmp_path / 'receipt.json'
    child_stderr = tmp_path / 'child.stderr'
    role_file = d / 'Foreman.json'
    down_args = ([str(exe_path()), 'down', 'fixture', '--close'] if native else
                 [sys.executable, '-c', BOOT, 'down', 'fixture', '--close'])
    # Driver is a child of the owned build process. A premature taskkill of its
    # ancestor prevents durable recovery/bus/receipt; all three are asserted.
    driver = '\n'.join([
        'import json, os, subprocess, time', 'from pathlib import Path',
        f'gate = Path({str(gate)!r})',
        'while not gate.exists(): time.sleep(0.02)',
        'holder = json.loads(gate.read_text())',
        "holder.update(wrapper_pid=os.getpid(), name='fixture ancestor', tool='codex')",
        f'Path({str(role_file)!r}).write_text(json.dumps(holder))',
        f'with open({str(receipt)!r}, "wb") as out, open({str(child_stderr)!r}, "wb") as err:',
        f'    subprocess.run({down_args!r}, cwd={str(REPO)!r}, stdout=out, stderr=err)',
    ])
    # Hex keeps the nested oracle bootstrap (quotes/newlines/metacharacters) out
    # of cmd.exe parsing entirely; the child reconstructs it in memory.
    command = subprocess.list2cmdline([sys.executable, '-c',
                                     f"exec(bytes.fromhex('{driver.encode().hex()}').decode())"])
    assert len(command) < 8191, 'fixture command exceeds CMD limit'
    process_dir = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-ancestor-driver'
    process_dir.mkdir()
    child_env = {**os.environ, **env}
    with (tmp_path / 'driver.stdout').open('wb') as out, (tmp_path / 'driver.stderr').open('wb') as err:
        parent = subprocess.Popen([str(harness), 'build', 'ancestor-driver', '--cwd', str(tmp_path), '--cmd', command],
                                  env=child_env, stdout=out, stderr=err,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            assert parent.poll() is None
            write_json(gate, {'pid': parent.pid})
            parent.wait(timeout=45)
            assert receipt.exists(), (parent.returncode, (tmp_path / 'driver.stdout').read_bytes(),
                                      (tmp_path / 'driver.stderr').read_bytes())
            assert receipt.read_bytes(), child_stderr.read_bytes()
            data = json.loads(receipt.read_bytes())
            assert parent.returncode != 0, 'owned ancestor completed naturally instead of being stopped'
            assert data['stopped'] == ['Foreman'] and data['lock_released'] == [str(repo)]
            assert not lock.exists() and not work.exists()
            recovery = json.loads((d / 'recovery.json').read_bytes())
            assert not recovery['cleanup_pending'] and recovery['error'] is None
            assert git(repo, 'show', info['branch'] + ':shared.txt') == 'ancestry recovery survives'
            events = [json.loads(line) for line in (d / 'bus.jsonl').read_bytes().splitlines()]
            assert [e['event'] for e in events] == ['result', 'down']
            assert events[-1]['stopped'] == ['Foreman']
            assert json.loads((Path(env['NIGHTSHIFT_STATE_ROOT']) / 'floor.json').read_bytes())['worktrees'][d.name] == recovery
        finally:
            if parent.poll() is None:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(parent.pid)], capture_output=True,
                               timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                parent.wait(timeout=15)


# This entry point is compiled only into a disposable source copy. It exposes
# private helpers without adding test switches or dependency hooks to product code.
HELPER_MAIN = r'''
package main
import ("fmt"; "os"; "path/filepath"; "strconv"; "strings")
func main() {
    if strings.EqualFold(strings.TrimSuffix(filepath.Base(os.Args[0]), ".exe"), "agents-cli") {
        fmt.Print(os.Getenv("NIGHTSHIFT_FIXTURE_PEEK")); return
    }
    if len(os.Args)>1 && os.Args[1]=="fixture-denials" {
        v, _ := readJSONFile(os.Args[3]); tails, _ := v.(object)
        rows, err := recordDenials(os.Args[2], tails)
        if err != nil { fmt.Fprintln(os.Stderr,err); os.Exit(1) }
        fmt.Println(renderJSON(rows,false,true)); return
    }
    if len(os.Args)>1 && os.Args[1]=="fixture-pane" {
        pid, _ := strconv.ParseUint(os.Args[2],10,32); lines, _ := strconv.Atoi(os.Args[3])
        rows := []any{}; for _, line := range paneTail(uint32(pid),lines) { rows=append(rows,line) }
        fmt.Println(renderJSON(rows,false,true)); return
    }
    originalMain()
}
'''


@pytest.fixture(scope='module')
def lifecycle_helper(tmp_path_factory):
    if ORACLE_ONLY:
        pytest.skip('helper parity requires native compilation after Foreman review')
    root = tmp_path_factory.mktemp('lifecycle-helper-source')
    source = root / 'cmd' / 'nightshift'
    source.mkdir(parents=True)
    for path in (REPO / 'cmd' / 'nightshift').glob('*.go'):
        if not path.name.endswith('_test.go'):
            shutil.copyfile(path, source / path.name)
    for name in ('go.mod', 'go.sum'):
        shutil.copyfile(REPO / name, root / name)
    main = source / 'main.go'
    original = main.read_text(encoding='utf-8')
    assert original.count('func main() {') == 1
    main.write_text(original.replace('func main() {', 'func originalMain() {', 1), encoding='utf-8')
    (source / 'fixture_entry.go').write_text(HELPER_MAIN, encoding='utf-8')
    try:
        built = subprocess.run(['cmd.exe', '/d', '/c', 'C:/scripts/build-nightshift-go.cmd', str(root)],
                               capture_output=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        assert built.returncode == 0, (built.stdout, built.stderr)
        yield root / 'build' / 'nightshift.exe'
    finally:
        # Temporary source helpers cannot become part of the deliverable.
        shutil.rmtree(source)


@pytest.mark.parametrize('existing', ['missing', 'invalid', 'wrong-type', 'existing'])
def test_go_record_denials_deduplicates_trimmed_role_line_and_preserves_first_time(tmp_path, lifecycle_helper, existing):
    base = tmp_path / 'denials'
    tails_file = tmp_path / 'tails.json'
    calls = [
        {'Coder': ['  Bash denied by auto mode classifier  ', 'ok', 'Permission DENIED: café',
                   'Bash denied by auto mode classifier'], 'Tester': ['Bash denied by auto mode classifier']},
        {'Coder': ['Bash denied by auto mode classifier', '\tPermission DENIED: café\t', 'new denied']},
    ]
    observed = []
    for native in (False, True):
        if base.exists():
            remove_fixture(base, tmp_path)
        base.mkdir()
        p = base / 'denials.json'
        if existing == 'invalid':
            p.write_bytes(b'{broken')
        if existing == 'wrong-type':
            write_json(p, {})
        if existing == 'existing':
            write_json(p, [{'time': 'first-time', 'role': 'Coder', 'line': 'Bash denied by auto mode classifier'}])
        outputs = []
        for tails in calls:
            if native:
                write_json(tails_file, tails)
                proc = subprocess.run([str(lifecycle_helper), 'fixture-denials', str(base), str(tails_file)],
                                      capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
                assert proc.returncode == 0 and not proc.stderr
                rows = json.loads(proc.stdout)
            else:
                rows = oracle.record_denials(base, tails)
            outputs.append(stable(json.dumps(rows, ensure_ascii=True).encode()))
        rows = json.loads(p.read_bytes())
        assert len(rows) == 4 and len({(r['role'], r['line']) for r in rows}) == 4
        if existing == 'existing':
            assert rows[0]['time'] == 'first-time'
        observed.append((outputs, stable(p.read_bytes())))
    assert observed[1] == observed[0]


@pytest.mark.parametrize('lines', [0, 1, 2, 40])
@pytest.mark.parametrize('payload', ['valid', 'invalid'])
def test_go_pane_tail_filters_blank_lines_and_limits_visible_tail(tmp_path, lifecycle_helper, monkeypatch, lines, payload):
    fake = tmp_path / 'agents-cli.exe'
    shutil.copyfile(lifecycle_helper, fake)
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('NIGHTSHIFT_FIXTURE_PEEK', json.dumps({'lines': ['first', ' ', '', ' café ', 'last']})
                       if payload == 'valid' else 'not-json')
    expected = oracle.pane_tail(DEAD_PID, lines)
    visible = ['first', ' café ', 'last']
    assert expected == (visible[-lines:] if payload == 'valid' else [])
    proc = subprocess.run([str(lifecycle_helper), 'fixture-pane', str(DEAD_PID), str(lines)],
                          capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    assert proc.returncode == 0 and not proc.stderr
    assert json.loads(proc.stdout) == expected


@pytest.mark.parametrize('native', [False, True])
def test_go_down_captures_live_tail_denials_and_retains_handover_after_hold(tmp_path, lifecycle_helper, monkeypatch, native):
    env, d, repo, work, info = world(tmp_path, False)
    fakebin = tmp_path / 'fake tools'
    fakebin.mkdir()
    shutil.copyfile(lifecycle_helper, fakebin / 'agents-cli.exe')
    monkeypatch.setenv('PATH', str(fakebin) + os.pathsep + os.environ['PATH'])
    visible = [f'visible {i}' for i in range(45)] + ['  Bash denied  ', 'Permission DENIED: café',
                                                     ' ', '  Bash denied  ']
    monkeypatch.setenv('NIGHTSHIFT_FIXTURE_PEEK', json.dumps({'lines': visible}))
    write_json(d / 'denials.json', [{'time': 'first-time', 'role': 'Foreman', 'line': 'Bash denied'}])
    with owned_fixture_process(tmp_path / 'owned process') as holder:
        write_json(d / 'Foreman.json', {'pid': holder.pid, 'name': 'fixture live', 'tool': 'codex'})
        proc = cli(['down', 'fixture', '--hold'], env, native)
        assert proc.returncode == 0 and not proc.stderr, (proc.stdout, proc.stderr)
        holder.wait(timeout=15)
        assert json.loads(proc.stdout)['stopped'] == ['Foreman']
    rows = json.loads((d / 'denials.json').read_bytes())
    assert [(r['role'], r['line']) for r in rows] == [
        ('Foreman', 'Bash denied'), ('Foreman', 'Permission DENIED: café')]
    assert rows[0]['time'] == 'first-time'
    handover = (d / 'handover.md').read_bytes()
    text = handover.decode().replace('\r\n', '\n')
    assert 'visible 0\n' not in text and 'visible 44\n' in text
    assert '  Bash denied  ' in text and 'Permission DENIED: café' in text
    assert 'alive)' in text
    proc = cli(['down', 'fixture', '--close'], env, native)
    assert proc.returncode == 0 and not proc.stderr
    assert (d / 'handover.md').read_bytes() == handover


@pytest.mark.parametrize('phase', ['index-locked', 'commit-rejected'])
def test_go_down_failed_recovery_stays_pending_and_cleaner_retries(tmp_path, phase):
    def prepare(env, d, repo, work, info):
        (work / 'shared.txt').write_bytes(b'failed-save original survives\n')
        if phase == 'index-locked':
            Path(git(work, 'rev-parse', '--git-path', 'index.lock')).touch()
        else:
            git(repo, 'config', 'user.name', '')
            git(repo, 'config', 'user.email', '')
            # Child Git must see an explicit missing identity rather than global user config.
            env['GIT_AUTHOR_NAME'] = env['GIT_COMMITTER_NAME'] = ''
    def commands(env, d, repo, work, info):
        yield ['down', 'fixture']
        recovery = json.loads((d / 'recovery.json').read_bytes())
        assert recovery['cleanup_pending'] and recovery['error']
        assert work.exists() and (work / 'shared.txt').read_bytes() == b'failed-save original survives\n'
        assert json.loads((d / 'bus.jsonl').read_bytes().splitlines()[-1])['event'] == 'down'
        if phase == 'index-locked':
            Path(git(work, 'rev-parse', '--git-path', 'index.lock')).unlink()
        else:
            env.update(git_env())
        yield ['cleaner']
    def inspect(env, d, repo, work, info, actual):
        recovery = json.loads((d / 'recovery.json').read_bytes())
        assert not recovery['cleanup_pending'] and recovery['error'] is None and not work.exists()
        assert git(repo, 'show', info['branch'] + ':shared.txt') == 'failed-save original survives'
    parity(tmp_path, prepare, commands, [0, 0], inspect)


@pytest.mark.parametrize('result', [
    {}, {'status': False, 'summary': False, 'changed': [True, None, 3]},
    {'status': 'done', 'evidence': [1]},
    {'status': 'done', 'evidence': [1, {'stage': None, 'outcome': True}]},
])
def test_go_down_handover_python_scalar_formatting_and_filtered_evidence(tmp_path, result):
    def prepare(env, d, repo, work, info):
        write_json(d / 'result.json', result)
    parity(tmp_path, prepare, [['down', 'fixture', '--hold']], [0], worktree=False)


def test_go_lifecycle_runs_standalone_from_foreign_cwd_without_python(tmp_path):
    if ORACLE_ONLY:
        pytest.skip('native standalone verification pending review')
    env, d, repo, work, info = world(tmp_path)
    head = commit(work, 'shared.txt', 'standalone landed\n')
    evidence(d, work)
    foreign = tmp_path / 'foreign cwd'
    foreign.mkdir()
    copied = foreign / 'nightshift standalone.exe'
    shutil.copyfile(exe_path(), copied)
    git_bin = Path(shutil.which('git')).parent
    restricted = {**os.environ, **env, 'PATH': os.pathsep.join([
        str(Path(os.environ['SystemRoot']) / 'System32'), str(git_bin)]),
        'PYTHONPATH': '', 'PYTHONHOME': ''}
    assert shutil.which('python', path=restricted['PATH']) is None
    for command in [['land', 'fixture'], ['down', 'fixture'], ['cleaner']]:
        proc = subprocess.run([str(copied), *command], cwd=foreign, env=restricted,
                              capture_output=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW)
        assert proc.returncode == 0 and not proc.stderr, (command, proc.stdout, proc.stderr)
    assert git(repo, 'rev-parse', 'main') == head and not work.exists()
    assert json.loads((d / 'recovery.json').read_bytes())['disposition'] == 'landed'
    assert json.loads((Path(env['NIGHTSHIFT_STATE_ROOT']) / 'floor.json').read_bytes())['reaped'][0]['shift'] == d.name


if __name__ == '__main__':
    if sys.argv[1:] != ['--oracle-only']:
        raise SystemExit('usage: test_go_lifecycle.py --oracle-only (fixture preparation, not native verification)')
    os.environ['NIGHTSHIFT_LIFECYCLE_ORACLE_ONLY'] = '1'
    raise SystemExit(pytest.main([__file__, '-q', '--tb=short']))

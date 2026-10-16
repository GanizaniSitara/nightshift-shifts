"""Independent native-write parity against the frozen Python CLI oracle (recorded goldens, NGH-055;
re-record with testdata/record_goldens.py).

Both implementations operate on the SAME restored fixture path. Output and text
files are compared byte for byte after validating and replacing only wall-clock
timestamps, delivered epochs and child PIDs. SQLite is compared as ordered rows,
not as implementation-dependent database pages. No real crew state is touched.
"""
import concurrent.futures
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from test_go_readonly import (REPO, ORACLE_BOOT, existing_pid, frozen, make_world,
                              junction_alias, oracle, run_cli, write_json)


STAMP = re.compile(rb'202[0-9]-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}')
VOLATILE_NUMBER = re.compile(rb'("(?:delivered_epoch|pid)": )([0-9]+(?:\.[0-9]+)?)')


def stable_bytes(data):
    build_record = b'"cmd": ' in data and b'"status": ' in data
    def stamp(match):
        value = datetime.datetime.fromisoformat(match[0].decode())
        return b'<wall-clock>' if abs(time.time() - value.timestamp()) < 120 else match[0]

    def number(match):
        value = float(match[2])
        if b'delivered_epoch' in match[1]:
            # Fixture epochs are fixed; only newly delivered values are volatile.
            if value < 1_000_000_000:
                return match[0]
            assert abs(time.time() - value) < 120
            return match[1] + b'<delivered_epoch>'
        assert value > 0 and value.is_integer()
        # Fixture member/lock PIDs are fixed and must remain observable.
        return match[1] + b'<child_pid>' if build_record else match[0]

    return VOLATILE_NUMBER.sub(number, STAMP.sub(stamp, data))


def world_state(base):
    files, rows = {}, {}
    for p in sorted(base.rglob('*')):
        if not p.is_file():
            continue
        key = str(p.relative_to(base))
        if p.name.endswith(('-wal', '-shm')):
            pytest.fail(f'connection sidecar leaked after command: {p}')
        if p.suffix == '.sqlite3':
            with contextlib.closing(sqlite3.connect(p)) as conn:
                conn.row_factory = sqlite3.Row
                messages = [dict(r) for r in conn.execute('SELECT * FROM messages ORDER BY id')]
                schema = conn.execute('PRAGMA table_info(messages)').fetchall()
            rows[key] = (stable_bytes(json.dumps(messages, ensure_ascii=False).encode()),
                         [tuple(r) for r in schema])
        else:
            files[key] = stable_bytes(p.read_bytes())
    return files, rows


def shift(env, name='alpha'):
    return Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / ('Nightshift-shift-' + name)


def clean_world(base):
    env = make_world(base)
    d = shift(env)
    (d / 'claims.json').unlink()
    (d / 'bus.jsonl').unlink()
    roster = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    roster['members'].append({'label': 'Advisor', 'role': 'Advisor',
                              'identity': 'mesh-advisor', 'human_contact': False})
    write_json(d / 'crew.json', roster)
    write_json(d / 'Advisor.json', {'pid': 2147483000, 'started': 500.0})
    return env


def parity(tmp_path, prepare, commands, expected_codes=None, extra=None):
    """Restore only the verified private fixture, keeping absolute path bytes equal; the oracle side is frozen."""
    base = tmp_path / 'private write fixture'

    def observe(go):
        if base.exists():
            assert base.resolve().is_relative_to(tmp_path.resolve())
            shutil.rmtree(base)
        env = clean_world(base)
        prepare(env, base)
        args_list = commands(env, base) if callable(commands) else commands
        outputs = []
        for args in args_list:
            proc = run_cli(args, env, go=go, timeout=40)
            outputs.append((stable_bytes(proc.stdout), stable_bytes(proc.stderr), proc.returncode))
        if expected_codes is not None:
            assert [o[2] for o in outputs] == expected_codes, outputs
        return outputs, world_state(base)

    expected = frozen(lambda: observe(False), tmp_path, {'PYTEST_PID': os.getpid(), **(extra or {})})
    assert observe(True) == expected
    return expected


def noop(env, base):
    pass


@pytest.mark.parametrize('scenario', ['raw-role', 'deduplicate', 'conflict', 'release-order',
                                      'stale-lock', 'invalid-claims', 'torn-bus'])
def test_go_claim_release_persisted_byte_parity(tmp_path, scenario):
    def prepare(env, base):
        d = shift(env)
        if scenario == 'stale-lock':
            p = d / 'claims.lock'
            p.touch()
            os.utime(p, (time.time() - 60,) * 2)
        if scenario == 'invalid-claims':
            (d / 'claims.json').write_bytes(b'not json')
        if scenario == 'torn-bus':
            (d / 'bus.jsonl').write_bytes(b'{"torn":')

    def commands(env, base):
        a, b = str(base / 'Café space' / 'Ω.py'), str(base / 'second.py')
        same = a.replace('\\', '/').upper()
        if scenario == 'conflict':
            return [['claim', 'alpha', 'QA', a], ['claim', 'alpha', 'Senior', b, a],
                    ['release', 'alpha', 'Senior', a], ['claims', 'alpha']]
        if scenario == 'release-order':
            return [['claim', 'alpha', 'QA', b, a], ['claim', 'alpha', 'Senior', str(base / 'third')],
                    ['release', 'alpha', 'qa', a], ['release', 'alpha', 'QA']]
        return [['claim', 'alpha', 'mesh-junior', a, same, b],
                ['claim', 'alpha', 'MESH-JUNIOR', a],
                ['release', 'alpha', 'Junior'], ['release', 'alpha', 'mesh-junior']]

    parity(tmp_path, prepare, commands, [0, 4, 0, 0] if scenario == 'conflict' else [0] * 4)


@pytest.mark.parametrize('isolated', [False, True, 'empty-object'])
def test_go_claim_cross_shift_live_conflict_and_worktree_isolation(tmp_path, isolated):
    crew_pid = existing_pid('codex.exe')
    assert crew_pid, 'requires existing Codex; never launch a crew for a test'

    def prepare(env, base):
        other = shift(env, 'empty')
        write_json(other / 'Foreman.json', {'pid': crew_pid})
        write_json(other / 'claims.json', {oracle.normalise_path(base / 'shared.py'):
                                          {'role': 'other-role', 'time': 'held'}})
        if isolated:
            write_json(shift(env) / 'worktree.json', {} if isolated == 'empty-object' else
                       {'repo_root': str(base), 'worktree_dir': str(base)})

    parity(tmp_path, prepare,
           lambda env, base: [['claim', 'alpha', 'QA', str(base / 'fresh'), str(base / 'shared.py')]],
           [0 if isolated is True else 4], {'CODEX_PID': crew_pid})


def test_go_claim_raw_roles_use_python_unicode_lower_semantics(tmp_path):
    parity(tmp_path, noop, lambda env, base: [
        ['claim', 'alpha', 'İ', str(base / 'role.py')],
        ['claim', 'alpha', 'i', str(base / 'role.py')],
        ['release', 'alpha', 'i\u0307'],
    ], [0, 4, 0])


@pytest.mark.parametrize('value,role', [({}, 'QA'), ({'role': None}, 'None'), ({'role': 42}, '42')])
def test_go_claim_existing_missing_and_nonstring_roles_match_oracle(tmp_path, value, role):
    def prepare(env, base):
        write_json(shift(env) / 'claims.json', {oracle.normalise_path(base / 'typed.py'): value})
    parity(tmp_path, prepare, lambda env, base: [
        ['release', 'alpha', role], ['claim', 'alpha', role, str(base / 'typed.py')],
    ])


def send(sender='Boss', recipient='QA', kind='note', body='Café 🚀 "quoted" \\path\nline\ttwo', *opts):
    return ['msg', 'send', 'alpha', sender, recipient, '--kind', kind, '--body', body, *opts]


@pytest.mark.parametrize('commands,codes', [
    ([send('session-boss', 'mesh-qa'), ['msg', 'sent', 'alpha', '15'],
      ['msg', 'sent', 'alpha', '15'], ['ack', 'alpha', 'session-qa', '15'],
      ['msg', 'sent', 'alpha', '15'], ['ack', 'alpha', 'QA', '15']], [0] * 6),
    ([send('Boss', 'Advisor'), send('Advisor', 'Boss')], [0, 0]),
    ([send('Junior', 'Advisor')], [1]),
    ([send('Advisor', 'QA')], [1]),
    ([send('outsider', 'QA')], [1]),
    ([['msg', 'sent', 'alpha', '999']], [1]),
    ([['msg', 'sent', 'alpha', '8']], [0]),
    ([['ack', 'alpha', 'QA', '1']], [5]),
    ([['ack', 'alpha', 'Junior', '7']], [5]),
    ([['ack', 'alpha', 'Junior', '999']], [5]),
    ([['ack', 'alpha', 'Junior', '14'], ['ack', 'alpha', 'Junior', '14']], [0, 0]),
    ([send('Boss', 'QA', 'assignment', 'verify', '--asg', 'verify-c', '--coding-asg', 'code-a')], [0]),
    ([send('Boss', 'QA', 'assignment', 'verify', '--coding-asg', 'code-a')], [1]),
    ([send('Boss', 'QA', 'assignment', 'verify', '--asg', 'v', '--coding-asg', 'absent')], [1]),
    ([send('Junior', 'QA', 'assignment', 'verify', '--asg', 'v', '--coding-asg', 'code-a')], [1]),
    ([send('Boss', 'Senior', 'assignment', 'transfer', '--asg', 'code-a')], [1]),
    ([send('Boss', 'Senior', 'note', 'bad reason', '--reason', 'timeout')], [1]),
    ([send('Boss', 'Senior', 'uplift', 'transfer', '--asg', 'code-a', '--reason', 'needs-senior'),
      send('Boss', 'Senior', 'uplift', 'different body ignored', '--asg', 'code-a', '--reason', 'needs-senior')], [0, 0]),
    ([send('Boss', 'Senior', 'uplift', 'transfer', '--asg', 'code-a', '--reason', 'tester-failed-twice')], [0]),
    ([send('Boss', 'Senior', 'uplift', 'transfer', '--asg', 'code-a', '--reason', 'timeout')], [0]),
    ([send('Boss', 'Senior', 'uplift', 'transfer', '--asg', 'absent', '--reason', 'timeout')], [1]),
    ([send('Boss', 'Junior', 'uplift', 'transfer', '--asg', 'code-a', '--reason', 'timeout')], [1]),
])
def test_go_mailbox_guards_and_transitions(tmp_path, commands, codes):
    parity(tmp_path, noop, commands, codes)


@pytest.mark.parametrize('alias', ['Junior', 'mesh-junior', 'session-junior'])
def test_go_uplift_rejects_claims_for_every_owner_alias(tmp_path, alias):
    def prepare(env, base):
        write_json(shift(env) / 'claims.json', {'held.py': {'role': alias, 'time': 'held'}})

    parity(tmp_path, prepare, [send('Boss', 'Senior', 'uplift', 'transfer', '--asg', 'code-a',
                                  '--reason', 'needs-senior')], [4])


@pytest.mark.parametrize('scenario', ['missing', 'old-schema', 'missing-incarnation'])
def test_go_mailbox_bootstrap_and_schema_migration(tmp_path, scenario):
    def prepare(env, base):
        d = shift(env)
        (d / 'mailbox.sqlite3').unlink()
        if scenario == 'old-schema':
            with contextlib.closing(sqlite3.connect(d / 'mailbox.sqlite3')) as conn:
                conn.execute(oracle.MAILBOX_SCHEMA)
                conn.commit()
        if scenario == 'missing-incarnation':
            (d / 'QA.json').unlink()
    parity(tmp_path, prepare, [send(), ['msg', 'sent', 'alpha', '1'], ['ack', 'alpha', 'QA', '1']], [0, 0, 0])


@pytest.mark.parametrize('scenario', ['torn-line', 'stale-bus-lock', 'held-bus-lock'])
def test_go_bus_append_recovers_torn_lines_and_warns_without_losing_mail(tmp_path, scenario):
    def prepare(env, base):
        d = shift(env)
        if scenario == 'torn-line':
            (d / 'bus.jsonl').write_bytes(b'first\r\n\xfftorn')
        else:
            lock = d / 'bus.lock'
            lock.touch()
            if scenario == 'stale-bus-lock':
                os.utime(lock, (time.time() - 20,) * 2)
    parity(tmp_path, prepare, [send()], [0])


def test_go_claim_fresh_lock_timeout_preserves_owner_state(tmp_path):
    def prepare(env, base):
        (shift(env) / 'claims.lock').touch()
    parity(tmp_path, prepare, lambda env, base: [['claim', 'alpha', 'QA', str(base / 'held')]], [1])


@pytest.mark.parametrize('command', [
    ['claim'], ['claim', 'alpha', 'QA'], ['release'], ['ack', 'alpha', 'QA', 'bad'],
    ['msg'], ['msg', 'send', 'alpha', 'Boss', 'QA', '--kind', 'invalid', '--body', 'x'],
    ['msg', 'sent', 'alpha', 'bad'], ['build', 'alpha', '--cancel'],
    ['claim', '--help'], ['release', '--help'], ['msg', '--help'],
    ['msg', 'send', '--help'], ['msg', 'sent', '--help'], ['ack', '--help'], ['build', '--help'],
    ['claim', '--he'], ['release', '--he'], ['claim', '--bogus'], ['release', '--bogus'],
])
def test_go_write_parser_exact_bytes(tmp_path, command):
    parity(tmp_path, noop, [command])


@pytest.mark.parametrize('scenario', ['success', 'exit-seven', 'config-default', 'worktree-cwd',
                                      'no-cwd', 'missing-cwd', 'no-command', 'empty-command',
                                      'invalid-config', 'stale-lock', 'live-timeout', 'empty-timeout',
                                      'stale-empty-lock', 'cancel-finished', 'reserve-number'])
def test_go_build_records_logs_config_and_locks(tmp_path, scenario):
    def prepare(env, base):
        cwd = base / 'build working Ω folder'
        cwd.mkdir()
        d = shift(env)
        crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
        if scenario not in ('no-cwd', 'worktree-cwd'):
            crew['cwd'] = str(cwd)
        write_json(d / 'crew.json', crew)
        if scenario == 'worktree-cwd':
            write_json(d / 'worktree.json', {'repo_root': str(cwd), 'worktree_dir': str(cwd)})
        if scenario in ('config-default', 'worktree-cwd', 'invalid-config'):
            p = cwd / '.nightshift' / 'config.yaml'
            p.parent.mkdir()
            p.write_text('build:\n  cmd: "echo from config"\n  timeout: 0\n' if scenario != 'invalid-config'
                         else 'build:\n  timeout: true\n', encoding='utf-8')
        if 'lock' in scenario or 'timeout' in scenario:
            key = re.sub(r'[:\\/]', '_', str(cwd.resolve()).lower()) + '.json'
            lock = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'locks' / 'builds' / key
            lock.parent.mkdir(parents=True)
            if scenario in ('empty-timeout', 'stale-empty-lock'):
                lock.touch()
                if scenario == 'stale-empty-lock':
                    os.utime(lock, (time.time() - 60,) * 2)
            else:
                write_json(lock, {'pid': os.getpid() if scenario == 'live-timeout' else 2147483000,
                                  'shift': 'holder', 'n': 99, 'started': 'held'})
        if scenario in ('cancel-finished', 'reserve-number'):
            write_json(d / 'builds' / '3.json', {'n': 3, 'status': 'passed'})
            (d / 'builds' / 'nonnumeric.json').write_bytes(b'{}')

    def commands(env, base):
        if scenario == 'cancel-finished':
            return [['build', 'alpha', '3', '--cancel']]
        command = ['build', 'alpha']
        if scenario == 'missing-cwd':
            command += ['--cwd', str(base / 'absent')]
        if scenario not in ('config-default', 'worktree-cwd', 'no-command'):
            command += ['--cmd', '' if scenario == 'empty-command' else
                        'exit /b 7' if scenario == 'exit-seven' else 'cd']
        if scenario in ('live-timeout', 'empty-timeout'):
            command += ['--timeout', '0']
        return [command]

    exit_code = 7 if scenario == 'exit-seven' else 6 if scenario in ('live-timeout', 'empty-timeout') else (
        2 if scenario in ('no-cwd', 'missing-cwd', 'no-command', 'empty-command') else
        1 if scenario == 'invalid-config' else 0)
    parity(tmp_path, prepare, commands, [exit_code])


@pytest.mark.xdist_group('serial')   # 18 native writers on a 5 s claims-lock wait; parallel workers starved it (builds/15)
def test_go_concurrent_writers_preserve_all_rows_and_bus_sequences(tmp_path):
    env = clean_world(tmp_path)
    d = shift(env)
    def write_one(i):
        # Native writers contend on the same named mutex and bus/claims locks (the oracle is frozen).
        return run_cli(send('QA', 'Boss', 'note', f'concurrent-{i}'), env, go=True, timeout=40)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(write_one, range(18)))
    assert all(p.returncode == 0 and not p.stderr for p in runs), [(p.returncode, p.stderr) for p in runs]
    ids = [int(re.search(rb'msg:(\d+)', p.stdout)[1]) for p in runs]
    assert len(set(ids)) == 18
    events = [json.loads(line) for line in (d / 'bus.jsonl').read_bytes().splitlines()]
    assert [e['seq'] for e in events] == list(range(1, 19))
    assert [e['id'] for e in events] == sorted(ids)
    with contextlib.closing(sqlite3.connect(d / 'mailbox.sqlite3')) as conn:
        rows = conn.execute('SELECT id,body FROM messages WHERE id > 14 ORDER BY id').fetchall()
    assert rows == [(e['id'], e['body']) for e in events]
    assert {body for _, body in rows} == {f'concurrent-{i}' for i in range(18)}
    assert not list(d.glob('*.lock')) and not list(d.glob('*.tmp'))


@pytest.mark.parametrize('kind', ['claim', 'mailbox'])
@pytest.mark.parametrize('through_junction', [False, True])
def test_go_write_waits_for_existing_python_lock(tmp_path, kind, through_junction):
    env = clean_world(tmp_path)
    d = shift(env)
    args = ['claim', 'alpha', 'QA', str(tmp_path / 'file')] if kind == 'claim' else send()
    lock = (oracle.claims_locked(d) if kind == 'claim' else
            oracle.mailbox_guard(d / 'mailbox.sqlite3'))
    alias = tmp_path / 'junction state alias'
    routed = (junction_alias(alias, Path(env['NIGHTSHIFT_STATE_ROOT'])) if through_junction else
              contextlib.nullcontext(Path(env['NIGHTSHIFT_STATE_ROOT'])))
    with routed as go_root:
        go_env = {**env, 'NIGHTSHIFT_STATE_ROOT': str(go_root)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with lock:
                pending = pool.submit(run_cli, args, go_env, True)
                time.sleep(0.5)
                assert not pending.done(), 'native writer bypassed Python lock'
            proc = pending.result(timeout=20)
    assert proc.returncode == 0 and not proc.stderr, (proc.stdout, proc.stderr)


@pytest.mark.xdist_group('serial')   # 18 native claim writers on a 5 s lock wait; parallel workers starved it (builds/15)
def test_go_concurrent_claims_have_no_lost_updates(tmp_path):
    env = clean_world(tmp_path)
    def claim_one(i):
        return run_cli(['claim', 'alpha', 'QA', str(tmp_path / f'file {i}.py')], env, go=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(claim_one, range(18)))
    assert all(p.returncode == 0 and not p.stderr for p in runs)
    claims = json.loads((shift(env) / 'claims.json').read_bytes())
    assert set(claims) == {oracle.normalise_path(tmp_path / f'file {i}.py') for i in range(18)}
    assert {c['role'] for c in claims.values()} == {'QA'}
    assert not list(shift(env).glob('*.lock')) and not list(shift(env).glob('*.tmp'))


def test_go_builds_serialize_same_folder_and_reserve_distinct_records(tmp_path):
    env = clean_world(tmp_path)
    cwd = tmp_path / 'build folder with spaces'
    cwd.mkdir()
    # Exclusive create fails if the two actual child commands ever overlap.
    code = ("(open('exclusive.marker','x').close(), __import__('time').sleep(0.8), "
            "__import__('os').unlink('exclusive.marker'))")
    command = f'{sys.executable} -c "{code}"'
    args = ['build', 'alpha', '--cwd', str(cwd), '--cmd', command]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda native: run_cli(args, env, go=native), [True, True]))
    assert all(p.returncode == 0 and not p.stderr for p in runs), [(p.stdout, p.stderr) for p in runs]
    records = [json.loads(p.stdout) for p in runs]
    assert {r['n'] for r in records} == {1, 2}
    assert all(r['status'] == 'passed' and r['exit'] == 0 for r in records)
    assert not (cwd / 'exclusive.marker').exists()
    assert not list((Path(env['NIGHTSHIFT_STATE_ROOT']) / 'locks' / 'builds').glob('*.json'))


def test_go_writes_are_standalone_with_no_python_on_path_and_foreign_cwd(tmp_path):
    env = clean_world(tmp_path)
    foreign = tmp_path / 'foreign cwd'
    foreign.mkdir()
    exe = Path(os.environ.get('NIGHTSHIFT_GO_EXE', REPO / 'build' / 'nightshift.exe'))
    copied = foreign / 'native executable.exe'
    shutil.copyfile(exe, copied)
    restricted = {**os.environ, **env, 'PATH': str(Path(os.environ['SystemRoot']) / 'System32'),
                  'PYTHONPATH': '', 'PYTHONHOME': ''}
    commands = [['claim', 'alpha', 'QA', 'relative space.py'], ['release', 'alpha', 'QA'],
                send('QA', 'Boss', 'note', 'standalone write'), ['msg', 'sent', 'alpha', '15'],
                ['ack', 'alpha', 'Boss', '15']]
    for args in commands:
        proc = subprocess.run([str(copied), *args], cwd=foreign, env=restricted,
                              capture_output=True, timeout=20)
        assert proc.returncode == 0 and not proc.stderr, (args, proc.stdout, proc.stderr)
        if args[0] == 'claim':
            assert json.loads(proc.stdout)['claimed'] == [oracle.normalise_path(foreign / 'relative space.py')]
    assert json.loads((shift(env) / 'claims.json').read_bytes()) == {}
    with contextlib.closing(sqlite3.connect(shift(env) / 'mailbox.sqlite3')) as conn:
        assert conn.execute('SELECT state,body FROM messages WHERE id=15').fetchone() == (
            'acknowledged', 'standalone write')
    events = [json.loads(line) for line in (shift(env) / 'bus.jsonl').read_bytes().splitlines()]
    assert [e['event'] for e in events] == ['msg', 'sent', 'ack']


@pytest.mark.parametrize('dirty', [False, True])
def test_go_build_records_actual_git_head_and_dirty_tree(tmp_path, dirty):
    repo = tmp_path / 'git working folder'
    repo.mkdir()
    git_env = {**os.environ, 'GIT_AUTHOR_NAME': 'Parity Tester', 'GIT_AUTHOR_EMAIL': 'parity@example.invalid',
               'GIT_COMMITTER_NAME': 'Parity Tester', 'GIT_COMMITTER_EMAIL': 'parity@example.invalid'}
    def git(*args):
        proc = subprocess.run(['git', '-C', str(repo), *args], env=git_env,
                              capture_output=True, timeout=20)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.decode().strip()
    git('init', '-q')
    tracked = repo / 'tracked.txt'
    tracked.write_bytes(b'original\n')
    git('add', 'tracked.txt')
    git('commit', '-qm', 'fixture')
    head = git('rev-parse', 'HEAD')
    if dirty:
        tracked.write_bytes(b'changed\n')
        (repo / 'untracked.txt').write_bytes(b'untracked\n')
    def prepare(env, base):
        crew = json.loads((shift(env) / 'crew.json').read_bytes())
        crew['cwd'] = str(repo)
        write_json(shift(env) / 'crew.json', crew)
    # The fixture commit takes the wall-clock date, so its SHA is a volatile value of this run.
    outputs, _ = parity(tmp_path, prepare, [['build', 'alpha', '--cmd', 'cd']], [0], {'GIT_HEAD': head})
    # Normalized bytes are deliberately not parsed: inspect the persisted native record.
    record = json.loads((tmp_path / 'private write fixture' / 'state space' / 'shifts' /
                         'Nightshift-shift-alpha' / 'builds' / '1.json').read_bytes())
    assert record['head'] == head and record['dirty'] is dirty
    assert record['porcelain'] == ([' M tracked.txt', '?? untracked.txt'] if dirty else [])
    assert git('rev-parse', 'HEAD') == head


@pytest.mark.parametrize('override', [False, True])
def test_go_build_uses_canonical_repo_config_and_explicit_overrides(tmp_path, override):
    def prepare(env, base):
        repo, work = base / 'canonical repo', base / 'worktree'
        for directory, command in [(repo, 'echo canonical'), (work, 'echo stale worktree')]:
            p = directory / '.nightshift' / 'config.yaml'
            p.parent.mkdir(parents=True)
            p.write_text(f'build:\n  cmd: "{command}"\n  timeout: 0\n', encoding='utf-8')
        write_json(shift(env) / 'worktree.json', {'repo_root': str(repo), 'worktree_dir': str(work)})
    outputs, _ = parity(tmp_path, prepare, [['build', 'alpha', *(
        ['--cmd', 'echo explicit', '--timeout', '1'] if override else [])]], [0])
    d = tmp_path / 'private write fixture' / 'state space' / 'shifts' / 'Nightshift-shift-alpha'
    assert (d / 'builds' / '1.log').read_bytes() == (b'explicit\r\n' if override else b'canonical\r\n')


def test_go_build_explicit_timeout_overrides_zero_second_config_under_contention(tmp_path):
    for native in (True,):
        base = tmp_path / str(native)
        env = clean_world(base)
        cwd = base / 'build cwd'
        config = cwd / '.nightshift' / 'config.yaml'
        config.parent.mkdir(parents=True)
        config.write_text('build:\n  cmd: "echo configured"\n  timeout: 0\n', encoding='utf-8')
        key = re.sub(r'[:\\/]', '_', str(cwd.resolve()).lower()) + '.json'
        lock = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'locks' / 'builds' / key
        write_json(lock, {'pid': os.getpid(), 'n': 99, 'shift': 'holder', 'started': 'held'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(run_cli, ['build', 'alpha', '--cwd', str(cwd), '--timeout', '0.2'],
                                  env, native)
            time.sleep(0.7)
            assert not pending.done(), 'explicit timeout ignored in favor of config timeout=0'
            lock.unlink()
            proc = pending.result(timeout=20)
        assert proc.returncode == 0 and not proc.stderr, (proc.stdout, proc.stderr)
        record = json.loads(proc.stdout)
        assert record['status'] == 'passed' and record['cmd'] == 'echo configured'
        assert Path(record['log']).read_bytes() == b'configured\r\n'


@pytest.mark.parametrize('config', [
    '[]\n', 'null\n', 'unknown: value\n', 'build: []\n', 'build:\n  unknown: value\n',
    'build:\n  cmd: false\n', 'build:\n  timeout: -1\n', 'build:\n  timeout: .inf\n',
    'build:\n  timeout: "3"\n', 'build: [\n',
    'build:\n  cmd: "echo first"\n  cmd: "echo last"\n',
])
def test_go_build_yaml_validation_errors_and_duplicate_key_semantics(tmp_path, config):
    def prepare(env, base):
        cwd = base / 'config build cwd'
        p = cwd / '.nightshift' / 'config.yaml'
        p.parent.mkdir(parents=True)
        p.write_text(config, encoding='utf-8')
        crew = json.loads((shift(env) / 'crew.json').read_bytes())
        crew['cwd'] = str(cwd)
        write_json(shift(env) / 'crew.json', crew)
    parity(tmp_path, prepare, [['build', 'alpha']], [0 if 'echo last' in config else 1])

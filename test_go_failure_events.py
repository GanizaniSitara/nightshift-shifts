"""NGH-061 failure events and lifecycle commands (native only, no oracle).

Contract: C:/nightshift/shifts/<shift>/contract-ngh061.md (with its Addendum). Fixtures are private temp worlds:
the NGH-060 bus world (lead Boss = Foreman, Junior = Apprentice, Senior = Coder, QA = Tester; members record no pid
unless a test kills or revives one), throwaway git repositories, and for respawn the launcher's pane world with a
stub console helper that types into nothing. NIGHTSHIFT_NUDGE=off throughout; no window or session is ever launched.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

import pytest

from test_go_bus_commands import (INC, WAIT_HEADER, bus_world, cli, err, events, header, insert, one_line_failure,
                                  row, rows, text)
from test_go_launcher import (DEAD_PID, fake_bin, go_toolchain, pane_world, real_terminal_hwnd, run as launcher_run,
                              sleeper)
from test_go_readonly import write_json
from test_go_write_commands import shift


GIT_ENV = {'GIT_AUTHOR_NAME': 'Failure Tester', 'GIT_AUTHOR_EMAIL': 'tester@example.invalid',
           'GIT_COMMITTER_NAME': 'Failure Tester', 'GIT_COMMITTER_EMAIL': 'tester@example.invalid',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'}


@pytest.fixture
def world(tmp_path):
    return bus_world(tmp_path / 'failure world')


def member_file(env, label):
    return shift(env) / f'{label}.json'


def set_member(env, label, **fields):
    p = member_file(env, label)
    info = json.loads(p.read_text(encoding='utf-8'))
    info.update(fields)
    write_json(p, info)


def event_rows(env, name=None):
    return [r for r in rows(env) if r['kind'] == 'event' and (name is None or r['body'].startswith(name + ' ')
                                                              or r['body'] == name)]


def bus(env, name):
    return [e for e in events(env) if e.get('event') == name]


def patrol_tick(env):
    """NGH-066: member-died and asg-timeout are raised by the in-shift patrol, no longer by the lead's wait."""
    proc = cli(env, 'patrol', 'alpha', '--once')
    assert proc.returncode == 0, (proc.returncode, text(proc), err(proc))


def lead_wait(env, timeout='5'):
    """One patrol tick, then the lead's wait (which only returns rows)."""
    patrol_tick(env)
    return cli(env, 'wait', 'alpha', 'Boss', '--timeout', timeout)


def wait_lines(proc):
    assert proc.returncode == 0, (proc.returncode, text(proc), err(proc))
    first, _, rest = text(proc).partition('\n')
    match = WAIT_HEADER.match(first)
    assert match, first
    return match.groups(), rest.rstrip('\n')


def drain_lead(env):
    """One patrol tick, then consume every row the lead's wait returns; their (header groups, body)."""
    out = []
    patrol_tick(env)
    while True:
        proc = cli(env, 'wait', 'alpha', 'Boss', '--timeout', '1')
        if proc.returncode == 3:
            return out
        out.append(wait_lines(proc))


# ================================================================ A. member-died

def test_member_died_raised_on_lead_wait_with_active_asg(world):
    insert(world, 'assignment', 'Boss', 'QA', asg='ver-1', state='acknowledged')
    set_member(world, 'QA', pid=DEAD_PID)
    (msg_id, kind, asg, sender), body = wait_lines(lead_wait(world))
    assert (kind, asg, sender) == ('event', 'ver-1', 'Nightshift')
    assert body.splitlines()[0].startswith('member-died QA') and 'active asg ver-1' in body.splitlines()[0]
    r = row(world, int(msg_id))
    assert (r['kind'], r['sender'], r['recipient'], r['asg']) == ('event', 'Nightshift', 'Boss', 'ver-1')
    assert r['state'] == 'acknowledged'   # wait acknowledges it like any message
    died = bus(world, 'member-died')
    assert len(died) == 1
    assert (died[0]['role'], died[0]['pid'], died[0]['incarnation'], died[0]['asg']) == ('QA', DEAD_PID, INC['QA'], 'ver-1')
    acks = [e for e in bus(world, 'ack') if e.get('id') == int(msg_id)]
    assert len(acks) == 1 and acks[0].get('via') == 'wait'


def test_member_died_without_active_asg_has_null_asg(world):
    set_member(world, 'Senior', pid=DEAD_PID)
    (msg_id, kind, asg, _), body = wait_lines(lead_wait(world))
    assert kind == 'event' and asg == '-' and body.splitlines()[0] == 'member-died Senior'
    assert row(world, int(msg_id))['asg'] is None
    assert bus(world, 'member-died')[0]['asg'] is None


def test_member_died_active_asg_ignores_answered_assignment(world):
    insert(world, 'assignment', 'Boss', 'Senior', asg='code-a', state='acknowledged')
    insert(world, 'result', 'Senior', 'Boss', asg='code-a', body='CODER-DONE', state='acknowledged')
    set_member(world, 'Senior', pid=DEAD_PID)
    (_, _, asg, _), body = wait_lines(lead_wait(world))
    assert asg == '-' and 'active asg' not in body


def test_member_died_deduped_per_role_and_incarnation(world):
    set_member(world, 'QA', pid=DEAD_PID)
    assert len(drain_lead(world)) == 1
    assert len(drain_lead(world)) == 0
    assert len(event_rows(world, 'member-died')) == 1 and len(bus(world, 'member-died')) == 1
    # A new incarnation that dies again is a new subject.
    set_member(world, 'QA', pid=DEAD_PID, started=INC['QA'] + 50)
    raised = drain_lead(world)
    assert len(raised) == 1 and raised[0][1].startswith('member-died QA')
    assert [e['incarnation'] for e in bus(world, 'member-died')] == [INC['QA'], INC['QA'] + 50]


def test_member_died_one_event_per_dead_member(world):
    set_member(world, 'QA', pid=DEAD_PID)
    set_member(world, 'Senior', pid=DEAD_PID)
    bodies = sorted(b.splitlines()[0] for _, b in drain_lead(world))
    assert bodies == ['member-died QA', 'member-died Senior']


def test_member_died_not_raised_by_a_members_wait(world):
    set_member(world, 'Senior', pid=DEAD_PID)
    header(cli(world, 'ready', 'alpha', 'QA'))
    assert cli(world, 'wait', 'alpha', 'QA', '--timeout', '1').returncode == 3
    assert event_rows(world) == [] and bus(world, 'member-died') == []


def test_member_died_not_raised_for_starting_or_not_started_members(world):
    d = shift(world)
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    write_json(d / 'crew.json', {**crew, 'advisor_slot': 3})
    (d / 'Advisor.json').unlink()   # reserved Advisor: not_started
    # Junior, Senior, QA record no pid (starting) in this world.
    assert lead_wait(world, '1').returncode == 3
    assert event_rows(world) == []


def test_member_died_not_raised_for_live_member(world, sleeper):
    pid, started = sleeper
    set_member(world, 'QA', pid=pid, started=started)
    assert lead_wait(world, '1').returncode == 3
    assert event_rows(world) == []


def test_member_died_not_raised_after_down(world):
    set_member(world, 'QA', pid=DEAD_PID)
    p = shift(world) / 'bus.jsonl'
    with p.open('ab') as f:
        f.write(json.dumps({'seq': 999, 'ts': '2026-10-17T00:00:00', 'event': 'down',
                            'stopped': ['QA'], 'lock_released': []}).encode() + b'\r\n')
    lead_wait(world, '1')
    assert event_rows(world, 'member-died') == []


def test_inbox_lists_event_rows(world):
    set_member(world, 'QA', pid=DEAD_PID)
    set_member(world, 'Senior', pid=DEAD_PID)
    assert lead_wait(world).returncode == 0   # raises both, acknowledges the lower id only
    pending = [r for r in event_rows(world) if r['state'] != 'acknowledged']
    assert len(pending) == 1
    inbox = cli(world, 'inbox', 'alpha', 'Boss')
    assert inbox.returncode == 0, err(inbox)
    assert pending[0]['body'].encode() in inbox.stdout


# ================================================================ A. asg-timeout

def delivered(seconds_ago):
    return time.time() - seconds_ago


def test_asg_timeout_after_900s(world):
    source = insert(world, 'assignment', 'Boss', 'Junior', asg='code-a', state='sent', delivered_epoch=delivered(901))
    (msg_id, kind, asg, sender), body = wait_lines(lead_wait(world))
    assert (kind, asg, sender) == ('event', 'code-a', 'Nightshift') and body.splitlines()[0] == 'asg-timeout code-a'
    t = bus(world, 'asg-timeout')
    assert len(t) == 1 and (t[0]['asg'], t[0]['recipient']) == ('code-a', 'Junior')
    assert abs(t[0]['delivered_epoch'] - row(world, source)['delivered_epoch']) < 1e-3


def test_asg_timeout_applies_to_coder_uplift(world):
    insert(world, 'uplift', 'Boss', 'Senior', asg='code-b', state='acknowledged', reason='timeout',
           delivered_epoch=delivered(1000))
    raised = drain_lead(world)
    assert [b for _, b in raised] == ['asg-timeout code-b']


@pytest.mark.parametrize('case', ['too-recent', 'answered', 'tester', 'from-other', 'undelivered'])
def test_asg_timeout_not_raised(world, case):
    recipient = 'QA' if case == 'tester' else 'Junior'
    sender = 'Senior' if case == 'from-other' else 'Boss'
    epoch = None if case == 'undelivered' else delivered(870 if case == 'too-recent' else 1200)
    insert(world, 'assignment', sender, recipient, asg='code-a', state='stored' if epoch is None else 'sent',
           delivered_epoch=epoch)
    if case == 'answered':
        insert(world, 'result', 'Junior', 'Boss', asg='code-a', body='CODER-DONE', state='acknowledged')
    raised = [b for _, b in drain_lead(world)]
    assert not [b for b in raised if b.startswith('asg-timeout')], raised
    assert bus(world, 'asg-timeout') == []


def test_asg_timeout_result_from_earlier_incarnation_does_not_count(world):
    insert(world, 'assignment', 'Boss', 'Junior', asg='code-a', state='sent', delivered_epoch=delivered(1000))
    insert(world, 'result', 'Junior', 'Boss', asg='code-a', body='CODER-DONE', state='acknowledged',
           sender_incarnation=INC['Junior'] - 1)
    assert [b for _, b in drain_lead(world)] == ['asg-timeout code-a']


def test_asg_timeout_deduped_per_asg_and_recipient_incarnation(world):
    insert(world, 'assignment', 'Boss', 'Junior', asg='code-a', state='sent', delivered_epoch=delivered(1000))
    assert len(drain_lead(world)) == 1
    assert drain_lead(world) == []
    assert len(event_rows(world, 'asg-timeout')) == 1


# ================================================================ A. build --asg, signature, loop-detected, tree

@pytest.fixture
def build_dir(tmp_path):
    d = tmp_path / 'build cwd'
    d.mkdir()
    return d


def build(env, cwd, command, asg=None):
    args = ['build', 'alpha', '--cwd', str(cwd), '--cmd', command] + (['--asg', asg] if asg else [])
    return cli(env, *args, timeout=120)


def record(env, n):
    return json.loads((shift(env) / 'builds' / f'{n}.json').read_bytes())


FAIL_A = 'echo FAILED t.py::test_a - assert 1 == 2 & exit /b 1'
FAIL_B = 'echo FAILED t.py::test_b - assert 1 == 2 & exit /b 1'


def test_build_records_asg_and_signature_for_failed_builds(world, build_dir):
    assert build(world, build_dir, 'exit /b 0', 'code-a').returncode == 0
    assert build(world, build_dir, FAIL_A, 'code-a').returncode == 1
    ok, bad = record(world, 1), record(world, 2)
    assert ok['asg'] == 'code-a' and ok['status'] == 'passed' and not ok.get('signature')
    assert bad['asg'] == 'code-a' and bad['status'] == 'failed' and isinstance(bad['signature'], str) and bad['signature']
    assert build(world, build_dir, 'exit /b 0').returncode == 0
    assert record(world, 3).get('asg') is None


def test_loop_detected_on_fourth_build_runs_nothing(world, build_dir):
    for _ in range(3):
        assert build(world, build_dir, FAIL_A, 'code-a').returncode == 1
    sigs = {record(world, n)['signature'] for n in (1, 2, 3)}
    assert len(sigs) == 1
    marker = build_dir / 'fourth ran.txt'
    proc = build(world, build_dir, f'echo ran>"{marker}" & exit /b 1', 'code-a')
    line = one_line_failure(proc, 4)
    assert 'loop detected: escalate or report BLOCKED' in line
    assert not marker.exists(), 'the fourth build ran its command'
    assert not (shift(world) / 'builds' / '4.json').exists() or record(world, 4).get('status') not in (
        'running', 'passed', 'failed')
    ev = event_rows(world, 'loop-detected')
    assert len(ev) == 1 and ev[0]['body'].splitlines()[0] == 'loop-detected code-a'
    assert (ev[0]['sender'], ev[0]['recipient'], ev[0]['asg']) == ('Nightshift', 'Boss', 'code-a')
    b = bus(world, 'loop-detected')
    assert len(b) == 1 and b[0]['asg'] == 'code-a' and sorted(b[0]['builds']) == [1, 2, 3]
    assert b[0]['signature'] == sigs.pop()
    # Lead's wait prints the event.
    (_, kind, asg, sender), body = wait_lines(lead_wait(world))
    assert (kind, asg, sender, body) == ('event', 'code-a', 'Nightshift', 'loop-detected code-a')


def test_loop_detected_refusal_repeats_but_event_is_deduped(world, build_dir):
    for _ in range(3):
        build(world, build_dir, FAIL_A, 'code-a')
    one_line_failure(build(world, build_dir, FAIL_A, 'code-a'), 4)
    one_line_failure(build(world, build_dir, FAIL_A, 'code-a'), 4)
    assert len(event_rows(world, 'loop-detected')) == 1 and len(bus(world, 'loop-detected')) == 1


@pytest.mark.parametrize('case', ['different-failures', 'no-asg', 'other-asg', 'only-two'])
def test_loop_detection_does_not_trip(world, build_dir, case):
    steps = {'different-failures': [(FAIL_A, 'code-a'), (FAIL_B, 'code-a'), (FAIL_A, 'code-a')],
             'no-asg': [(FAIL_A, None)] * 3,
             'other-asg': [(FAIL_A, 'code-a'), (FAIL_A, 'code-a'), (FAIL_A, 'code-b')],
             'only-two': [(FAIL_A, 'code-a'), (FAIL_A, 'code-a')]}[case]
    for command, asg in steps:
        assert build(world, build_dir, command, asg).returncode == 1
    marker = build_dir / 'ran.txt'
    proc = build(world, build_dir, f'echo ran>"{marker}" & exit /b 1', 'code-a' if case != 'no-asg' else None)
    assert proc.returncode == 1 and marker.exists(), (proc.returncode, err(proc))
    assert event_rows(world, 'loop-detected') == []


def test_loop_detection_reset_by_passing_build(world, build_dir):
    for command in (FAIL_A, FAIL_A, 'exit /b 0', FAIL_A, FAIL_A):
        build(world, build_dir, command, 'code-a')
    marker = build_dir / 'ran.txt'
    # Same FAILED line (so the same signature), plus a marker proving it ran.
    proc = build(world, build_dir, f'echo FAILED t.py::test_a - assert 1 == 2 & echo ran>"{marker}" & exit /b 1', 'code-a')
    assert proc.returncode == 1 and marker.exists(), err(proc)
    # That was the third identical failure since the pass: the next one trips.
    one_line_failure(build(world, build_dir, FAIL_A, 'code-a'), 4)


def test_loop_signature_ignores_digits_and_timestamps_in_fallback(world, build_dir):
    for i in range(3):
        command = f'echo step {i} at 2026-10-17 20:0{i}:1{i} & echo error: boom after {i * 7} tries & exit /b 2'
        assert build(world, build_dir, command, 'code-a').returncode == 2
    assert len({record(world, n)['signature'] for n in (1, 2, 3)}) == 1
    one_line_failure(build(world, build_dir, 'exit /b 2', 'code-a'), 4)


def git(repo, *args):
    proc = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=30,
                          env={**os.environ, **GIT_ENV})
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout.decode('utf-8', 'replace').strip()


def make_repo(path):
    path.mkdir(parents=True)
    git(path, 'init', '-q', '-b', 'main')
    for key, value in [('core.autocrlf', 'false'), ('commit.gpgsign', 'false')]:
        git(path, 'config', key, value)
    (path / 'tracked.txt').write_bytes(b'original\n')
    (path / '.gitignore').write_bytes(b'ignored.txt\n')
    git(path, 'add', '-A')
    git(path, 'commit', '-qm', 'initial')
    return path


def expected_tree(repo, scratch):
    index = scratch / 'expected.index'
    env = {**os.environ, **GIT_ENV, 'GIT_INDEX_FILE': str(index)}
    for args in (['read-tree', 'HEAD'], ['add', '-A']):
        subprocess.run(['git', '-C', str(repo), *args], env=env, check=True, capture_output=True, timeout=30)
    tree = subprocess.run(['git', '-C', str(repo), 'write-tree'], env=env, check=True,
                          capture_output=True, timeout=30).stdout.decode().strip()
    index.unlink()
    return tree


def test_build_records_tree_including_untracked_without_touching_index(world, tmp_path):
    repo = make_repo(tmp_path / 'tree repo')
    (repo / 'tracked.txt').write_bytes(b'changed\n')
    (repo / 'new file.txt').write_bytes(b'untracked\n')
    (repo / 'ignored.txt').write_bytes(b'ignored\n')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    index_before = (repo / '.git' / 'index').read_bytes()
    status_before = git(repo, 'status', '--porcelain=v1', '--untracked-files=all')
    assert build(world, repo, 'exit /b 0').returncode == 0
    rec = record(world, 1)
    assert rec['tree'] == expected_tree(repo, scratch)
    assert rec['tree'] != git(repo, 'rev-parse', 'HEAD^{tree}')
    assert (repo / '.git' / 'index').read_bytes() == index_before, 'build touched the real index'
    assert git(repo, 'status', '--porcelain=v1', '--untracked-files=all') == status_before
    # Ignored files are not part of it: changing one keeps the tree id.
    (repo / 'ignored.txt').write_bytes(b'still ignored\n')
    assert expected_tree(repo, scratch) == rec['tree']


def test_build_tree_is_null_outside_git(world, build_dir):
    assert build(world, build_dir, 'exit /b 0').returncode == 0
    assert record(world, 1)['tree'] is None


# ================================================================ B. commit

@pytest.fixture
def commit_world(world, tmp_path):
    repo = make_repo(tmp_path / 'commit repo')
    d = shift(world)
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    write_json(d / 'crew.json', {**crew, 'cwd': str(repo)})
    write_json(d / 'worktree.json', {'repo_root': str(repo), 'worktree_dir': str(repo), 'branch': 'main',
                                     'target_branch': 'main', 'base_sha': git(repo, 'rev-parse', 'HEAD')})
    (repo / 'tracked.txt').write_bytes(b'feature\n')
    (repo / 'added file.txt').write_bytes(b'new\n')
    return world, repo


def passed(env, repo, n_expected, asg='code-a'):
    proc = build(env, repo, 'exit /b 0', asg)
    assert proc.returncode == 0, err(proc)
    assert record(env, n_expected)['exit'] == 0
    return record(env, n_expected)


def _tested_pass(env, n, via='verification', coding='code-a', body_prefix='TESTED-PASS'):
    if via == 'verification':
        insert(env, 'assignment', 'Boss', 'QA', asg=f'ver-{n}', state='acknowledged', coding_asg=coding)
        asg = f'ver-{n}'
    else:
        asg = coding
    return insert(env, 'result', 'QA', 'Boss', asg=asg, state='acknowledged',
                  body=f'{body_prefix} all green\nEvidence: builds/{n}.json head x dirty true exit 0; later builds/99.json')


def commit_cmd(env, *extra, message='Add the feature'):
    return cli(env, 'commit', 'alpha', 'code-a', '-m', message, *extra)


@pytest.mark.parametrize('via', ['verification', 'direct'])
def test_commit_commits_exactly_the_passed_tree(commit_world, tmp_path, via):
    env, repo = commit_world
    old_head = git(repo, 'rev-parse', 'HEAD')
    rec = passed(env, repo, 1)
    _tested_pass(env, 1, via=via)
    proc = commit_cmd(env)
    assert proc.returncode == 0, (text(proc), err(proc))
    sha = git(repo, 'rev-parse', 'HEAD')
    assert text(proc).strip() == f'committed {sha} for code-a (builds/1.json)'
    assert git(repo, 'rev-parse', 'HEAD^') == old_head, 'commit amended or skipped a parent'
    assert git(repo, 'rev-parse', 'HEAD^{tree}') == rec['tree']
    assert git(repo, 'log', '-1', '--format=%B') == 'Add the feature'
    assert git(repo, 'status', '--porcelain=v1', '--untracked-files=all') == ''
    c = bus(env, 'commit')
    assert len(c) == 1 and c[0]['asg'] == 'code-a' and c[0]['sha'] == sha
    assert str(c[0]['build']) in ('1', 'builds/1.json')


def test_commit_uses_latest_pass_and_its_first_build_reference(commit_world):
    env, repo = commit_world
    passed(env, repo, 1)
    _tested_pass(env, 1)
    (repo / 'tracked.txt').write_bytes(b'feature v2\n')
    rec = passed(env, repo, 2)
    _tested_pass(env, 2)
    proc = commit_cmd(env)
    assert proc.returncode == 0, err(proc)
    assert text(proc).strip().endswith('(builds/2.json)')
    assert git(repo, 'rev-parse', 'HEAD^{tree}') == rec['tree']


@pytest.mark.parametrize('problem', ['no-pass', 'only-fail', 'other-asg', 'missing-build', 'failed-build',
                                     'tree-changed', 'untracked-added'])
def test_commit_refuses_and_changes_nothing(commit_world, problem):
    env, repo = commit_world
    old_head = git(repo, 'rev-parse', 'HEAD')
    if problem != 'missing-build':
        passed(env, repo, 1)
    if problem == 'failed-build':
        rec = record(env, 1)
        write_json(shift(env) / 'builds' / '1.json', {**rec, 'exit': 1, 'status': 'failed'})
    if problem == 'only-fail':
        _tested_pass(env, 1, body_prefix='TESTED-FAIL')
    elif problem == 'other-asg':
        _tested_pass(env, 1, coding='code-z')
    elif problem != 'no-pass':
        _tested_pass(env, 1)
    if problem == 'tree-changed':
        (repo / 'tracked.txt').write_bytes(b'edited after the pass\n')
    if problem == 'untracked-added':
        (repo / 'late file.txt').write_bytes(b'after the pass\n')
    status_before = git(repo, 'status', '--porcelain=v1', '--untracked-files=all')
    one_line_failure(commit_cmd(env), 4)
    assert git(repo, 'rev-parse', 'HEAD') == old_head
    assert git(repo, 'status', '--porcelain=v1', '--untracked-files=all') == status_before
    assert git(repo, 'diff', '--cached', '--name-only') == ''
    assert bus(env, 'commit') == []


def test_commit_refuses_multiline_message_exit_2(commit_world):
    env, repo = commit_world
    old_head = git(repo, 'rev-parse', 'HEAD')
    passed(env, repo, 1)
    _tested_pass(env, 1)
    proc = commit_cmd(env, message='first line\nsecond line')
    assert proc.returncode == 2, (text(proc), err(proc))
    assert git(repo, 'rev-parse', 'HEAD') == old_head


# ================================================================ C. uplift

def _tester_failed_twice(env):
    insert(env, 'assignment', 'Boss', 'Junior', asg='code-a', state='acknowledged')
    for v in ('ver-a', 'ver-b'):
        insert(env, 'assignment', 'Boss', 'QA', asg=v, state='acknowledged', coding_asg='code-a')
        insert(env, 'result', 'QA', 'Boss', asg=v, body=f'TESTED-FAIL {v}', state='acknowledged')


def test_uplift_stores_validated_uplift_to_coder(world):
    _tester_failed_twice(world)
    header(cli(world, 'ready', 'alpha', 'Senior'))
    before = len(rows(world))
    proc = cli(world, 'uplift', 'alpha', 'code-a', '--reason', 'tester-failed-twice', 'take over code-a')
    msg_id, kind, asg = header(proc)
    assert (kind, asg) == ('uplift', 'code-a') and len(rows(world)) == before + 1
    r = row(world, msg_id)
    assert (r['sender'], r['recipient'], r['reason'], r['body']) == ('Boss', 'Senior', 'tester-failed-twice',
                                                                     'take over code-a')
    assert r['source_dispatch_id'] is not None and r['evidence']


@pytest.mark.parametrize('reason', ['timeout', 'needs-senior', 'tester-failed-twice'])
def test_uplift_refuses_unsatisfied_trigger(world, reason):
    insert(world, 'assignment', 'Boss', 'Junior', asg='code-a', state='acknowledged')
    header(cli(world, 'ready', 'alpha', 'Senior'))
    before = rows(world)
    line = one_line_failure(cli(world, 'uplift', 'alpha', 'code-a', '--reason', reason, 'x'), 4)
    assert f'uplift trigger not satisfied: {reason}' in line
    assert rows(world) == before


def test_uplift_refuses_unreleased_apprentice_claims(world, tmp_path):
    _tester_failed_twice(world)
    header(cli(world, 'ready', 'alpha', 'Senior'))
    assert cli(world, 'claim', 'alpha', 'Junior', str(tmp_path / 'held.py')).returncode == 0
    before = rows(world)
    one_line_failure(cli(world, 'uplift', 'alpha', 'code-a', '--reason', 'tester-failed-twice', 'x'), 4)
    assert rows(world) == before


@pytest.mark.parametrize('args', [['uplift', 'alpha', 'code-a', 'x'],
                                  ['uplift', 'alpha', 'code-a', '--reason', 'bored', 'x'],
                                  ['uplift', 'alpha', '--reason', 'timeout', 'x']])
def test_uplift_usage_errors_exit_2(world, args):
    assert cli(world, *args).returncode == 2
    assert rows(world) == []


def test_uplift_refused_when_crew_has_no_coder(world):
    _tester_failed_twice(world)
    d = shift(world)
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    write_json(d / 'crew.json', {**crew, 'members': [m for m in crew['members'] if m['role'] != 'Coder']})
    before = rows(world)
    line = one_line_failure(cli(world, 'uplift', 'alpha', 'code-a', 'x', '--reason', 'tester-failed-twice'), 4)
    assert line == 'shift Nightshift-shift-alpha has no Coder to uplift to; nothing stored', line
    assert rows(world) == before


def test_uplift_unknown_asg_refused(world):
    header(cli(world, 'ready', 'alpha', 'Senior'))
    one_line_failure(cli(world, 'uplift', 'alpha', 'absent', '--reason', 'timeout', 'x'), 4)
    assert len(rows(world)) == 1


# ================================================================ C. handover

def test_handover_writes_live_state(world, tmp_path):
    insert(world, 'assignment', 'Boss', 'Senior', asg='code-live', state='acknowledged',
           delivered_epoch=delivered(120))
    claimed = tmp_path / 'claimed thing.py'
    assert cli(world, 'claim', 'alpha', 'Senior', str(claimed)).returncode == 0
    write_json(shift(world) / 'waiting' / 'QA.json', {'pid': os.getpid(), 'since': time.time()})
    insert(world, 'result', 'QA', 'Boss', asg='ver-9', body='TESTED-PASS last report line\nmore',
           state='acknowledged')
    unacked = insert(world, 'note', 'Boss', 'Junior', body='an unacked note body', state='sent')
    proc = cli(world, 'handover', 'alpha')
    assert proc.returncode == 0, err(proc)
    path = Path(text(proc).strip().splitlines()[-1])
    assert path == shift(world) / 'handover.md'
    md = path.read_text(encoding='utf-8')
    for needle in ('code-live', 'Senior', 'claimed thing.py', 'QA', 'TESTED-PASS last report line',
                   f'msg:{unacked}'):
        assert needle in md, needle


# ================================================================ C. result

def result_cmd(env, *args):
    return cli(env, 'result', 'alpha', *args)


def result_json(env):
    return json.loads((shift(env) / 'result.json').read_bytes())


def build_record(env, n, **fields):
    rec = {'n': n, 'cmd': 'suite', 'cwd': 'C:/x', 'head': 'a' * 40, 'dirty': False, 'porcelain': [],
           'exit': 0, 'status': 'passed', 'asg': 'code-a', **fields}
    write_json(shift(env) / 'builds' / f'{n}.json', rec)


def test_result_done_implemented_writes_all_fields(world):
    write_json(shift(world) / 'Senior-usage.json', {'tokens': 1234})
    build_record(world, 1, exit=1, status='failed', asg=None)
    proc = result_cmd(world, 'done', '--stage', 'implemented', '--summary', 'did it café',
                      '--changed', 'a.py', '--changed', 'b dir/c.py',
                      '--evidence', 'implemented|unit suite|passed', '--evidence', 'tested|manual|skipped')
    assert proc.returncode == 0, err(proc)
    assert Path(text(proc).strip().splitlines()[-1]) == shift(world) / 'result.json'
    r = result_json(world)
    assert (r['status'], r['stage'], r['summary']) == ('done', 'implemented', 'did it café')
    assert r['changed'] == ['a.py', 'b dir/c.py']
    assert r['evidence'] == [{'stage': 'implemented', 'what': 'unit suite', 'outcome': 'passed'},
                             {'stage': 'tested', 'what': 'manual', 'outcome': 'skipped'}]
    assert Path(r['bus']) == shift(world) / 'bus.jsonl'
    assert [{k: b[k] for k in ('n', 'head', 'dirty', 'exit', 'asg')} for b in r['builds']] == [
        {'n': 1, 'head': 'a' * 40, 'dirty': False, 'exit': 1, 'asg': None}]
    assert isinstance(r['uplift_counts'], dict) and 'total' in r['uplift_counts']
    assert 'Senior' in json.dumps(r['usage'])


@pytest.mark.parametrize('builds', [[], [dict(exit=1, status='failed')], [dict(dirty=True)], [dict(dirty=None)]])
def test_result_done_tested_refused_without_clean_passed_build(world, builds):
    for n, fields in enumerate(builds, 1):
        build_record(world, n, **fields)
    before = (shift(world) / 'result.json').read_bytes()
    one_line_failure(result_cmd(world, 'done', '--stage', 'tested', '--summary', 'x'), 4)
    assert (shift(world) / 'result.json').read_bytes() == before


def git_cwd(env, repo):
    d = shift(env)
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    write_json(d / 'crew.json', {**crew, 'cwd': str(repo)})
    write_json(d / 'worktree.json', {'repo_root': str(repo), 'worktree_dir': str(repo), 'branch': 'main',
                                     'target_branch': 'main', 'base_sha': git(repo, 'rev-parse', 'HEAD')})


@pytest.mark.parametrize('status,stage', [('done', 'tested'), ('done', 'validated'), ('blocked', 'tested')])
def test_result_accepted_with_clean_pass_or_when_blocked(world, tmp_path, status, stage):
    repo = make_repo(tmp_path / 'result repo')
    git_cwd(world, repo)
    if status == 'done':
        build_record(world, 1, exit=1, status='failed', head=git(repo, 'rev-parse', 'HEAD'))
        build_record(world, 2, head=git(repo, 'rev-parse', 'HEAD'))
    proc = result_cmd(world, status, '--stage', stage, '--summary', 'ok')
    assert proc.returncode == 0, err(proc)
    assert (result_json(world)['status'], result_json(world)['stage']) == (status, stage)


def test_result_done_tested_refused_when_pass_is_on_an_older_head(world, tmp_path):
    repo = make_repo(tmp_path / 'result repo')
    git_cwd(world, repo)
    build_record(world, 1, head=git(repo, 'rev-parse', 'HEAD'))
    (repo / 'tracked.txt').write_bytes(b'moved on\n')
    git(repo, 'commit', '-qam', 'later commit')
    before = (shift(world) / 'result.json').read_bytes()
    one_line_failure(result_cmd(world, 'done', '--stage', 'tested', '--summary', 'x'), 4)
    assert (shift(world) / 'result.json').read_bytes() == before


@pytest.mark.parametrize('args', [['done'], ['finished', '--stage', 'tested', '--summary', 'x'],
                                  ['done', '--stage', 'shipped', '--summary', 'x'],
                                  ['done', '--stage', 'implemented', '--summary', 'x', '--evidence', 'only|two']])
def test_result_usage_errors(world, args):
    before = (shift(world) / 'result.json').read_bytes()
    proc = result_cmd(world, *args)
    assert proc.returncode == 2, (text(proc), err(proc))
    assert (shift(world) / 'result.json').read_bytes() == before


# ================================================================ C. failure lines

def test_claim_conflict_names_holder_and_next_step(world, tmp_path):
    path = tmp_path / 'contested.py'
    assert cli(world, 'claim', 'alpha', 'QA', str(path)).returncode == 0
    proc = cli(world, 'claim', 'alpha', 'Senior', str(path))
    assert proc.returncode == 4
    lines = [line for line in err(proc).splitlines() if line.strip()]
    assert 'QA' in err(proc) and 'already claimed by' in err(proc)
    assert lines[-1].startswith('ask Foreman to release or reassign ') and lines[-1].endswith('contested.py'), lines


def test_land_conflict_and_retest_lines(tmp_path):
    from test_go_lifecycle import cli as lcli, commit as lcommit, evidence, world as lworld
    env, d, repo, work, info = lworld(tmp_path / 'conflict')
    lcommit(work, 'shared.txt', 'crew conflict\n')
    lcommit(repo, 'shared.txt', 'canonical conflict\n')
    evidence(d, work)
    proc = lcli(['land', 'fixture'], env, True)
    assert proc.returncode == 4 and json.loads(proc.stdout)['status'] == 'conflict'
    assert err(proc).splitlines()[-1] == ('rebase conflict: assign the conflict to Coder in the worktree, '
                                          'have Tester pass it, then land again')

    env, d, repo, work, info = lworld(tmp_path / 'retest')
    lines = [f'line {i}\n' for i in range(1, 11)]
    base = lcommit(work, 'multi.txt', ''.join(lines), 'multi-line base')
    subprocess.run(['git', '-C', str(repo), 'merge', '-q', '--ff-only', base], check=True, capture_output=True,
                   env={**os.environ, **GIT_ENV})
    lcommit(work, 'multi.txt', ''.join(['crew first\n'] + lines[1:]))
    lcommit(repo, 'multi.txt', ''.join(lines[:-1] + ['main last\n']))
    evidence(d, work)
    proc = lcli(['land', 'fixture'], env, True)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert err(proc).splitlines()[-1] == 'rebased cleanly: have Tester pass the new HEAD, then land again'


def test_spawn_no_free_slot_lists_reserved_and_ops(tmp_path, sleeper):
    hwnd = real_terminal_hwnd()
    if hwnd is None:
        pytest.skip('needs an open Windows Terminal window to read (never launched here)')
    from test_go_launcher import base_env
    pid, started = sleeper
    env = base_env(tmp_path / 'spawn', None)
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-slotsfull'
    members = [{'role': r, 'label': r, 'identity': f'{d.name}-{r.lower()}', 'brief': f'{r.lower()}.md',
                'colour': 'red', 'human_contact': r == 'Foreman', 'tool': 'claude', 'model': 'claude-opus-5-5'}
               for r in ('Foreman', 'Advisor', 'Coder')]
    write_json(d / 'crew.json', {'crew': 'default', 'members': members, 'slots': 4, 'permission_mode': 'auto',
                                 'model': 'claude-opus-5-5', 'cwd': str(tmp_path), 'advisor_slot': 2})
    write_json(d / 'launch.json', {'hwnd': hwnd})
    for label in ('Foreman', 'Coder'):
        write_json(d / f'{label}.json', {'pid': pid, 'started': started, 'tool': 'claude'})
    for n, role in {1: 'Foreman', 2: None, 3: 'Coder', 4: None}.items():
        write_json(d / 'slots' / f'{n}.json', {'slot': n, 'role': role, 'cmd_pid': pid, 'started': 900.0,
                                                **({'spawned': 500.0} if role else {})})
    proc = launcher_run(['spawn', 'slotsfull', '--role', 'Helper', '--dry-run'], env, True)
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    stderr = proc.stderr.decode('utf-8')
    assert 'no free slot' in stderr
    last = [line for line in stderr.splitlines() if line.strip()][-1]
    assert '2' in last and 'Advisor' in last and 'Ops' in last and '4' in last, last


# ================================================================ C. respawn

STUB_AGENTS_CLI = r'''package main

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
)

// Records argv and stdin, types into nothing, and reports success the way agents-cli _attach inject does.
func main() {
	in, _ := io.ReadAll(os.Stdin)
	line, _ := json.Marshal(map[string]any{"argv": os.Args[1:], "stdin": string(in)})
	if f, err := os.OpenFile(filepath.Join(os.Getenv("INJECT_RECORD_DIR"), "inject.jsonl"),
		os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644); err == nil {
		f.Write(append(line, '\n'))
		f.Close()
	}
	os.Stdout.WriteString("{\"ok\": true}\n")
}
'''


@pytest.fixture(scope='session')
def inject_stub(tmp_path_factory):
    go = go_toolchain()
    if not go:
        pytest.skip('no Go toolchain to build the stub console helper')
    base = tmp_path_factory.mktemp('inject stub')
    (base / 'stub.go').write_text(STUB_AGENTS_CLI, encoding='utf-8')
    out = base / 'bin'
    out.mkdir()
    built = subprocess.run([go, 'build', '-o', str(out / 'console-helper.exe'), str(base / 'stub.go')], cwd=base,
                           capture_output=True, timeout=300,
                           env={**os.environ, 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off', 'GOFLAGS': '',
                                'GO111MODULE': 'off'})
    assert built.returncode == 0, built.stderr
    return out


@pytest.fixture
def respawn_world(tmp_path, fake_bin, sleeper, inject_stub):
    hwnd = real_terminal_hwnd()
    if hwnd is None:
        pytest.skip('respawn needs an open Windows Terminal window to read (never launched here)')
    env, d, repo, record = pane_world(tmp_path / 'respawn space', fake_bin, sleeper)
    write_json(d / 'launch.json', {'hwnd': hwnd})
    pid, _ = sleeper
    write_json(d / 'slots' / '3.json', {'slot': 3, 'role': 'Coder', 'cmd_pid': pid, 'started': 900.0,
                                         'spawned': 950.0})
    inject_record = tmp_path / 'inject record'
    inject_record.mkdir()
    respawn_env = {**env, 'INJECT_RECORD_DIR': str(inject_record), 'NIGHTSHIFT_NUDGE': 'off',
                   'NIGHTSHIFT_CONSOLE_HELPER': str(inject_stub / 'console-helper.exe')}
    return env, respawn_env, d, repo, inject_record


def mailbox_rows(d):
    with contextlib.closing(sqlite3.connect(d / 'mailbox.sqlite3')) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT * FROM messages ORDER BY id')]


def send_assignment(env, d, asg='code-a', body='implement café thing'):
    proc = launcher_run(['msg', 'send', d.name, 'Foreman', 'Coder', '--kind', 'assignment', '--body', body,
                         '--asg', asg], env, True)
    assert proc.returncode == 0, proc.stderr
    return int(re.search(rb'msg:(\d+)', proc.stdout)[1])


def test_respawn_refuses_running_member(respawn_world, sleeper):
    env, respawn_env, d, repo, inject_record = respawn_world
    pid, started = sleeper
    write_json(d / 'Coder.json', {'pid': pid, 'started': started, 'tool': 'codex'})
    proc = launcher_run(['respawn', d.name, 'Coder'], respawn_env, True)
    assert proc.returncode == 4
    assert [line for line in proc.stderr.decode().splitlines() if line.strip()] == ['Coder is running; nothing to do']
    assert not (inject_record / 'inject.jsonl').exists()


def test_respawn_redelivers_active_assignment_to_new_incarnation(respawn_world):
    env, respawn_env, d, repo, inject_record = respawn_world
    original = send_assignment(env, d)
    proc = launcher_run(['respawn', d.name, 'Coder'], respawn_env, True)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    out = proc.stdout.decode('utf-8').strip().splitlines()[-1]
    assert out.startswith('respawned Coder in slot 3') and 'asg code-a' in out, out
    pending = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['respawn']
    assert (pending['role'], pending['kind'], pending['asg'], pending['body']) == (
        'Coder', 'assignment', 'code-a', 'implement café thing')
    injected = [json.loads(line) for line in (inject_record / 'inject.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(injected) == 1 and injected[0]['argv'][:2] == ['_attach', 'inject']
    assert '--pane Coder' in injected[0]['stdin'] and '--slot 3' in injected[0]['stdin']
    ev = [json.loads(line) for line in (d / 'bus.jsonl').read_bytes().splitlines() if b'"respawn"' in line]
    assert len(ev) == 1 and (ev[0]['role'], ev[0]['slot'], ev[0]['asg'], ev[0]['msg']) == ('Coder', 3, 'code-a', original)
    # The pane start hook (stub codex, no real CLI) stores the same-asg row for the NEW incarnation.
    pane = launcher_run(['--pane', 'Coder', '--state-dir', str(d), '--start', '--cwd', str(repo), '--slot', '3'],
                        env, True, timeout=120)
    assert pane.returncode == 0, pane.stderr
    new_inc = json.loads((d / 'Coder.json').read_text(encoding='utf-8'))['started']
    assert new_inc != 1000.5
    again = [r for r in mailbox_rows(d) if r['id'] != original and r['asg'] == 'code-a' and r['recipient'] == 'Coder']
    assert len(again) == 1 and again[0]['kind'] == 'assignment' and again[0]['body'] == 'implement café thing'
    assert abs(again[0]['recipient_incarnation'] - new_inc) < 1e-6
    assert not json.loads((d / 'crew.json').read_text(encoding='utf-8')).get('respawn')


def test_respawn_without_active_assignment(respawn_world):
    env, respawn_env, d, repo, inject_record = respawn_world
    send_assignment(env, d)
    done = launcher_run(['msg', 'send', d.name, 'Coder', 'Foreman', '--kind', 'result', '--body', 'CODER-DONE',
                         '--asg', 'code-a'], env, True)
    assert done.returncode == 0, done.stderr
    proc = launcher_run(['respawn', d.name, 'Coder'], respawn_env, True)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert proc.stdout.decode().strip().splitlines()[-1].startswith('respawned Coder in slot 3 (no active assignment')
    assert not json.loads((d / 'crew.json').read_text(encoding='utf-8')).get('respawn')
    ev = [json.loads(line) for line in (d / 'bus.jsonl').read_bytes().splitlines() if b'"respawn"' in line]
    assert len(ev) == 1 and ev[0]['asg'] is None


def test_respawn_window_gone_rolls_back(respawn_world):
    env, respawn_env, d, repo, inject_record = respawn_world
    send_assignment(env, d)
    write_json(d / 'launch.json', {'hwnd': 0x7FFFFFFF})
    before = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    proc = launcher_run(['respawn', d.name, 'Coder'], respawn_env, True)
    assert proc.returncode != 0
    assert len([line for line in proc.stderr.decode().splitlines() if line.strip()]) == 1, proc.stderr
    after = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    assert {k: v for k, v in after.items() if k != 'respawn'} == before and not after.get('respawn')
    assert not (inject_record / 'inject.jsonl').exists()

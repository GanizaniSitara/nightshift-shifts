"""NGH-060 pull-based bus: nightshift ready / assign / note / report / wait (native only, no oracle).

Contract: C:/nightshift/shifts/<shift>/contract-ngh060.md. Every test runs on the private make_world fixture
(lead Boss = Foreman, Junior = Apprentice, Senior = Coder, QA = Tester; member PIDs are dead) with the fixture's
mailbox rows deleted so that only rows written here are addressed to anyone. NIGHTSHIFT_NUDGE=off: nothing is
ever typed into a pane.
"""
import concurrent.futures
import contextlib
import json
import os
import re
import sqlite3
import time

import pytest

from test_go_readonly import run_cli, write_json
from test_go_write_commands import clean_world, shift


HEADER = re.compile(r'^msg:(\d+) kind:(\S+) asg:(\S+)$')
WAIT_HEADER = re.compile(r'^msg:(\d+) kind:(\S+) asg:(\S+) from:(\S+)$')
INC = {'Boss': 100.25, 'Junior': 200.5, 'Senior': 300.75, 'QA': 400.0}


def bus_world(base):
    env = clean_world(base)
    # NGH-061: a recorded pid that is not running raises member-died on the lead's wait. These members keep their
    # incarnation ('started') but record no pid (status 'starting'), so only rows written here reach the lead.
    for label in INC:
        p = shift(env) / f'{label}.json'
        info = json.loads(p.read_text(encoding='utf-8'))
        info.pop('pid', None)
        write_json(p, info)
    advisor = shift(env) / 'Advisor.json'
    write_json(advisor, {k: v for k, v in json.loads(advisor.read_text(encoding='utf-8')).items() if k != 'pid'})
    with contextlib.closing(sqlite3.connect(shift(env) / 'mailbox.sqlite3')) as conn:
        conn.execute('DELETE FROM messages')
        conn.commit()
    return {**env, 'NIGHTSHIFT_NUDGE': 'off'}


def cli(env, *args, timeout=40):
    return run_cli(list(args), env, go=True, timeout=timeout)


def text(proc):
    return proc.stdout.decode('utf-8').replace('\r\n', '\n')


def err(proc):
    return proc.stderr.decode('utf-8').replace('\r\n', '\n')


def header(proc):
    assert proc.returncode == 0, (proc.returncode, text(proc), err(proc))
    match = HEADER.match(text(proc).strip())
    assert match, text(proc)
    return int(match[1]), match[2], match[3]


def rows(env):
    with contextlib.closing(sqlite3.connect(shift(env) / 'mailbox.sqlite3')) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT * FROM messages ORDER BY id')]


def row(env, msg_id):
    return next(r for r in rows(env) if r['id'] == msg_id)


def events(env):
    p = shift(env) / 'bus.jsonl'
    if not p.exists():
        return []
    out = []
    for line in p.read_bytes().splitlines():
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def insert(env, kind, sender, recipient, asg=None, body='body', state='stored', **extra):
    values = dict(kind=kind, sender=sender, sender_incarnation=INC[sender], recipient=recipient,
                  recipient_incarnation=INC[recipient], asg=asg, body=body,
                  created='2026-10-17 00:00:00', state=state)
    values.update(extra)
    with contextlib.closing(sqlite3.connect(shift(env) / 'mailbox.sqlite3')) as conn:
        cur = conn.execute('INSERT INTO messages (' + ','.join(values) + ') VALUES (' +
                           ','.join('?' for _ in values) + ')', list(values.values()))
        conn.commit()
        return cur.lastrowid


def one_line_failure(proc, code):
    assert proc.returncode == code, (proc.returncode, text(proc), err(proc))
    lines = [line for line in err(proc).splitlines() if line.strip()]
    assert len(lines) == 1, err(proc)
    return lines[0]


@pytest.fixture
def world(tmp_path):
    return bus_world(tmp_path / 'bus world')


# ---------------------------------------------------------------- ready

def test_ready_stores_ready_note_to_lead(world):
    msg_id, kind, asg = header(cli(world, 'ready', 'alpha', 'QA'))
    assert (kind, asg) == ('note', '-')
    r = row(world, msg_id)
    assert (r['kind'], r['sender'], r['recipient'], r['body'], r['asg']) == ('note', 'QA', 'Boss', 'READY', None)
    assert r['sender_incarnation'] == INC['QA'] and r['recipient_incarnation'] == INC['Boss']
    assert r['state'] == 'stored'   # nudge off, no live waiter
    assert any(e.get('event') == 'msg' and e.get('id') == msg_id for e in events(world))


def test_ready_accepts_role_aliases(world):
    msg_id, _, _ = header(cli(world, 'ready', 'alpha', 'mesh-qa'))
    assert row(world, msg_id)['sender'] == 'QA'


def test_ready_duplicate_from_same_incarnation_refused_exit_4(world):
    first, _, _ = header(cli(world, 'ready', 'alpha', 'QA'))
    line = one_line_failure(cli(world, 'ready', 'alpha', 'QA'), 4)
    assert 'already READY as QA' in line and f'msg:{first}' in line and 'nightshift wait' in line
    assert [r['id'] for r in rows(world)] == [first]


def test_ready_from_earlier_incarnation_does_not_block_new_ready(world):
    insert(world, 'note', 'QA', 'Boss', body='READY', sender_incarnation=399.0)
    msg_id, _, _ = header(cli(world, 'ready', 'alpha', 'QA'))
    assert row(world, msg_id)['sender_incarnation'] == INC['QA']


@pytest.mark.parametrize('args', [['ready'], ['ready', 'alpha'], ['ready', 'alpha', 'QA', 'extra']])
def test_ready_usage_error_exit_2(world, args):
    proc = cli(world, *args)
    assert proc.returncode == 2, (text(proc), err(proc))
    assert rows(world) == []


# ---------------------------------------------------------------- assign

def test_assign_explicit_asg_after_ready(world):
    header(cli(world, 'ready', 'alpha', 'QA'))
    msg_id, kind, asg = header(cli(world, 'assign', 'alpha', 'QA', 'verify it', '--asg', 'v1',
                                   '--ready-timeout', '5'))
    assert (kind, asg) == ('assignment', 'v1')
    r = row(world, msg_id)
    assert (r['kind'], r['sender'], r['recipient'], r['asg'], r['body']) == (
        'assignment', 'Boss', 'QA', 'v1', 'verify it')
    assert r['recipient_incarnation'] == INC['QA'] and r['state'] == 'stored'


def test_assign_generates_unique_asg(world):
    header(cli(world, 'ready', 'alpha', 'QA'))
    a = header(cli(world, 'assign', 'alpha', 'QA', 'one', '--ready-timeout', '5'))
    b = header(cli(world, 'assign', 'alpha', 'QA', 'two', '--ready-timeout', '5'))
    assert a[2] != '-' and b[2] != '-' and a[2] != b[2]
    assert row(world, a[0])['asg'] == a[2] and row(world, b[0])['asg'] == b[2]


def test_assign_ready_timeout_exit_3_stores_nothing(world):
    started = time.monotonic()
    line = one_line_failure(cli(world, 'assign', 'alpha', 'QA', 'too early', '--asg', 'v1',
                                '--ready-timeout', '1'), 3)
    assert time.monotonic() - started >= 0.9
    assert 'QA' in line and 'READY' in line
    assert rows(world) == []
    assert not any(e.get('event') == 'msg' for e in events(world))


def test_assign_ignores_ready_from_earlier_incarnation(world):
    insert(world, 'note', 'QA', 'Boss', body='READY', sender_incarnation=399.0)
    one_line_failure(cli(world, 'assign', 'alpha', 'QA', 'stale ready', '--ready-timeout', '1'), 3)
    assert len(rows(world)) == 1


def test_assign_treats_acknowledged_row_as_ready(world):
    # consult/summon members never send READY; an acknowledged row in the current incarnation suffices
    insert(world, 'assignment', 'Boss', 'QA', asg='first', state='acknowledged')
    msg_id, _, asg = header(cli(world, 'assign', 'alpha', 'QA', 'next', '--asg', 'second',
                                '--ready-timeout', '1'))
    assert asg == 'second' and row(world, msg_id)['asg'] == 'second'


def test_assign_acknowledged_row_from_earlier_incarnation_is_not_ready(world):
    insert(world, 'assignment', 'Boss', 'QA', asg='first', state='acknowledged', recipient_incarnation=399.0)
    one_line_failure(cli(world, 'assign', 'alpha', 'QA', 'next', '--asg', 'second', '--ready-timeout', '1'), 3)


def test_assign_waits_for_ready_that_arrives_later(world):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(cli, world, 'assign', 'alpha', 'QA', 'patient', '--asg', 'v1',
                              '--ready-timeout', '20')
        time.sleep(1.5)
        assert not pending.done(), 'assign stored before READY'
        assert rows(world) == []
        header(cli(world, 'ready', 'alpha', 'QA'))
        proc = pending.result(timeout=30)
    msg_id, _, asg = header(proc)
    assert asg == 'v1' and row(world, msg_id)['kind'] == 'assignment'


def test_assign_coding_asg_links_verification(world):
    header(cli(world, 'ready', 'alpha', 'Senior'))
    header(cli(world, 'ready', 'alpha', 'QA'))
    header(cli(world, 'assign', 'alpha', 'Senior', 'code it', '--asg', 'code-a', '--ready-timeout', '5'))
    msg_id, _, asg = header(cli(world, 'assign', 'alpha', 'QA', 'verify it', '--asg', 'ver-a',
                                '--coding-asg', 'code-a', '--ready-timeout', '5'))
    assert asg == 'ver-a' and row(world, msg_id)['coding_asg'] == 'code-a'


def test_assign_coding_asg_unknown_refused_and_nothing_stored(world):
    header(cli(world, 'ready', 'alpha', 'QA'))
    before = rows(world)
    proc = cli(world, 'assign', 'alpha', 'QA', 'verify', '--asg', 'ver-a', '--coding-asg', 'absent',
               '--ready-timeout', '5')
    assert proc.returncode != 0, text(proc)
    assert len([line for line in err(proc).splitlines() if line.strip()]) == 1, err(proc)
    assert rows(world) == before


def test_assign_reserved_member_asleep_exit_4(world):
    d = shift(world)
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    crew['advisor_slot'] = 3
    write_json(d / 'crew.json', crew)
    (d / 'Advisor.json').unlink()
    line = one_line_failure(cli(world, 'assign', 'alpha', 'Advisor', 'question', '--asg', 'adv-1',
                                '--ready-timeout', '1'), 4)
    assert 'asleep' in line and ('consult' in line or 'summon' in line)
    assert rows(world) == []


@pytest.mark.parametrize('args', [['assign', 'alpha', 'QA'], ['assign', 'alpha'],
                                  ['assign', 'alpha', 'QA', 'x', '--ready-timeout', 'soon']])
def test_assign_usage_error_exit_2(world, args):
    proc = cli(world, *args)
    assert proc.returncode == 2, (text(proc), err(proc))
    assert rows(world) == []


# ---------------------------------------------------------------- note

def test_note_has_no_generated_asg(world):
    header(cli(world, 'ready', 'alpha', 'QA'))
    msg_id, kind, asg = header(cli(world, 'note', 'alpha', 'QA', 'heads-up'))
    assert (kind, asg) == ('note', '-')
    r = row(world, msg_id)
    assert (r['kind'], r['sender'], r['recipient'], r['asg'], r['body']) == ('note', 'Boss', 'QA', None, 'heads-up')


def test_note_with_asg(world):
    header(cli(world, 'ready', 'alpha', 'QA'))
    msg_id, _, asg = header(cli(world, 'note', 'alpha', 'QA', 'about t1', '--asg', 't1'))
    assert asg == 't1' and row(world, msg_id)['asg'] == 't1'


def test_note_waits_for_ready_and_times_out_exit_3(world):
    one_line_failure(cli(world, 'note', 'alpha', 'QA', 'early', '--ready-timeout', '1'), 3)
    assert rows(world) == []


# ---------------------------------------------------------------- report

def test_report_defaults_asg_to_latest_dispatch(world):
    header(cli(world, 'ready', 'alpha', 'QA'))
    header(cli(world, 'assign', 'alpha', 'QA', 'first', '--asg', 'v1', '--ready-timeout', '5'))
    header(cli(world, 'assign', 'alpha', 'QA', 'second', '--asg', 'v2', '--ready-timeout', '5'))
    header(cli(world, 'note', 'alpha', 'QA', 'a note with its own asg', '--asg', 'not-a-dispatch'))
    msg_id, kind, asg = header(cli(world, 'report', 'alpha', 'QA', 'TESTED-PASS\ndetail line'))
    assert (kind, asg) == ('result', 'v2')
    r = row(world, msg_id)
    assert (r['kind'], r['sender'], r['recipient'], r['asg'], r['body']) == (
        'result', 'QA', 'Boss', 'v2', 'TESTED-PASS\ndetail line')


def test_report_default_asg_ignores_earlier_incarnation_and_falls_back_to_null(world):
    insert(world, 'assignment', 'Boss', 'QA', asg='old', recipient_incarnation=399.0)
    msg_id, _, asg = header(cli(world, 'report', 'alpha', 'QA', 'CODER-DONE nothing assigned'))
    assert asg == '-' and row(world, msg_id)['asg'] is None


def test_report_default_asg_follows_uplift(world):
    insert(world, 'assignment', 'Boss', 'Senior', asg='code-a', state='acknowledged')
    insert(world, 'uplift', 'Boss', 'Senior', asg='code-b', state='acknowledged', reason='timeout')
    _, _, asg = header(cli(world, 'report', 'alpha', 'Senior', 'CODER-DONE'))
    assert asg == 'code-b'


def test_report_explicit_asg_wins(world):
    insert(world, 'assignment', 'Boss', 'QA', asg='v1', state='acknowledged')
    msg_id, _, asg = header(cli(world, 'report', 'alpha', 'QA', 'TESTED-FAIL x', '--asg', 'other'))
    assert asg == 'other' and row(world, msg_id)['asg'] == 'other'


def test_report_apprentice_needs_senior_is_stored_as_note(world):
    insert(world, 'assignment', 'Boss', 'Junior', asg='code-a', state='acknowledged')
    msg_id, kind, asg = header(cli(world, 'report', 'alpha', 'Junior', 'APPRENTICE-NEEDS-SENIOR stuck\nwhy'))
    assert (kind, asg) == ('note', 'code-a')
    r = row(world, msg_id)
    assert (r['kind'], r['sender'], r['recipient'], r['body']) == (
        'note', 'Junior', 'Boss', 'APPRENTICE-NEEDS-SENIOR stuck\nwhy')


@pytest.mark.parametrize('body', ['READY', ''])
def test_report_refuses_ready_and_empty(world, body):
    proc = cli(world, 'report', 'alpha', 'QA', body)
    assert proc.returncode != 0, text(proc)
    line = [line for line in err(proc).splitlines() if line.strip()]
    assert len(line) == 1, err(proc)
    if body == 'READY':
        assert 'nightshift ready' in line[0]
    assert rows(world) == []


def test_report_ready_with_suffix_is_an_ordinary_result(world):
    msg_id, kind, _ = header(cli(world, 'report', 'alpha', 'QA', 'READY for review'))
    assert kind == 'result' and row(world, msg_id)['body'] == 'READY for review'


# ---------------------------------------------------------------- wait

def test_wait_timeout_exit_3_one_stdout_line(world):
    started = time.monotonic()
    proc = cli(world, 'wait', 'alpha', 'QA', '--timeout', '1')
    assert proc.returncode == 3, (text(proc), err(proc))
    assert time.monotonic() - started >= 0.9
    lines = text(proc).splitlines()
    assert len(lines) == 1 and lines[0].startswith('timeout: nothing for QA in 1s'), lines
    assert 'nightshift wait' in lines[0]
    assert not (shift(world) / 'waiting' / 'QA.json').exists()


def test_wait_prints_header_and_verbatim_body_and_acks(world):
    body = 'Café 🚀 "quoted" \\path\nline\ttwo\n  indented'
    header(cli(world, 'ready', 'alpha', 'QA'))
    msg_id, _, _ = header(cli(world, 'assign', 'alpha', 'QA', body, '--asg', 'v1', '--ready-timeout', '5'))
    proc = cli(world, 'wait', 'alpha', 'QA', '--timeout', '5')
    assert proc.returncode == 0, (text(proc), err(proc))
    first, _, rest = text(proc).partition('\n')
    assert WAIT_HEADER.match(first).groups() == (str(msg_id), 'assignment', 'v1', 'Boss'), first
    assert rest.rstrip('\n') == body
    assert row(world, msg_id)['state'] == 'acknowledged'
    acks = [e for e in events(world) if e.get('event') == 'ack' and e.get('id') == msg_id]
    assert len(acks) == 1 and acks[0].get('via') == 'wait' and acks[0].get('by') == 'QA', acks
    assert not (shift(world) / 'waiting' / 'QA.json').exists()


def test_wait_returns_lowest_id_first_then_next(world):
    header(cli(world, 'ready', 'alpha', 'QA'))
    a, _, _ = header(cli(world, 'note', 'alpha', 'QA', 'first'))
    b, _, _ = header(cli(world, 'note', 'alpha', 'QA', 'second'))
    one = cli(world, 'wait', 'alpha', 'QA', '--timeout', '5')
    two = cli(world, 'wait', 'alpha', 'QA', '--timeout', '5')
    assert text(one).startswith(f'msg:{a} ') and text(two).startswith(f'msg:{b} ')
    assert cli(world, 'wait', 'alpha', 'QA', '--timeout', '1').returncode == 3


def test_wait_skips_acknowledged_other_recipient_and_earlier_incarnation(world):
    insert(world, 'note', 'Boss', 'QA', body='acked', state='acknowledged')
    insert(world, 'note', 'Boss', 'QA', body='stale', recipient_incarnation=399.0)
    insert(world, 'note', 'Boss', 'Senior', body='for senior')
    proc = cli(world, 'wait', 'alpha', 'QA', '--timeout', '1')
    assert proc.returncode == 3, text(proc)
    assert [r['state'] for r in rows(world)] == ['acknowledged', 'stored', 'stored']


def test_wait_picks_up_sent_rows_written_by_msg_send(world):
    proc = cli(world, 'msg', 'send', 'alpha', 'QA', 'Boss', '--kind', 'result', '--body', 'old path', '--asg', 'v9')
    msg_id = int(re.search(rb'msg:(\d+)', proc.stdout)[1])
    assert cli(world, 'msg', 'sent', 'alpha', str(msg_id)).returncode == 0
    out = cli(world, 'wait', 'alpha', 'Boss', '--timeout', '5')
    assert out.returncode == 0, (text(out), err(out))
    assert text(out).splitlines()[:2] == [f'msg:{msg_id} kind:result asg:v9 from:QA', 'old path']
    assert row(world, msg_id)['state'] == 'acknowledged'


def test_wait_json_prints_row(world):
    header(cli(world, 'ready', 'alpha', 'QA'))
    msg_id, _, _ = header(cli(world, 'note', 'alpha', 'QA', 'json body', '--asg', 't1'))
    proc = cli(world, 'wait', 'alpha', 'QA', '--timeout', '5', '--json')
    assert proc.returncode == 0, (text(proc), err(proc))
    data = json.loads(proc.stdout)
    assert (data['id'], data['kind'], data['asg'], data['sender'], data['body']) == (
        msg_id, 'note', 't1', 'Boss', 'json body')


def test_wait_holds_waiter_file_while_blocked(world):
    waiter = shift(world) / 'waiting' / 'QA.json'
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(cli, world, 'wait', 'alpha', 'QA', '--timeout', '4')
        deadline = time.monotonic() + 3
        while not waiter.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert waiter.exists(), 'no waiter file while wait is blocked'
        info = json.loads(waiter.read_bytes())
        assert isinstance(info.get('pid'), int) and info['pid'] != os.getpid() and 'since' in info
        proc = pending.result(timeout=20)
    assert proc.returncode == 3
    assert not waiter.exists()


def test_second_wait_exiting_does_not_remove_first_waiters_file(world):
    # A member whose first wait was backgrounded runs a second one; when that second wait times out, the first is
    # still blocked and must still count as a live waiter (delivery without typing).
    waiter = shift(world) / 'waiting' / 'QA.json'
    header(cli(world, 'ready', 'alpha', 'QA'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cli, world, 'wait', 'alpha', 'QA', '--timeout', '15')
        deadline = time.monotonic() + 5
        while not waiter.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert cli(world, 'wait', 'alpha', 'QA', '--timeout', '1').returncode == 3
        assert waiter.exists(), 'second wait removed the waiter file of a wait that is still blocked'
        msg_id, _, _ = header(cli(world, 'assign', 'alpha', 'QA', 'still listening', '--asg', 'v1',
                                  '--ready-timeout', '5'))
        got = first.result(timeout=30)
    assert got.returncode == 0 and text(got).splitlines()[1] == 'still listening', text(got)
    sent = [e for e in events(world) if e.get('event') == 'sent' and e.get('id') == msg_id]
    assert len(sent) == 1 and sent[0].get('via') == 'wait', sent


def test_surviving_wait_reclaims_waiter_file_after_owner_exits(world):
    waiter = shift(world) / 'waiting' / 'QA.json'
    header(cli(world, 'ready', 'alpha', 'QA'))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cli, world, 'wait', 'alpha', 'QA', '--timeout', '3')
        deadline = time.monotonic() + 5
        while not waiter.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        owner_pid = json.loads(waiter.read_bytes())['pid']
        survivor = pool.submit(cli, world, 'wait', 'alpha', 'QA', '--timeout', '20')
        assert owner.result(timeout=20).returncode == 3
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError, ValueError):
                if json.loads(waiter.read_bytes())['pid'] != owner_pid:
                    break
            time.sleep(0.1)
        assert json.loads(waiter.read_bytes())['pid'] != owner_pid, 'survivor never re-claimed the waiter file'
        msg_id, _, _ = header(cli(world, 'note', 'alpha', 'QA', 'to the survivor'))
        got = survivor.result(timeout=30)
    assert got.returncode == 0 and text(got).splitlines()[1] == 'to the survivor', text(got)
    assert [e.get('via') for e in events(world) if e.get('event') == 'sent' and e.get('id') == msg_id] == ['wait']
    assert not waiter.exists()


@pytest.mark.parametrize('args', [['wait', 'alpha', 'QA', '--timeout', '-1'],
                                  ['assign', 'alpha', 'QA', 'x', '--ready-timeout', '-5'],
                                  ['note', 'alpha', 'QA', 'x', '--ready-timeout', '-0.5']])
def test_negative_timeouts_are_usage_errors(world, args):
    proc = cli(world, *args, timeout=15)
    one_line_failure(proc, 2)
    assert rows(world) == [] and not (shift(world) / 'waiting' / 'QA.json').exists()


def test_stale_waiter_file_is_not_a_live_waiter(world):
    write_json(shift(world) / 'waiting' / 'QA.json', {'pid': 2147483000, 'since': 1.0})
    header(cli(world, 'ready', 'alpha', 'QA'))
    msg_id, _, _ = header(cli(world, 'note', 'alpha', 'QA', 'nobody listening'))
    assert row(world, msg_id)['state'] == 'stored'
    assert not any(e.get('event') == 'sent' and e.get('id') == msg_id for e in events(world))


def test_nudge_to_dead_pane_keeps_row_stored_and_exits_0(world):
    env = {k: v for k, v in world.items() if k != 'NIGHTSHIFT_NUDGE'}
    env['NIGHTSHIFT_NUDGE'] = ''
    header(cli(env, 'ready', 'alpha', 'QA'))   # recipient Boss: PID 2147483000 is dead, nothing to type into
    proc = cli(env, 'note', 'alpha', 'QA', 'dead pane')
    msg_id, _, _ = header(proc)
    assert row(env, msg_id)['state'] == 'stored'
    assert not any(e.get('event') == 'sent' and e.get('id') == msg_id for e in events(env))
    failed = [e for e in events(env) if e.get('event') == 'nudge-failed' and e.get('id') == msg_id]
    assert len(failed) == 1 and failed[0].get('to') == 'QA' and failed[0].get('reason'), failed
    warning = [line for line in err(proc).splitlines() if line.strip()]
    assert len(warning) == 1 and f'msg:{msg_id}' in warning[0], warning


@pytest.mark.parametrize('args', [['wait'], ['wait', 'alpha'], ['wait', 'alpha', 'QA', '--timeout', 'x']])
def test_wait_usage_error_exit_2(world, args):
    assert cli(world, *args).returncode == 2


# ---------------------------------------------------------------- two processes

def test_member_waits_receives_assign_reports_and_lead_wait_prints_it(world):
    waiter = shift(world) / 'waiting' / 'QA.json'
    header(cli(world, 'ready', 'alpha', 'QA'))
    # The lead consumes QA's READY first so its later wait sees only the report.
    first = cli(world, 'wait', 'alpha', 'Boss', '--timeout', '5')
    assert text(first).splitlines()[1] == 'READY', text(first)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        member = pool.submit(cli, world, 'wait', 'alpha', 'QA', '--timeout', '30')
        deadline = time.monotonic() + 5
        while not waiter.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert waiter.exists()
        lead = pool.submit(cli, world, 'wait', 'alpha', 'Boss', '--timeout', '30')
        asg_id, _, asg = header(cli(world, 'assign', 'alpha', 'QA', 'verify c1', '--asg', 'ver-1',
                                    '--ready-timeout', '5'))
        got = member.result(timeout=40)
        assert got.returncode == 0, (text(got), err(got))
        assert text(got).splitlines() == [f'msg:{asg_id} kind:assignment asg:ver-1 from:Boss', 'verify c1']
        # Delivered to the live waiter without typing.
        sent = [e for e in events(world) if e.get('event') == 'sent' and e.get('id') == asg_id]
        assert len(sent) == 1 and sent[0].get('via') == 'wait', sent
        rep_id, kind, rep_asg = header(cli(world, 'report', 'alpha', 'QA', 'TESTED-PASS\nall green'))
        assert (kind, rep_asg) == ('result', 'ver-1')
        out = lead.result(timeout=40)
    assert out.returncode == 0, (text(out), err(out))
    assert text(out).splitlines() == [f'msg:{rep_id} kind:result asg:ver-1 from:QA', 'TESTED-PASS', 'all green']
    assert row(world, asg_id)['state'] == 'acknowledged' and row(world, rep_id)['state'] == 'acknowledged'
    assert not waiter.exists() and not (shift(world) / 'waiting' / 'Boss.json').exists()


# ---------------------------------------------------------------- old commands on new rows

def test_msg_sent_ack_and_inbox_work_on_rows_from_new_commands(world):
    ready_id, _, _ = header(cli(world, 'ready', 'alpha', 'QA'))
    note_id, _, _ = header(cli(world, 'note', 'alpha', 'QA', 'old-style consumer', '--asg', 't1'))
    inbox = cli(world, 'inbox', 'alpha', 'QA')
    assert inbox.returncode == 0, err(inbox)
    assert str(note_id).encode() in inbox.stdout and b'old-style consumer' in inbox.stdout
    lead_inbox = cli(world, 'inbox', 'alpha', 'Boss')
    assert lead_inbox.returncode == 0 and b'READY' in lead_inbox.stdout
    for msg_id in (ready_id, note_id):
        assert cli(world, 'msg', 'sent', 'alpha', str(msg_id)).returncode == 0
        assert row(world, msg_id)['state'] == 'sent'
    assert cli(world, 'ack', 'alpha', 'QA', str(note_id)).returncode == 0
    assert cli(world, 'ack', 'alpha', 'Boss', str(ready_id)).returncode == 0
    assert row(world, note_id)['state'] == 'acknowledged' and row(world, ready_id)['state'] == 'acknowledged'
    # An acknowledged row is never returned by wait.
    assert cli(world, 'wait', 'alpha', 'QA', '--timeout', '1').returncode == 3


def test_new_commands_listed_in_top_level_help(world):
    proc = cli(world, '--help')
    for name in ('wait', 'ready', 'assign', 'report', 'note'):
        assert re.search(rb'^  ' + name.encode() + rb'\b', proc.stdout, re.M), name

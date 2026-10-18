"""NGH-065 nudges and warden re-nudge (native only, no oracle).

Every nudge goes through `agents-cli send --force` (Claude panes included). The warden re-nudges an unacked row
older than 10 minutes whose recipient pid is alive and has no live waiter (bus `nudge-retry` {id, to, attempt}),
retries at least 10 minutes apart, and the third retry raises `member-stalled <Role>` to the lead once per
(role, msg id) plus a floor-bus line to the owner.

Fixtures: a private state root; members are our own harmless sleeper (a copy named claude.exe, so the pid counts as
a live pane); a stub agents-cli first on PATH records argv and reports success without typing into anything. The
warden runs made safe as in test_go_launcher's delivery test: no slots (nothing to recover), no session files (no
idle-Foreman nudge), no live-shift queue.
"""
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

import pytest

from test_go_launcher import (DEAD_PID, INVALID_HWND, REPO, base_env, fake_bin, go_toolchain, make_mailbox_rows,
                              run, sleeper, write_json)


SHIFT = 'Nightshift-shift-nudgefix'
MEMBERS = ('Foreman', 'Coder', 'Tester')
TEN_MIN = 600
# Retry bookkeeping the tests age (Tester msg 135, item 1); adjust here if the coder keeps it elsewhere.
RETRY_FILE = 'warden-retry.json'

STUB = r'''package main

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
)

// Records argv (and stdin) and reports a submitted send; types into nothing.
func main() {
	in, _ := io.ReadAll(os.Stdin)
	line, _ := json.Marshal(map[string]any{"argv": os.Args[1:], "stdin": string(in)})
	if f, err := os.OpenFile(filepath.Join(os.Getenv("STUB_RECORD_DIR"), "agents.jsonl"),
		os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644); err == nil {
		f.Write(append(line, '\n'))
		f.Close()
	}
	os.Stdout.WriteString("{\"submitted\": true, \"enter\": true, \"ok\": true}\n")
}
'''


@pytest.fixture(scope='session')
def agents_stub(tmp_path_factory):
    go = go_toolchain()
    if not go:
        pytest.skip('no Go toolchain to build the stub agents-cli')
    base = tmp_path_factory.mktemp('agents stub')
    (base / 'stub.go').write_text(STUB, encoding='utf-8')
    out = base / 'bin'
    out.mkdir()
    built = subprocess.run([go, 'build', '-o', str(out / 'agents-cli.exe'), str(base / 'stub.go')], cwd=base,
                           capture_output=True, timeout=300,
                           env={**os.environ, 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off', 'GOFLAGS': '',
                                'GO111MODULE': 'off'})
    assert built.returncode == 0, built.stderr
    return out


class World:
    def __init__(self, base, fake_bin, sleeper, agents_stub):
        self.pid, self.created = sleeper
        self.env = base_env(base, fake_bin)
        self.record = base / 'stub record'
        self.record.mkdir()
        self.env.update(STUB_RECORD_DIR=str(self.record),
                        PATH=str(agents_stub) + os.pathsep + self.env['PATH'])
        self.env.pop('NIGHTSHIFT_NUDGE', None)
        self.root = Path(self.env['NIGHTSHIFT_STATE_ROOT'])
        self.d = self.root / 'shifts' / SHIFT
        write_json(self.d / 'crew.json', {'crew': 'default', 'members': [
            {'label': m, 'role': m, 'identity': f'{SHIFT}-{m.lower()}', 'human_contact': m == 'Foreman',
             'tool': 'claude'} for m in MEMBERS]})
        write_json(self.d / 'launch.json', {'hwnd': INVALID_HWND})
        for m in MEMBERS:
            self.alive(m)
        make_mailbox_rows(self.d, [])

    def alive(self, label):
        write_json(self.d / f'{label}.json', {'pid': self.pid, 'started': self.created, 'tool': 'claude'})

    def dead(self, label):
        write_json(self.d / f'{label}.json', {'pid': DEAD_PID, 'started': self.created, 'tool': 'claude'})

    def insert(self, recipient='Coder', kind='assignment', asg='code-a', state='sent', age=TEN_MIN + 60,
               sender='Foreman', body='do the thing'):
        now = time.time()
        values = dict(kind=kind, sender=sender, sender_incarnation=self.created, recipient=recipient,
                      recipient_incarnation=self.created, asg=asg, body=body,
                      created=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now - age)), state=state,
                      delivered_epoch=None if state == 'stored' else now - age)
        with contextlib.closing(sqlite3.connect(self.d / 'mailbox.sqlite3')) as conn:
            cur = conn.execute('INSERT INTO messages (' + ','.join(values) + ') VALUES (' +
                               ','.join('?' for _ in values) + ')', list(values.values()))
            conn.commit()
            return cur.lastrowid

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.d / 'mailbox.sqlite3')) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute('SELECT * FROM messages ORDER BY id')]

    def events(self, name=None):
        p = self.d / 'bus.jsonl'
        out = []
        if p.exists():
            for line in p.read_bytes().splitlines():
                with contextlib.suppress(ValueError):
                    e = json.loads(line)
                    if name is None or e.get('event') == name:
                        out.append(e)
        return out

    def sends(self):
        p = self.record / 'agents.jsonl'
        calls = [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines()] if p.exists() else []
        return [c['argv'] for c in calls if c['argv'][:1] == ['send']]

    def warden(self):
        proc = run(['warden', SHIFT], self.env, True, timeout=120)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        return proc

    def age_retries(self, seconds=TEN_MIN + 60):
        """Move every recorded retry's last attempt into the past (the warden's own bookkeeping)."""
        p = self.d / RETRY_FILE
        data = json.loads(p.read_text(encoding='utf-8'))
        for v in data.values():
            v['at'] = float(v['at']) - seconds
        write_json(p, data)

    def floor_lines(self):
        p = self.root / 'floor' / 'bus.jsonl'
        return [json.loads(line) for line in p.read_bytes().splitlines() if line.strip()] if p.exists() else []


@pytest.fixture
def world(tmp_path, fake_bin, sleeper, agents_stub):
    return World(tmp_path / 'nudge world', fake_bin, sleeper, agents_stub)


def assert_forced_send(argv, pid):
    # Agreed shape (Foreman msg 137): agents-cli send <pid> <header> --force
    assert len(argv) == 4 and argv[0] == 'send' and argv[1] == str(pid) and argv[3] == '--force', argv


# ---------------------------------------------------------------- every nudge is forced

def test_store_time_nudge_uses_send_force_for_claude(world):
    assert run(['ready', SHIFT, 'Coder'], world.env, True).returncode == 0
    proc = run(['note', SHIFT, 'Coder', 'heads-up', '--ready-timeout', '5'], world.env, True)
    assert proc.returncode == 0, proc.stderr
    sends = world.sends()
    assert sends, 'the note must be nudged'
    for argv in sends:
        assert_forced_send(argv, world.pid)
    msg_id = int(re.search(rb'msg:(\d+)', proc.stdout)[1])
    assert any(e.get('id') == msg_id and e.get('via') == 'nudge' for e in world.events('sent'))


def test_warden_delivery_send_is_forced(world):
    world.insert(state='stored', age=5)
    world.warden()
    sends = world.sends()
    assert sends, 'the stored row is delivered'
    for argv in sends:
        assert_forced_send(argv, world.pid)


def test_assignment_with_nudge_suppressed_is_still_returned_by_wait(world):
    env = {**world.env, 'NIGHTSHIFT_NUDGE': 'off'}
    assert run(['ready', SHIFT, 'Coder'], env, True).returncode == 0
    proc = run(['assign', SHIFT, 'Coder', 'silent work', '--asg', 'code-q', '--ready-timeout', '5'], env, True)
    assert proc.returncode == 0, proc.stderr
    assert world.sends() == [], 'NIGHTSHIFT_NUDGE=off types nothing'
    got = run(['wait', SHIFT, 'Coder', '--timeout', '5'], env, True)
    assert got.returncode == 0, got.stderr
    lines = got.stdout.decode('utf-8').replace('\r\n', '\n').splitlines()
    assert re.match(r'msg:\d+ kind:assignment asg:code-q from:Foreman$', lines[0]) and lines[1] == 'silent work'


# ---------------------------------------------------------------- warden re-nudge

def test_warden_renudges_unacked_row_older_than_ten_minutes(world):
    msg_id = world.insert(age=TEN_MIN + 60)
    world.warden()
    retries = world.events('nudge-retry')
    assert len(retries) == 1 and (retries[0]['id'], retries[0]['to'], retries[0]['attempt']) == (msg_id, 'Coder', 1)
    sends = world.sends()
    assert len(sends) == 1
    assert_forced_send(sends[0], world.pid)
    assert sends[0][2].startswith(f'msg:{msg_id} '), sends[0]


@pytest.mark.parametrize('state', ['stored', 'sent'])
def test_warden_renudge_covers_unacked_states(world, state):
    msg_id = world.insert(state=state, age=TEN_MIN + 60)
    world.warden()
    assert [e['id'] for e in world.events('nudge-retry')] == [msg_id] or (
        state == 'stored' and [e['id'] for e in world.events('sent')] == [msg_id]), world.events()


@pytest.mark.parametrize('case', ['young', 'acked', 'dead', 'waiter', 'stale-incarnation', 'other-recipient-dead'])
def test_warden_does_not_renudge(world, case):
    msg_id = world.insert(age=TEN_MIN - 60 if case == 'young' else TEN_MIN + 60,
                          state='acknowledged' if case == 'acked' else 'sent')
    if case == 'dead':
        world.dead('Coder')
    if case == 'waiter':
        write_json(world.d / 'waiting' / 'Coder.json', {'pid': os.getpid(), 'since': time.time()})
    if case == 'stale-incarnation':
        set_started = json.loads((world.d / 'Coder.json').read_text(encoding='utf-8'))
        with contextlib.closing(sqlite3.connect(world.d / 'mailbox.sqlite3')) as conn:
            conn.execute('UPDATE messages SET recipient_incarnation = ? WHERE id = ?',
                         (set_started['started'] - 100, msg_id))
            conn.commit()
    if case == 'other-recipient-dead':
        world.dead('Tester')
        world.insert(recipient='Tester', asg='ver-a', age=TEN_MIN + 60)
        world.warden()
        assert [e['to'] for e in world.events('nudge-retry')] == ['Coder']
        return
    world.warden()
    assert world.events('nudge-retry') == [], world.events()
    assert world.sends() == []


def test_retries_are_at_least_ten_minutes_apart(world):
    world.insert(age=TEN_MIN + 60)
    world.warden()
    world.warden()
    world.warden()
    assert [e['attempt'] for e in world.events('nudge-retry')] == [1]
    world.age_retries(TEN_MIN - 60)       # 9 minutes since the last retry: still too soon
    world.warden()
    assert [e['attempt'] for e in world.events('nudge-retry')] == [1]
    world.age_retries(120)                # now 11 minutes
    world.warden()
    assert [e['attempt'] for e in world.events('nudge-retry')] == [1, 2]


def test_third_retry_raises_member_stalled_once_and_tells_the_owner(world):
    msg_id = world.insert(age=TEN_MIN + 60)
    for attempt in (1, 2, 3):
        world.warden()
        assert [e['attempt'] for e in world.events('nudge-retry')] == list(range(1, attempt + 1))
        world.age_retries()
    stalled = [r for r in world.rows() if r['kind'] == 'event' and r['body'].startswith('member-stalled')]
    assert len(stalled) == 1, stalled
    assert stalled[0]['body'].splitlines()[0].startswith('member-stalled Coder')
    assert (stalled[0]['sender'], stalled[0]['recipient']) == ('Nightshift', 'Foreman')
    assert len(world.events('member-stalled')) == 1
    floor = [f for f in world.floor_lines() if 'member-stalled' in json.dumps(f)]
    assert len(floor) == 1, floor
    assert (floor[0]['from'], floor[0]['to'], floor[0]['text']) == (
        'warden', 'owner', f'member-stalled Coder in {SHIFT} (msg:{msg_id})'), floor[0]
    # Retries stop at 3 (Foreman msg 148), and further patrols neither raise it again nor spam the owner.
    for _ in range(2):
        world.warden()
        world.age_retries()
    assert [e['attempt'] for e in world.events('nudge-retry')] == [1, 2, 3]
    assert len([argv for argv in world.sends() if argv[2].startswith(f'msg:{msg_id} ')]) == 3
    assert len([r for r in world.rows() if r['kind'] == 'event' and r['body'].startswith('member-stalled')]) == 1
    assert len([f for f in world.floor_lines() if 'member-stalled' in json.dumps(f)]) == 1
    # The lead's wait returns it like any message.
    got = run(['wait', SHIFT, 'Foreman', '--timeout', '5'], world.env, True)
    assert got.returncode == 0 and b'kind:event' in got.stdout and b'member-stalled Coder' in got.stdout, got.stdout


def test_member_stalled_is_per_message(world):
    first = world.insert(asg='code-a', age=TEN_MIN + 60)
    for _ in range(3):
        world.warden()
        world.age_retries()
    second = world.insert(asg='code-b', age=TEN_MIN + 60)
    for _ in range(3):
        world.warden()
        world.age_retries()
    stalled = [r for r in world.rows() if r['kind'] == 'event' and r['body'].startswith('member-stalled')]
    assert len(stalled) == 2, [r['body'] for r in stalled]
    assert {r['asg'] for r in stalled} <= {'code-a', 'code-b', None}


def test_acknowledging_stops_the_retries(world):
    msg_id = world.insert(age=TEN_MIN + 60)
    world.warden()
    assert run(['ack', SHIFT, 'Coder', str(msg_id)], world.env, True).returncode == 0
    world.age_retries()
    world.warden()
    assert [e['attempt'] for e in world.events('nudge-retry')] == [1]


# ---------------------------------------------------------------- Foreman page (c11)

def test_foreman_page_has_wait_when_idle_and_member_stalled_rule():
    text = (REPO / 'briefs' / 'foreman.md').read_text(encoding='utf-8')
    assert 'member-stalled' in text
    stalled = next(line for line in text.splitlines() if 'member-stalled' in line)
    assert len(stalled.split()) <= 40, stalled
    assert re.search(r'nothing (is )?in hand|nothing to do|idle', text, re.I) and '{nightshift} wait' in text


def test_foreman_page_stays_within_budget(tmp_path, fake_bin):
    env = base_env(tmp_path / 'budget', fake_bin)
    plain = tmp_path / 'budget plain'
    plain.mkdir()
    proc = run(['start', '--dry-run', '--cwd', str(plain), '--crew', 'default', 'x'], env, True)
    assert proc.returncode == 0, proc.stderr
    foreman = json.loads(proc.stdout)['roles']['Foreman']
    assert not [w for w in foreman['brief_warnings'] if ' role layer ' in w], foreman['brief_warnings']


def test_one_patrol_never_nudges_the_same_row_twice(world):
    """A stored row already older than 10 minutes (the warden was away, or delivery kept failing): the patrol's
    delivery nudge and the retry pass must not both type it; retries stay at least 10 minutes apart."""
    msg_id = world.insert(state='stored', age=TEN_MIN + 60)
    world.warden()
    sends = [argv for argv in world.sends() if argv[2].startswith(f'msg:{msg_id} ')]
    assert len(sends) == 1, sends
    world.warden()   # immediately again: the row was just delivered, nothing is due
    assert len([argv for argv in world.sends() if argv[2].startswith(f'msg:{msg_id} ')]) == 1

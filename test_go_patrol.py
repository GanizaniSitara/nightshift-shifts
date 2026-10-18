"""NGH-066 in-shift patrol: liveness, stalls, re-nudges, costs and the budget stop (native only, no oracle).

Contract: C:/nightshift/shifts/<shift>/contract-ngh066.md. The world is test_go_nudge_retry's: members are our own
harmless claude.exe sleeper, a stub console helper records every call (send, key, tail) and types into
nothing, and the shift has no slots and no session files. `patrol --once` runs one tick; the loop form runs with
NIGHTSHIFT_PATROL_INTERVAL=1 under a timeout. Usage figures are written into <Role>-usage.json in the contract schema,
because the stub's screen carries no footer. No window is opened.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from test_go_launcher import (DEAD_PID, REPO, base_env, fake_bin, pane_world, run, sleeper, write_json)
from test_go_nudge_retry import SHIFT, TEN_MIN, World, agents_stub


@pytest.fixture
def world(tmp_path, fake_bin, sleeper, agents_stub):
    return World(tmp_path / 'patrol world', fake_bin, sleeper, agents_stub)


def patrol(world, *extra, env=None, timeout=60):
    proc = run(['patrol', SHIFT, '--once', *extra], env or world.env, True, timeout=timeout)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return proc


def event_rows(world, name):
    return [r for r in world.rows() if r['kind'] == 'event' and r['body'].split()[:1] == [name]]


def calls(world, verb):
    if verb == 'send':
        return world.sends()
    p = world.record / 'agents.jsonl'
    if not p.exists():
        return []
    return [c['argv'][1:] for c in map(json.loads, p.read_text(encoding='utf-8').splitlines()) if c['argv'][:2] == ['_attach', verb]]


def write_usage(world, role, unit, value):
    sample = {'at': time.time(), 'unit': unit, 'value': value, 'source': 'test'}
    write_json(world.d / f'{role}-usage.json', {'role': role, 'samples': [sample], 'latest': sample})


def set_crew(world, **keys):
    crew = json.loads((world.d / 'crew.json').read_text(encoding='utf-8'))
    write_json(world.d / 'crew.json', {**crew, **keys})


# ---------------------------------------------------------------- liveness

def test_member_killed_mid_assignment_raises_member_died_in_one_tick(world):
    world.insert(recipient='Coder', asg='code-a', state='acknowledged', age=30)
    world.dead('Coder')
    # The lead's wait no longer raises it (moved into the patrol): nothing for the lead yet.
    assert run(['wait', SHIFT, 'Foreman', '--timeout', '1'], world.env, True).returncode == 3
    assert event_rows(world, 'member-died') == []
    patrol(world)
    died = event_rows(world, 'member-died')
    assert len(died) == 1 and died[0]['body'].startswith('member-died Coder') and 'active asg code-a' in died[0]['body']
    assert (died[0]['sender'], died[0]['recipient'], died[0]['asg']) == ('Nightshift', 'Foreman', 'code-a')
    patrol(world)
    assert len(event_rows(world, 'member-died')) == 1, 'deduped per (role, incarnation)'
    got = run(['wait', SHIFT, 'Foreman', '--timeout', '5'], world.env, True)
    assert got.returncode == 0 and b'member-died Coder' in got.stdout


def test_patrol_raises_asg_timeout(world):
    world.insert(recipient='Coder', asg='code-t', state='sent', age=1000)
    patrol(world)
    assert [r['body'].splitlines()[0] for r in event_rows(world, 'asg-timeout')] == ['asg-timeout code-t']


def test_patrol_quiet_for_a_healthy_shift(world):
    patrol(world)
    assert [r for r in world.rows() if r['kind'] == 'event'] == []
    assert calls(world, 'send') == [] and calls(world, 'key') == []


# ---------------------------------------------------------------- stalls via the patrol

def test_patrol_renudges_at_ten_minutes_and_raises_member_stalled_after_three(world):
    msg_id = world.insert(age=TEN_MIN - 60)
    patrol(world)
    assert world.events('nudge-retry') == [], 'under 10 minutes: no re-nudge'
    world.insert  # noqa: B018 (documentation: the row ages below)
    with __import__('contextlib').closing(__import__('sqlite3').connect(world.d / 'mailbox.sqlite3')) as conn:
        conn.execute('UPDATE messages SET delivered_epoch = delivered_epoch - 120 WHERE id = ?', (msg_id,))
        conn.commit()
    for attempt in (1, 2, 3):
        patrol(world)
        assert [e['attempt'] for e in world.events('nudge-retry')] == list(range(1, attempt + 1))
        world.age_retries()
    patrol(world)
    assert [e['attempt'] for e in world.events('nudge-retry')] == [1, 2, 3]
    stalled = event_rows(world, 'member-stalled')
    assert len(stalled) == 1 and stalled[0]['body'].startswith('member-stalled Coder')
    for argv in calls(world, 'send'):
        assert argv[-1] == '1', argv


def test_floor_warden_leaves_patrolled_shifts_alone(world, sleeper):
    world.insert(age=TEN_MIN + 60)
    write_json(world.d / 'patrol.json', {'pid': sleeper[0], 'started': sleeper[1]})   # a live patrol
    proc = run(['warden', SHIFT], world.env, True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert world.events('nudge-retry') == [] and calls(world, 'send') == []


def test_floor_warden_still_patrols_shifts_without_patrol_json(world):
    world.insert(age=TEN_MIN + 60)
    assert run(['warden', SHIFT], world.env, True, timeout=120).returncode == 0
    assert [e['attempt'] for e in world.events('nudge-retry')] == [1]


# ---------------------------------------------------------------- budget

def test_budget_warning_then_stop(world):
    set_crew(world, budget_usd=10)
    (world.d / 'result.json').unlink(missing_ok=True)
    write_usage(world, 'Coder', 'usd', 7.0)
    patrol(world)
    assert event_rows(world, 'budget-warning') == []            # 70%: below the warning line
    write_usage(world, 'Coder', 'usd', 8.5)
    patrol(world)
    warn = event_rows(world, 'budget-warning')
    assert len(warn) == 1 and warn[0]['body'].splitlines()[0].startswith('budget-warning usd'), warn
    patrol(world)
    assert len(event_rows(world, 'budget-warning')) == 1, 'warned once'
    assert calls(world, 'key') == [], 'a warning interrupts nothing'

    write_usage(world, 'Coder', 'usd', 6.0)
    write_usage(world, 'Tester', 'usd', 4.5)                   # 10.5 in total: over the budget
    patrol(world)
    stop = event_rows(world, 'budget-stop')
    assert len(stop) == 1 and stop[0]['body'].splitlines()[0].startswith('budget-stop usd'), stop
    keys = calls(world, 'key')
    assert keys and all(argv[1:] == [str(world.pid), 'escape'] for argv in keys), keys
    assert (world.d / 'handover.md').is_file()
    paused = world.events('paused')
    assert len(paused) == 1
    assert (paused[0]['unit'], paused[0]['total'], paused[0]['budget']) == ('usd', 10.5, 10)
    assert paused[0]['reason']
    result = json.loads((world.d / 'result.json').read_text(encoding='utf-8'))
    assert result['status'] == 'paused' and 'stage' in result
    assert result['summary'] == 'budget stop: usd 10.5/10', result['summary']

    patrol(world)
    assert len(event_rows(world, 'budget-stop')) == 1 and len(world.events('paused')) == 1


def test_budget_units_are_separate(world):
    set_crew(world, budget_usd=10, max_credits=100)
    write_usage(world, 'Coder', 'credits', 85)        # 85% of credits
    write_usage(world, 'Tester', 'usd', 1.0)          # 10% of usd
    write_usage(world, 'Foreman', 'context_left', 5)  # recorded, never budgeted
    patrol(world)
    bodies = [r['body'].splitlines()[0] for r in event_rows(world, 'budget-warning')]
    assert len(bodies) == 1 and bodies[0].startswith('budget-warning credits'), bodies
    assert event_rows(world, 'budget-stop') == []


def test_no_budget_keys_no_budget_events(world):
    write_usage(world, 'Coder', 'usd', 1e6)
    patrol(world)
    assert event_rows(world, 'budget-warning') == [] and event_rows(world, 'budget-stop') == []


def test_patrol_loop_stops_after_budget_stop(world):
    set_crew(world, budget_tokens=1000)
    write_usage(world, 'Coder', 'tokens', 2000)
    env = {**world.env, 'NIGHTSHIFT_PATROL_INTERVAL': '1'}
    started = time.monotonic()
    proc = run(['patrol', SHIFT], env, True, timeout=60)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert time.monotonic() - started < 50
    assert len(event_rows(world, 'budget-stop')) == 1


def test_patrol_loop_exits_when_the_shift_is_down(world):
    with (world.d / 'bus.jsonl').open('ab') as f:
        f.write(json.dumps({'seq': 1, 'ts': '2026-10-18T00:00:00', 'event': 'down', 'stopped': [],
                            'lock_released': []}).encode() + b'\r\n')
    env = {**world.env, 'NIGHTSHIFT_PATROL_INTERVAL': '1'}
    proc = run(['patrol', SHIFT], env, True, timeout=30)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_paused_shift_is_resumable_with_continue_from(world, tmp_path):
    set_crew(world, budget_usd=1)
    write_usage(world, 'Coder', 'usd', 2.0)
    patrol(world)
    assert json.loads((world.d / 'result.json').read_text(encoding='utf-8'))['status'] == 'paused'
    plain = tmp_path / 'resume plain'
    plain.mkdir()
    proc = run(['start', '--dry-run', '--cwd', str(plain), '--continue-from', str(world.d), 'resume the work'],
               world.env, True)
    assert proc.returncode == 0, proc.stderr
    # Foreman decision (v17): the plan carries no continue_from field; the resumed task prompt carries the paused
    # shift's handover text, whose header names that shift.
    handover_header = (world.d / 'handover.md').read_text(encoding='utf-8').splitlines()[0]
    assert SHIFT in handover_header, handover_header
    assert SHIFT in json.dumps(json.loads(proc.stdout)), 'the dry-run plan must carry the paused shift handover'


# ---------------------------------------------------------------- down stops the patrol

def test_down_stops_the_patrol(world, fake_bin):
    own = subprocess.Popen([str(fake_bin[1]), 'sleep', '120'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        time.sleep(0.5)
        write_json(world.d / 'patrol.json', {'pid': own.pid})
        for m in ('Foreman', 'Coder', 'Tester'):
            world.dead(m)
        proc = run(['down', SHIFT, '--hold'], world.env, True, timeout=120)
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        deadline = time.monotonic() + 15
        while own.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        assert own.poll() is not None, 'down must stop the patrol process recorded in patrol.json'
    finally:
        if own.poll() is None:
            own.kill()


# ---------------------------------------------------------------- Claude status line settings (c13)

def test_claude_pane_gets_shift_local_settings_and_user_settings_untouched(tmp_path, fake_bin, sleeper):
    env, d, repo, record = pane_world(tmp_path / 'claude pane', fake_bin, sleeper)
    stub_bin = tmp_path / 'claude pane' / 'stub bin'
    shutil.copyfile(fake_bin[0] / 'tasks-cli.exe', stub_bin / 'claude.exe')   # records argv, exits 7, never Claude
    user = Path(env['NIGHTSHIFT_HOME']) / '.claude' / 'settings.json'
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text('{"theme": "dark", "statusLine": {"type": "command", "command": "mine"}}', encoding='utf-8')
    before = user.read_bytes()
    proc = run(['--pane', 'Foreman', '--state-dir', str(d), '--start', '--cwd', str(repo), '--slot', '1'], env, True,
               timeout=120)
    assert proc.returncode == 0, proc.stderr
    launched = [json.loads(line) for line in (record / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    claude = [c for c in launched if c['exe'] == 'claude']
    assert claude, 'the stub claude must have been launched (not a real Claude)'
    argv = claude[0]['argv']
    assert '--settings' in argv, argv
    settings = Path(argv[argv.index('--settings') + 1])
    # One settings file per pane (Foreman msg 165): <shift>/<Role>.claude-settings.json
    assert settings.resolve() == (d / 'Foreman.claude-settings.json').resolve(), settings
    data = json.loads(settings.read_text(encoding='utf-8'))
    command = data['statusLine']['command']
    assert 'statusline' in command and '--state-dir' in command and '--role Foreman' in command, command
    assert data.get('permissions', {}).get('deny'), 'the Foreman file keeps its permissions deny'
    assert user.read_bytes() == before, "the user's ~/.claude/settings.json must not be edited"


# ---------------------------------------------------------------- Foreman lines (c13)

def test_foreman_page_has_budget_lines():
    text = (REPO / 'briefs' / 'foreman.md').read_text(encoding='utf-8')
    for event in ('budget-warning', 'budget-stop', 'member-stalled'):
        assert event in text, event
    warning = next(line for line in text.splitlines() if 'budget-warning' in line).lower()
    stop = next(line for line in text.splitlines() if 'budget-stop' in line).lower()
    assert 'human' in warning and ('in flight' in warning or 'finish' in warning), warning
    assert 'paused' in stop and 'stop' in stop, stop


CREWS_WITH_FOREMAN = sorted(p.stem for p in (REPO / 'crews').glob('*.yaml'))


@pytest.mark.parametrize('crew', CREWS_WITH_FOREMAN)
def test_foreman_within_budget_in_every_crew(tmp_path, fake_bin, crew):
    env = base_env(tmp_path / 'budget', fake_bin)
    plain = tmp_path / 'budget plain'
    plain.mkdir()
    proc = run(['start', '--dry-run', '--cwd', str(plain), '--crew', crew, 'x'], env, True)
    assert proc.returncode == 0, proc.stderr
    foreman = json.loads(proc.stdout)['roles'].get('Foreman')
    assert foreman is not None, crew
    assert not [w for w in foreman['brief_warnings'] if ' role layer ' in w], (crew, foreman['brief_warnings'])

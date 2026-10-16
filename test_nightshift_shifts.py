"""Unit tests for the Nightshift shifts launcher (nightshift_shifts.py).

Run: C:/miniconda3/envs/python312/python.exe -m pytest C:/git/nightshift-shifts/test_nightshift_shifts.py -q
Nothing here launches Windows Terminal or Claude: subprocess calls are faked.
"""
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nightshift_shifts as launcher  # noqa: E402

_real_notify = launcher.notify_owner   # restored only by tests that fake subprocess.run themselves


@pytest.fixture(autouse=True)
def _locks_in_tmp(monkeypatch, tmp_path):
    """No test reads or writes the real C:/Data/nightshift/locks."""
    monkeypatch.setattr(launcher, 'LOCKS_DIR', tmp_path / 'locks')


@pytest.fixture(autouse=True)
def notified(monkeypatch):
    """No test can reach Mailkeep: notify_owner only records (subject, body)."""
    sent = []
    monkeypatch.setattr(launcher, 'notify_owner',
                        lambda subject, body: sent.append((subject, body)) or {'sent': True, 'draft_id': 'fake'})
    return sent


# ── wt.exe argv ───────────────────────────────────────────────────────────────

def test_wt_command_is_one_tab_two_columns_foreman_left():
    argv = launcher.wt_command('Nightshift-shift-abc', Path('C:/x/state'), agents=True,
                               mode_flag='--start', cwd='C:/git/proj')
    assert argv[:6] == ['wt.exe', '-w', '-1', '--pos', '0,0', '--maximized']
    assert argv.count(';') == 2
    assert 'new-tab' in argv and 'split-pane' in argv and 'move-focus' in argv
    assert argv[argv.index('split-pane') + 1:argv.index('split-pane') + 4] == ['-V', '-s', '0.5']
    assert argv[argv.index('move-focus') + 1] == 'left'
    # start mode: short pane titles, crew profile, cwd passed through to the pane
    assert _titles(argv) == ['Nightshift-shift-abc Foreman', 'Nightshift-shift-abc Coder']
    assert argv.count('--profile') == 2 and launcher.PROFILE in argv
    assert argv.count('--cwd') == 2 and 'C:/git/proj' in argv
    assert '--start' in argv


def test_wt_command_trial_titles_carry_shift_name():
    argv = launcher.wt_command('Nightshift-trial-123', Path('C:/x'), agents=False)
    assert 'Nightshift | Foreman | Nightshift-trial-123' in argv
    assert '--profile' not in argv
    argv = launcher.wt_command('Nightshift-trial-123', Path('C:/x'), agents=True, mode_flag='--live', ticket='NGH-001')
    assert _titles(argv) == ['Nightshift | Foreman | Nightshift-trial-123', 'Nightshift | Coder | Nightshift-trial-123']


# ── pane, window and loop titles (NGH-023) ───────────────────────────────────

def test_start_pane_titles_use_the_ticket_else_the_shift_name():
    argv = launcher.wt_command('Nightshift-shift-abc', Path('C:/x'), agents=True, mode_flag='--start', cwd='C:/w',
                               ticket='NGH-019')
    assert _titles(argv) == ['NGH-019 Foreman', 'NGH-019 Coder']
    assert argv.count('--suppressApplicationTitle') == 2
    assert argv[argv.index('--pane') + 1] == 'Foreman'   # the wrapper still gets the bare role label
    argv = launcher.wt_command('Nightshift-shift-abc', Path('C:/x'), agents=True, mode_flag='--start', cwd='C:/w')
    assert _titles(argv) == ['Nightshift-shift-abc Foreman', 'Nightshift-shift-abc Coder']
    assert launcher.pane_title('NGH-019', 'Coder-2') == 'NGH-019 Coder-2'


def test_start_dry_run_from_a_ticket_titles_panes_with_it(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    monkeypatch.setattr(launcher, 'ticket_prompt', lambda t: (t, 'Ticket NGH-019: x', str(tmp_path)))
    launcher.start(['--dry-run', 'NGH-019'])
    plan = json.loads(capsys.readouterr().out)
    assert {r: v['pane_title'] for r, v in plan['roles'].items()} == {'Foreman': 'NGH-019 Foreman',
                                                                     'Coder': 'NGH-019 Coder'}
    assert _titles(plan['wt_argv']) == ['NGH-019 Foreman', 'NGH-019 Coder']


@pytest.mark.parametrize('tickets, title', [
    (['NGH-019'], 'Nightshift NGH-019'),
    (['NGH-019', 'NGH-020'], 'Nightshift NGH-019..NGH-020'),
    (['NGH-019', 'NGH-007', 'NGH-020'], 'Nightshift NGH-019..NGH-020'),
])
def test_loop_title(tickets, title):
    assert launcher.loop_title(tickets) == title


def test_finished_loop_titles_and_closes_its_own_window(monkeypatch, tmp_path):
    d = tmp_path / 'shift-NGH-040'
    d.mkdir()
    titled, closed = [], []
    monkeypatch.delenv('PYTEST_CURRENT_TEST')
    monkeypatch.setattr(launcher.ctypes.windll.kernel32, 'SetConsoleTitleW', lambda t: titled.append(t))
    monkeypatch.setattr(launcher, 'close_terminal_windows', lambda prefix: closed.append(prefix))
    monkeypatch.setattr(launcher, 'start', lambda argv: d)
    monkeypatch.setattr(launcher, 'wait_for_result', lambda state_dir, stall, **kw: {'status': 'done', 'stage': 'tested'})
    monkeypatch.setattr(launcher, 'down', lambda argv: None)
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess([], 0, b'', b''))
    launcher.loop(['NGH-040', 'NGH-041', '--no-notify'])
    assert titled == ['Nightshift NGH-040..NGH-041'] and closed == ['Nightshift NGH-040..NGH-041']


def test_architecture_doc_mentions_every_command():
    import ast
    import inspect
    import re
    tree = ast.parse(inspect.getsource(launcher.main))
    commands = next(n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', None) == 'commands')
    keys = [k.value for k in commands.keys]
    assert 'start' in keys and 'build' in keys
    doc = (Path(launcher.__file__).resolve().parent / 'docs' / 'ARCHITECTURE.md').read_text(encoding='utf-8')
    assert [k for k in keys if not re.search(rf'\b{re.escape(k)}\b', doc)] == []


# ── crew manifests (nightshift-crews/*.yaml) ─────────────────────────────────

def _crew_file(monkeypatch, tmp_path, name, text):
    crews = tmp_path / 'crews'
    crews.mkdir(exist_ok=True)
    (crews / f'{name}.yaml').write_text(text, encoding='utf-8')
    monkeypatch.setattr(launcher, 'CREWS_DIR', crews)


TRIO = """\
name: trio
model: claude-sonnet-5-5
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true}
  - {role: Coder, brief: coder.md, count: 3, colour: blue}
"""


def test_default_manifest_matches_the_original_crew():
    crew = launcher.load_crew()
    assert crew == {'name': 'default', 'model': 'claude-opus-5-5', 'permission_mode': 'auto',
                    'contract': 'result.json', 'layout': 'split', 'roles': [
                        {'role': 'Foreman', 'brief': 'foreman.md', 'count': 1, 'human_contact': True, 'colour': 'red'},
                        {'role': 'Coder', 'brief': 'coder.md', 'count': 1, 'human_contact': False, 'colour': 'green'}]}
    members = launcher.crew_members(crew, 'S')
    assert [(m['label'], m['identity'], m['peer']) for m in members] == [('Foreman', 'S-foreman', 'S-coder'),
                                                                          ('Coder', 'S-coder', 'S-foreman')]


def test_custom_crew_defaults_and_numbered_members(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'trio', TRIO)
    crew = launcher.load_crew('trio')
    assert crew['model'] == 'claude-sonnet-5-5' and crew['permission_mode'] == 'auto' and crew['contract'] == 'result.json'
    assert crew['roles'][0]['colour'] == 'red' and crew['roles'][1]['colour'] == 'blue'   # red default for human contact
    members = launcher.crew_members(crew, 'S')
    assert [m['label'] for m in members] == ['Foreman', 'Coder-1', 'Coder-2', 'Coder-3']
    assert [m['identity'] for m in members] == ['S-foreman', 'S-coder-1', 'S-coder-2', 'S-coder-3']
    assert members[0]['peer'] == 'S-coder-1, S-coder-2, S-coder-3'
    assert all(m['peer'] == 'S-foreman' and m['brief'] == 'coder.md' for m in members[1:])
    assert launcher.opening_message(members[2]) == launcher.START_MESSAGES['Coder']


def test_unknown_role_gets_a_generic_opening_and_default_green(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'rev', 'roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                                             '  - {role: Reviewer, brief: coder.md}\n')
    reviewer = launcher.crew_members(launcher.load_crew('rev'), 'S')[1]
    assert reviewer['colour'] == 'green'
    assert launcher.opening_message(reviewer) == 'Begin your Reviewer role: send READY to Foreman and await assignments.'


@pytest.mark.parametrize('text, message', [
    ('roles: []\n', 'non-empty list'),
    ('roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
     '  - {role: Coder, brief: coder.md, human_contact: true}\n', 'exactly one role needs human_contact'),
    ('roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
     '  - {role: Coder, brief: coder.md, count: 8}\n', '9 panes'),
    ('roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n', '1 panes'),
    ('roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true, count: 2}\n'
     '  - {role: Coder, brief: coder.md}\n', 'must have count 1'),
    ('roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
     '  - {role: Coder, brief: nope.md}\n', 'brief nope.md'),
    ('roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
     '  - {role: Coder, brief: coder.md, count: 0}\n', 'count for Coder'),
    ('roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
     '  - {role: Coder}\n', 'needs both role: and brief:'),
    ('layout: grid\nroles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
     '  - {role: Coder, brief: coder.md}\n', "layout 'grid'"),
])
def test_bad_manifests_are_refused_with_a_reason(monkeypatch, tmp_path, text, message):
    _crew_file(monkeypatch, tmp_path, 'bad', text)
    with pytest.raises(SystemExit, match=message):
        launcher.load_crew('bad')


def test_missing_crew_names_the_available_ones(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'trio', TRIO)
    with pytest.raises(SystemExit, match='available crews: trio'):
        launcher.load_crew('ghost')


# ── wt layout for 2-8 panes ───────────────────────────────────────────────────

def _layout(argv):
    """The wt subcommands with their split flags, pane titles dropped: [('split-pane', '-V', '0.6667'), ...]."""
    steps, i = [], 6
    while i < len(argv):
        if argv[i] == 'split-pane':
            steps.append(('split-pane', argv[i + 1], argv[i + 3]))
        elif argv[i] in ('new-tab', 'move-focus', 'focus-pane'):
            steps.append((argv[i],) + ((argv[i + 1],) if argv[i] == 'move-focus' else
                                        (argv[i + 2],) if argv[i] == 'focus-pane' else ()))
        i += 1
    return steps


def _titles(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == '--title']


@pytest.mark.parametrize('n, expected', [
    (2, [('new-tab',), ('split-pane', '-V', '0.5'), ('move-focus', 'left')]),
    (3, [('new-tab',), ('split-pane', '-V', '0.6667'), ('split-pane', '-V', '0.5'), ('focus-pane', '0')]),
    (4, [('new-tab',), ('split-pane', '-V', '0.75'), ('split-pane', '-V', '0.6667'), ('split-pane', '-V', '0.5'),
         ('focus-pane', '0')]),
    (6, [('new-tab',), ('split-pane', '-H', '0.5'), ('move-focus', 'up'), ('split-pane', '-V', '0.6667'),
         ('split-pane', '-V', '0.5'), ('move-focus', 'down'), ('split-pane', '-V', '0.6667'),
         ('split-pane', '-V', '0.5'), ('focus-pane', '0')]),
    (5, [('new-tab',), ('split-pane', '-H', '0.5'), ('move-focus', 'up'), ('split-pane', '-V', '0.6667'),
         ('split-pane', '-V', '0.5'), ('move-focus', 'down'), ('split-pane', '-V', '0.5'), ('focus-pane', '0')]),
])
def test_wt_layout_for_n_panes(n, expected):
    labels = [f'P{i}' for i in range(n)]
    argv = launcher.wt_command('S', Path('C:/x'), agents=True, mode_flag='--start', cwd='C:/w', members=labels)
    assert _layout(argv) == expected
    assert argv.count(';') == len(expected) - 1
    top = n if n <= 4 else -(-n // 2)
    # pane creation order: the top row's first pane, then (2 rows) the bottom row's first, then the rest in order
    order = labels if n <= 4 else [labels[0], labels[top]] + labels[1:top] + labels[top + 1:]
    assert _titles(argv) == [f'S {label}' for label in order]
    assert '--crew' not in argv


def test_wt_command_passes_a_non_default_crew_to_every_pane():
    argv = launcher.wt_command('S', Path('C:/x'), agents=True, mode_flag='--start', cwd='C:/w',
                               members=['Foreman', 'Coder-1', 'Coder-2'], crew='trio')
    assert argv.count('--crew') == 3 and argv[argv.index('--crew') + 1] == 'trio'


def test_start_dry_run_custom_crew(monkeypatch, tmp_path, capsys):
    _crew_file(monkeypatch, tmp_path, 'trio', TRIO)
    _fixed_uuid(monkeypatch)
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([]))
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', '--crew', 'trio', 'task'])
    plan = json.loads(capsys.readouterr().out)
    s = 'Nightshift-shift-12345678'
    assert plan['crew'] == 'trio' and plan['model'] == 'claude-sonnet-5-5'
    assert list(plan['roles']) == ['Foreman', 'Coder-1', 'Coder-2', 'Coder-3']
    assert plan['roles']['Foreman']['peer'] == f'{s}-coder-1, {s}-coder-2, {s}-coder-3'
    assert plan['roles']['Coder-2'] == {'session_name': f'{s}-coder-2', 'pane_title': f'{s} Coder-2', 'peer': f'{s}-foreman',
                                        'receives_prompt': False, 'first_message': launcher.START_MESSAGES['Coder']}
    assert plan['wt_argv'].count('--crew') == 4 and 'focus-pane' in plan['wt_argv']


def test_start_writes_crew_json_and_status_reads_its_labels(monkeypatch, tmp_path, capsys):
    cwd = _no_launch(monkeypatch, tmp_path)
    _crew_file(monkeypatch, tmp_path, 'trio', TRIO)
    state_dir = launcher.start(['--cwd', cwd, '--crew', 'trio', 'do it'])
    crew = json.loads((state_dir / 'crew.json').read_text(encoding='utf-8'))
    assert crew['crew'] == 'trio' and [m['label'] for m in crew['members']] == ['Foreman', 'Coder-1', 'Coder-2', 'Coder-3']
    (state_dir / 'Coder-2.json').write_text(json.dumps({'pid': 7, 'name': 'x'}), encoding='utf-8')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: True)
    assert list(launcher.shift_record(state_dir)['roles']) == ['Coder-2']


def test_pane_start_uses_the_manifest_member(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'trio', TRIO)
    state = tmp_path / 'Nightshift-shift-pane0001'
    state.mkdir()
    (state / 'prompt.txt').write_text('THE TASK', encoding='utf-8')
    seen = {}

    class Child:
        pid = 42

        def wait(self):
            return 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda cmd, **k: seen.setdefault('cmd', cmd) and Child())
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda child: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda child, colour, cwd: seen.setdefault('colour', colour))
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane('Coder-2', str(state), start=True, cwd=str(tmp_path), crew='trio')
    cmd = seen['cmd']
    assert cmd[cmd.index('--name') + 1] == 'Nightshift-shift-pane0001-coder-2'
    assert cmd[cmd.index('--model') + 1] == 'claude-sonnet-5-5' and seen['colour'] == 'blue'
    brief = cmd[cmd.index('--append-system-prompt') + 1]
    assert 'THE TASK' not in brief and 'Nightshift-shift-pane0001-foreman' in brief
    record = json.loads((state / 'Coder-2.json').read_text(encoding='utf-8'))
    assert record['model'] == 'claude-sonnet-5-5' and record['peer'] == 'Nightshift-shift-pane0001-foreman'
    seen.clear()
    launcher.pane('Foreman', str(state), start=True, cwd=str(tmp_path), crew='trio')
    assert seen['cmd'][seen['cmd'].index('--append-system-prompt') + 1].endswith('THE TASK') and seen['colour'] == 'red'
    with pytest.raises(SystemExit, match='not a member'):
        launcher.pane('Coder-9', str(state), start=True, cwd=str(tmp_path), crew='trio')


# ── role briefs live in nightshift-briefs/*.md ────────────────────────────────

def test_role_briefs_come_from_files_with_identities_filled_in():
    foreman = launcher.role_brief('Foreman', 'shift-1-foreman', 'shift-1-coder')
    coder = launcher.role_brief('Coder', 'shift-1-coder', 'shift-1-foreman')
    for brief in (foreman, coder):
        assert 'never launch Claude sessions' in brief           # the hard rule is in common.md
        assert not any(p in brief for p in ('{role}', '{identity}', '{peer_name}', '{state_dir}', '{shift}'))
    assert 'You are Foreman' in foreman and 'shift-1-coder' in foreman
    assert foreman.rstrip().endswith('verbatim:')               # the task text is appended after this
    assert 'send READY from shift-1-coder' in coder


# ── environment handed to the crew ────────────────────────────────────────────

def test_crew_env_drops_claude_markers_but_keeps_afk_and_forces_colour():
    env = launcher.crew_env({
        'CLAUDECODE': '1', 'CLAUDE_CODE_CHILD_SESSION': '1', 'CLAUDE_CODE_SESSION_ID': 'x',
        'CLAUDE_AFK_TIMEOUT_MS': '999999', 'NO_COLOR': '1', 'TERM': 'dumb', 'PATH': 'p',
    })
    assert 'CLAUDECODE' not in env and 'CLAUDE_CODE_CHILD_SESSION' not in env
    assert env['CLAUDE_AFK_TIMEOUT_MS'] == '999999'
    assert 'NO_COLOR' not in env
    assert env['TERM'] == 'xterm-256color' and env['COLORTERM'] == 'truecolor' and env['FORCE_COLOR'] == '3'
    assert env['PATH'] == 'p'


# ── ticket -> prompt + folder ─────────────────────────────────────────────────

def _fake_run(outputs):
    """Return a subprocess.run stand-in that replays canned (argv-prefix -> result) answers."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        for prefix, result in outputs:
            if argv[:len(prefix)] == prefix:
                return result if callable(result) is False else result(argv)
        raise AssertionError(f'unexpected subprocess call: {argv}')
    run.calls = calls
    return run


def test_ticket_prompt_uses_body_codebase_line(monkeypatch):
    task = {'task_id': 'NGH-003', 'title': 'Review', 'frontmatter': {},
            'body': 'codebase: C:\\git\\nightshift\n\nDo the thing.'}
    done = subprocess.CompletedProcess(['tasks-cli'], 0, stdout=json.dumps(task).encode(), stderr=b'')
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([(['tasks-cli', 'get', 'NGH-003'], done)]))
    ticket, prompt, cwd = launcher.ticket_prompt('NGH-003')
    assert ticket == 'NGH-003' and cwd == 'C:\\git\\nightshift'
    assert prompt.startswith('Ticket NGH-003: Review') and 'Do the thing.' in prompt
    assert 'tasks-cli note NGH-003' in prompt and 'never edit task files' in prompt


def test_ticket_prompt_prefers_frontmatter_codebase(monkeypatch):
    task = {'task_id': 'OP-001', 'title': 'T', 'frontmatter': {'codebase': 'C:\\git\\a'}, 'body': 'codebase: C:\\git\\b'}
    done = subprocess.CompletedProcess([], 0, stdout=json.dumps(task).encode(), stderr=b'')
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([(['tasks-cli'], done)]))
    assert launcher.ticket_prompt('OP-001')[2] == 'C:\\git\\a'


def test_ticket_prompt_fails_loudly_when_ticket_missing(monkeypatch):
    bad = subprocess.CompletedProcess([], 1, stdout=b'', stderr=b'not found')
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([(['tasks-cli'], bad)]))
    with pytest.raises(SystemExit, match='not found'):
        launcher.ticket_prompt('NGH-999')


def test_ticket_regex_matches_ids_only():
    assert launcher.TICKET_RE.fullmatch('NGH-002') and launcher.TICKET_RE.fullmatch('OP-123')
    assert not launcher.TICKET_RE.fullmatch('fix the bug') and not launcher.TICKET_RE.fullmatch('ngh-2')


# ── start --dry-run launches nothing ──────────────────────────────────────────

def test_start_dry_run_prints_plan_and_touches_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([]))   # any launch would blow up
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('Popen called'))
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', 'say "hi" & echo PWNED > x.txt'])
    plan = json.loads(capsys.readouterr().out)
    assert plan['dry_run'] is True and plan['permission_mode'] == 'auto'
    assert plan['prompt'] == 'say "hi" & echo PWNED > x.txt'
    assert all('PWNED' not in a for a in plan['wt_argv'])          # prompt never reaches argv
    assert plan['roles']['Foreman']['receives_prompt'] and not plan['roles']['Coder']['receives_prompt']
    assert not (tmp_path / 'shifts').exists() and not (tmp_path / 'x.txt').exists()


def _fixed_uuid(monkeypatch):
    monkeypatch.setattr(launcher.uuid, 'uuid4', lambda: launcher.uuid.UUID('12345678123456781234567812345678'))


def test_start_dry_run_default_crew_is_unchanged_golden(monkeypatch, tmp_path, capsys):
    """Golden captured from the pre-manifest launcher (NGH-015): the default crew's plan must not move."""
    _fixed_uuid(monkeypatch)
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    launcher.start(['--dry-run', 'task'])
    plan = json.loads(capsys.readouterr().out)
    shift = 'Nightshift-shift-12345678'
    state = str(Path('C:/Data/nightshift/shifts') / shift)
    cwd = str(work.resolve())
    py, script = sys.executable, str(Path(launcher.__file__).resolve())

    def pane_args(role):
        return ['--profile', 'Nightshift Crew', '--title', f'{shift} {role}', '--suppressApplicationTitle', '-d', '%USERPROFILE%',
                py, script, '--pane', role, '--state-dir', state, '--start', '--cwd', cwd]
    assert plan == {
        'dry_run': True, 'shift': shift, 'state_dir': state, 'cwd': cwd, 'ticket': None,
        'prompt_file': str(Path(state) / 'prompt.txt'), 'model': 'claude-opus-5-5', 'profile': 'Nightshift Crew',
        'permission_mode': 'auto', 'lock': str(launcher.lock_path(cwd)),
        'roles': {
            'Foreman': {'session_name': f'{shift}-foreman', 'pane_title': f'{shift} Foreman', 'peer': f'{shift}-coder',
                        'receives_prompt': True,
                        'first_message': 'Begin the assignment: the task text is at the end of your role instructions, '
                                         'after "The human task for this shift follows, verbatim:".'},
            'Coder': {'session_name': f'{shift}-coder', 'pane_title': f'{shift} Coder', 'peer': f'{shift}-foreman',
                      'receives_prompt': False,
                      'first_message': 'Begin your Coder role: send READY to Foreman and await assignments.'}},
        'wt_argv': ['wt.exe', '-w', '-1', '--pos', '0,0', '--maximized', 'new-tab', *pane_args('Foreman'),
                    ';', 'split-pane', '-V', '-s', '0.5', *pane_args('Coder'), ';', 'move-focus', 'left'],
        'prompt': 'task'}
    assert list(plan) == ['dry_run', 'shift', 'state_dir', 'cwd', 'ticket', 'prompt_file', 'model', 'profile',
                          'permission_mode', 'lock', 'roles', 'wt_argv', 'prompt']   # key order too


def test_start_rejects_empty_prompt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        launcher.start(['   '])


def test_start_rejects_missing_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        launcher.start(['--dry-run', '--cwd', str(tmp_path / 'nope'), 'task'])


# ── trust dialog auto-answer ──────────────────────────────────────────────────

def _peek(text):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps({'lines': [text]}).encode(), stderr=b'')


def test_accept_trust_prompt_presses_down_until_yes_is_highlighted_then_enter(monkeypatch):
    screens = iter([
        _peek(''),                                               # dialog not drawn yet
        _peek(' \u276f No, exit\n   Yes, I trust this folder'),  # dialog, No highlighted
        _peek('   No, exit\n \u276f Yes, I trust this folder'),  # after Down: Yes highlighted
    ])
    keys = []

    def run(argv, **kwargs):
        if argv[:2] == ['agents-cli', '_attach']:
            return next(screens)
        if argv[:2] == ['agents-cli', 'key']:
            keys.append(argv[3])
            return subprocess.CompletedProcess([], 0, stdout=b'{}', stderr=b'')
        raise AssertionError(argv)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher.time, 'sleep', lambda s: None)
    launcher.accept_trust_prompt(types.SimpleNamespace(pid=1, poll=lambda: None))
    assert keys == ['down', 'enter']


def test_accept_trust_prompt_does_nothing_past_the_banner(monkeypatch):
    keys = []

    def run(argv, **kwargs):
        if argv[:2] == ['agents-cli', '_attach']:
            return _peek('Claude Code v2.1.295')
        keys.append(argv)
        return subprocess.CompletedProcess([], 0, stdout=b'{}', stderr=b'')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher.time, 'sleep', lambda s: None)
    launcher.accept_trust_prompt(types.SimpleNamespace(pid=1, poll=lambda: None))
    assert keys == []


def test_accept_trust_prompt_survives_undecodable_peek(monkeypatch):
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: subprocess.CompletedProcess([], 0, stdout=b'\x9d\xff', stderr=b''))
    monkeypatch.setattr(launcher.time, 'sleep', lambda s: None)
    launcher.accept_trust_prompt(types.SimpleNamespace(pid=1, poll=lambda: 0))   # exits immediately


# ── status / down ─────────────────────────────────────────────────────────────

def _shift(tmp_path, name, pids, prompt='Ticket X: do it\nmore'):
    d = tmp_path / 'shifts' / name
    d.mkdir(parents=True)
    (d / 'prompt.txt').write_text(prompt, encoding='utf-8')
    for role, pid in pids.items():
        (d / f'{role}.json').write_text(json.dumps({'pid': pid, 'wrapper_pid': pid + 1000, 'role': role,
                                                    'name': f'{name}-{role.lower()}', 'cwd': 'C:/x'}), encoding='utf-8')
    return d


def test_status_lists_only_live_shifts_unless_all(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    _shift(tmp_path, 'Nightshift-shift-old1', {'Foreman': 11, 'Coder': 12})
    _shift(tmp_path, 'Nightshift-shift-new2', {'Foreman': 21, 'Coder': 22})
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: pid in (21, 22))
    launcher.status([])
    live = json.loads(capsys.readouterr().out)
    assert [r['shift'] for r in live] == ['Nightshift-shift-new2']
    assert live[0]['task'] == 'Ticket X: do it' and live[0]['roles']['Foreman']['alive'] is True
    launcher.status(['--all'])
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_pid_alive_compares_the_whole_pid_field(monkeypatch):
    csv = '"claude.exe","12334","Console","1","123,348 K"\r\n'
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: subprocess.CompletedProcess([], 0, stdout=csv.encode(), stderr=b''))
    assert launcher.pid_alive(12334) is True
    assert launcher.pid_alive(1233) is False      # prefix of the pid
    assert launcher.pid_alive(23348) is False     # appears only inside the memory column
    reused = '"svchost.exe","12334","Services","0","8,000 K"\r\n'
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: subprocess.CompletedProcess([], 0, stdout=reused.encode(), stderr=b''))
    assert launcher.pid_alive(12334) is False     # PID reused by a non-Claude process
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: subprocess.CompletedProcess([], 0, stdout=b'INFO: No tasks are running which match the specified criteria.\r\n', stderr=b''))
    assert launcher.pid_alive(12334) is False


def test_pid_alive_rejects_a_claude_process_started_at_the_wrong_time(monkeypatch):
    csv = '"claude.exe","500","Console","1","1 K"\r\n'
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: subprocess.CompletedProcess([], 0, stdout=csv.encode(), stderr=b''))
    monkeypatch.setattr(launcher, 'process_start_epoch', lambda pid: 1_000_000.0)
    assert launcher.pid_alive(500, started=1_000_100.0) is True     # same launch, within tolerance
    assert launcher.pid_alive(500, started=900_000.0) is False      # PID reused by a later Claude session


def test_prompt_is_empty_only_for_a_bare_prompt_line():
    assert launcher.prompt_is_empty(['❯ Begin the assignment', '  done', '❯ '])
    assert not launcher.prompt_is_empty(['❯ Begin the assignment', '❯ And at some point we should/color red'])
    assert not launcher.prompt_is_empty(['no prompt drawn yet'])


def test_inject_command_waits_for_an_empty_prompt_and_gives_up(monkeypatch):
    screens = iter([['❯ typing…'], ['❯ typing…'], ['❯ ']])
    sent = []
    monkeypatch.setattr(launcher, 'peek_lines', lambda pid: next(screens))
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: sent.append(argv[-1]) or subprocess.CompletedProcess([], 0, b'{}', b''))
    monkeypatch.setattr(launcher.time, 'sleep', lambda s: None)
    assert launcher.inject_command(1, '/color red') is True and sent == ['/color red']
    monkeypatch.setattr(launcher, 'peek_lines', lambda pid: ['❯ never empty'])
    assert launcher.inject_command(1, '/color red', tries=3) is False and sent == ['/color red']


def test_resolve_shift_by_suffix_and_latest(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    a = _shift(tmp_path, 'Nightshift-shift-aaaa1111', {})
    b = _shift(tmp_path, 'Nightshift-shift-bbbb2222', {})
    assert launcher.resolve_shift('bbbb2222') == b and launcher.resolve_shift(a.name) == a
    assert launcher.resolve_shift('latest') in (a, b)
    with pytest.raises(SystemExit):
        launcher.resolve_shift('zzz')


def test_down_writes_handover_kills_crew_and_closes_only_its_window(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    d = _shift(tmp_path, 'Nightshift-shift-dead0001', {'Foreman': 501, 'Coder': 502})
    (d / 'launch.json').write_text(json.dumps({'argv': [], 'hwnd': 777}), encoding='utf-8')
    (d / 'result.json').write_text(json.dumps({'status': 'done', 'summary': 'did the thing',
                                               'changed': ['a.py', 'b.py']}), encoding='utf-8')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: True)
    monkeypatch.setattr(launcher, 'pane_tail', lambda pid, lines=40: [f'last words of {pid}'])
    killed, closed, git_calls = [], [], []

    def run(argv, **kwargs):
        if argv[0] == 'taskkill':
            killed.append(argv[-1])
        if argv[0] == 'git':
            git_calls.append(argv)
            return subprocess.CompletedProcess([], 0, stdout=b'abc1234 First change\ndef5678 Second change\n', stderr=b'')
        if argv[1:3] == ['-m', 'pytest']:
            return subprocess.CompletedProcess([], 1, stdout=b'..F\n1 failed, 2 passed in 0.5s\n', stderr=b'')
        return subprocess.CompletedProcess([], 0, stdout=b'', stderr=b'')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {777: 'Foreman', 888: 'other'})
    monkeypatch.setattr(launcher.ctypes.windll.user32, 'PostMessageW', lambda h, m, w, l: closed.append((h.value, m)))
    launcher.down(['dead0001'])
    out = json.loads(capsys.readouterr().out)
    handover = (d / 'handover.md').read_text(encoding='utf-8')
    assert 'last words of 501' in handover and 'last words of 502' in handover and 'Ticket X: do it' in handover
    assert sorted(killed) == ['1501', '1502', '501', '502']          # both claude pids and both wrappers
    assert closed == [(777, 0x10)] and out['window_closed'] is True   # WM_CLOSE to that hwnd only
    assert 'Status: done' in handover and 'Summary: did the thing' in handover and '- a.py' in handover
    assert 'abc1234 First change' in handover and 'def5678 Second change' in handover
    assert 'test_nightshift_shifts.py: failed — 1 failed, 2 passed in 0.5s' in handover
    assert git_calls and git_calls[0][:3] == ['git', '-C', 'C:/x'] and git_calls[0][4].startswith('--since=')
    assert handover.index('## Result') < handover.index('## Commits') < handover.index('## Foreman')


def test_down_handover_without_result_or_launch_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    d = _shift(tmp_path, 'Nightshift-shift-bare0001', {'Foreman': 601})
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    monkeypatch.setattr(launcher, 'suite_result', lambda: ('passed', '5 passed'))
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([(['taskkill'], subprocess.CompletedProcess([], 0))]))
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {})
    launcher.down(['bare0001'])
    handover = (d / 'handover.md').read_text(encoding='utf-8')
    assert '(no result.json)' in handover and '(no launch.json)' in handover and ': passed — 5 passed' in handover


@pytest.mark.parametrize('ticket, windows, expected', [
    ('NGH-023', {1: 'NGH-023 Foreman', 2: 'NGH-099 Foreman', 3: 'other'}, 1),        # this shift's ticket title
    (None, {4: 'Nightshift-shift-titl0001 Coder', 5: 'NGH-023 Coder'}, 4),           # free-text: the shift name
    (None, {6: 'Foreman', 7: 'other'}, 6),                                            # legacy bare title
    ('NGH-023', {8: 'NGH-023 Foreman', 9: 'Coder'}, None),                            # two matches: close nothing
])
def test_down_without_hwnd_falls_back_to_this_shifts_titles(monkeypatch, tmp_path, capsys, ticket, windows, expected):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    d = _shift(tmp_path, 'Nightshift-shift-titl0001', {'Foreman': 601})
    if ticket:
        (d / 'ticket.txt').write_text(ticket, encoding='utf-8')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    monkeypatch.setattr(launcher, 'suite_result', lambda: ('passed', '5 passed'))
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([(['taskkill'], subprocess.CompletedProcess([], 0))]))
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: windows)
    closed = []
    monkeypatch.setattr(launcher.ctypes.windll.user32, 'PostMessageW', lambda h, m, w, l: closed.append(h.value))
    launcher.down(['titl0001'])
    out = json.loads(capsys.readouterr().out)
    assert closed == ([expected] if expected else []) and out['window_closed'] is bool(expected)


def test_status_all_shows_result_status(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    a = _shift(tmp_path, 'Nightshift-shift-res00001', {'Foreman': 11})
    (a / 'result.json').write_text(json.dumps({'status': 'blocked'}), encoding='utf-8')
    _shift(tmp_path, 'Nightshift-shift-res00002', {'Foreman': 12})
    bad = _shift(tmp_path, 'Nightshift-shift-res00003', {'Foreman': 13})
    (bad / 'result.json').write_text('{not json', encoding='utf-8')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    launcher.status(['--all'])
    results = {r['shift']: r['result'] for r in json.loads(capsys.readouterr().out)}
    assert results == {a.name: 'blocked', 'Nightshift-shift-res00002': 'none', bad.name: 'invalid'}


def test_shift_commits_and_suite_result_failure_paths(monkeypatch):
    seen = []
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: seen.append(argv) or subprocess.CompletedProcess([], 128, stdout=b'fatal: not a repo', stderr=b''))
    assert launcher.shift_commits('C:/x', 0) == []
    assert launcher.suite_result()[0] == 'failed'
    assert seen[-1][0] == 'C:/miniconda3/envs/python312/python.exe'   # never sys.executable

    def boom(argv, **k):
        raise subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(launcher.subprocess, 'run', boom)
    assert launcher.shift_commits('C:/x', 0) == []
    assert launcher.suite_result()[0].startswith('not run: ')


# ── loop: one shift per ticket, torn down after each ─────────────────────────

def test_foreman_brief_carries_shift_contract_with_state_dir():
    brief = launcher.role_brief('Foreman', 'f', 'c', Path('C:/x/shift-1'))
    assert 'C:\\x\\shift-1\\result.json' in brief.replace('/', '\\') and '"done" or "blocked"' in brief
    assert 'test_nightshift_shifts.py' in brief


def test_wait_for_result_returns_result_json(tmp_path, monkeypatch):
    (tmp_path / 'result.json').write_text(json.dumps({'status': 'done', 'summary': 'ok'}), encoding='utf-8')
    assert launcher.wait_for_result(tmp_path, 20, sleep=lambda s: None) == {'status': 'done', 'summary': 'ok'}


def test_wait_for_result_reports_death_and_stall(tmp_path, monkeypatch):
    d = _shift(tmp_path, 'Nightshift-shift-w1', {'Foreman': 1, 'Coder': 2})
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    assert launcher.wait_for_result(d, 20, sleep=lambda s: None)['status'] == 'died'
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: True)
    monkeypatch.setattr(launcher, 'pane_tail', lambda pid, lines=40: [])
    monkeypatch.setattr(launcher, 'session_idle_since', lambda pid: 21 * 60)
    assert launcher.wait_for_result(d, 20, sleep=lambda s: None)['status'] == 'stalled'
    # busy session (None) keeps waiting: the sleep hook ends the test by raising
    monkeypatch.setattr(launcher, 'session_idle_since', lambda pid: None)
    with pytest.raises(RuntimeError, match='waited'):
        launcher.wait_for_result(d, 20, sleep=lambda s: (_ for _ in ()).throw(RuntimeError('waited')))


def test_record_denials_dedups_across_polls_and_keeps_first_time(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.time, 'strftime', lambda fmt, *a: '2026-10-15 01:00:00')
    first = launcher.record_denials(tmp_path, {'Coder': ['  Bash denied by auto mode classifier  ', 'ok line']})
    assert first == [{'time': '2026-10-15 01:00:00', 'role': 'Coder', 'line': 'Bash denied by auto mode classifier'}]
    monkeypatch.setattr(launcher.time, 'strftime', lambda fmt, *a: '2026-10-15 02:00:00')
    second = launcher.record_denials(tmp_path, {'Coder': ['Bash denied by auto mode classifier', 'Permission denied: x']})
    assert [(d['time'], d['line']) for d in second] == [('2026-10-15 01:00:00', 'Bash denied by auto mode classifier'),
                                                        ('2026-10-15 02:00:00', 'Permission denied: x')]
    assert launcher.load_denials(tmp_path) == second
    assert launcher.record_denials(tmp_path / 'missing', {'Coder': ['fine']}) == []
    assert not (tmp_path / 'missing').exists()                      # nothing new, nothing written


def test_wait_for_result_records_denials_from_both_panes_while_waiting(tmp_path, monkeypatch):
    d = _shift(tmp_path, 'Nightshift-shift-w2', {'Foreman': 1, 'Coder': 2})
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: True)
    monkeypatch.setattr(launcher, 'session_idle_since', lambda pid: None)
    screens = {1: ['Foreman working', 'Write denied by auto mode classifier'], 2: ['Permission denied (publickey)']}
    monkeypatch.setattr(launcher, 'pane_tail', lambda pid, lines=40: screens[pid])
    polls = []

    def sleep(s):
        polls.append(s)
        if len(polls) == 2:
            raise RuntimeError('waited')
    with pytest.raises(RuntimeError, match='waited'):
        launcher.wait_for_result(d, 20, sleep=sleep)
    denials = json.loads((d / 'denials.json').read_text(encoding='utf-8'))
    assert [(x['role'], x['line']) for x in denials] == [('Foreman', 'Write denied by auto mode classifier'),
                                                         ('Coder', 'Permission denied (publickey)')]


def test_handover_lists_denials_or_none(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'suite_result', lambda: ('passed', '1 passed'))
    record = {'roles': {}}
    assert '(none)' in launcher.handover_report(tmp_path, record)
    (tmp_path / 'denials.json').write_text(json.dumps([{'time': 't1', 'role': 'Coder', 'line': 'Bash denied'}]),
                                           encoding='utf-8')
    report = launcher.handover_report(tmp_path, record)
    assert '- t1 Coder: Bash denied' in report
    assert report.index('## Result') < report.index('## Auto-mode denials') < report.index('## Commits since shift start')


@pytest.mark.parametrize('status, expect_denials', [('blocked', True), ('done', False)])
def test_loop_note_carries_denials_only_when_blocked(monkeypatch, tmp_path, status, expect_denials):
    d = tmp_path / 'shift-NGH-030'
    d.mkdir()
    (d / 'denials.json').write_text(json.dumps([{'time': 't1', 'role': 'Coder', 'line': 'Bash denied'}]),
                                    encoding='utf-8')
    notes = []
    monkeypatch.setattr(launcher, 'start', lambda argv: d)
    monkeypatch.setattr(launcher, 'wait_for_result',
                        lambda state_dir, stall, **kw: {'status': status, 'stage': 'tested', 'summary': 's'})
    monkeypatch.setattr(launcher, 'down', lambda argv: None)
    monkeypatch.setattr(launcher, 'close_terminal_windows', lambda prefix: pytest.fail('must not touch real windows'))
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: notes.append(argv[-1]) or subprocess.CompletedProcess([], 0, b'', b''))
    try:
        launcher.loop(['NGH-030'])
    except SystemExit:
        pass
    assert len(notes) == 1
    assert ('Denials: Coder: Bash denied' in notes[0]) is expect_denials


def test_loop_reruns_a_died_or_stalled_shift_with_the_handover(monkeypatch, tmp_path, capsys):
    outcomes = iter([{'status': 'died', 'summary': 'crash'}, {'status': 'done', 'stage': 'tested', 'summary': 'ok'}])
    starts = []

    def fake_start(argv):
        d = tmp_path / f'shift-{len(starts)}'
        d.mkdir()
        (d / 'handover.md').write_text('previous work', encoding='utf-8')
        starts.append(argv)
        return d
    monkeypatch.setattr(launcher, 'start', fake_start)
    monkeypatch.setattr(launcher, 'wait_for_result', lambda state_dir, stall, **kw: next(outcomes))
    monkeypatch.setattr(launcher, 'down', lambda argv: None)
    monkeypatch.setattr(launcher, 'close_terminal_windows', lambda prefix: pytest.fail('must not touch real windows'))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess([], 0, b'', b''))
    launcher.loop(['NGH-020'])
    assert starts[0] == ['NGH-020']
    assert starts[1][:3] == ['NGH-020', '--continue-from', str(tmp_path / 'shift-0')]   # rerun carries the handover
    events = [json.loads(l)['event'] for l in capsys.readouterr().out.splitlines()]
    assert events == ['started', 'ended', 'retrying', 'started', 'ended', 'finished']


def test_loop_gives_up_after_retries(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'start', lambda argv: (tmp_path / f'shift-{len(argv)}-{id(argv)}').resolve())
    monkeypatch.setattr(launcher, 'wait_for_result', lambda state_dir, stall, **kw: {'status': 'stalled', 'summary': ''})
    monkeypatch.setattr(launcher, 'down', lambda argv: None)
    monkeypatch.setattr(launcher, 'close_terminal_windows', lambda prefix: pytest.fail('must not touch real windows'))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess([], 0, b'', b''))
    with pytest.raises(SystemExit):
        launcher.loop(['NGH-021', '--retries', '1'])
    events = [json.loads(l)['event'] for l in capsys.readouterr().out.splitlines()]
    assert events.count('started') == 2 and events[-1] == 'stopped'


def test_start_continue_from_appends_previous_handover(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    monkeypatch.chdir(tmp_path)
    prev = tmp_path / 'old-shift'
    prev.mkdir()
    (prev / 'handover.md').write_text('# Handover\ncommitted abc123', encoding='utf-8')
    launcher.start(['--dry-run', '--continue-from', str(prev), 'do the thing'])
    plan = json.loads(capsys.readouterr().out)
    assert plan['prompt'].startswith('do the thing') and 'committed abc123' in plan['prompt']


def test_loop_runs_tickets_in_order_tearing_down_each_and_stops_when_blocked(monkeypatch, tmp_path, capsys):
    events = []
    results = {'NGH-010': {'status': 'done', 'stage': 'tested', 'summary': 'a'},
               'NGH-011': {'status': 'blocked', 'summary': 'b'},
               'NGH-012': {'status': 'done', 'stage': 'tested', 'summary': 'c'}}

    def fake_start(argv):
        d = tmp_path / ('shift-' + argv[0])
        d.mkdir()
        events.append(('start', argv[0]))
        return d

    def fake_wait(state_dir, stall_minutes, **kw):
        return results[state_dir.name.split('-', 1)[1]]
    monkeypatch.setattr(launcher, 'start', fake_start)
    monkeypatch.setattr(launcher, 'wait_for_result', fake_wait)
    monkeypatch.setattr(launcher, 'down', lambda argv: events.append(('down', argv[0])))
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: events.append(('note', argv[2])) or subprocess.CompletedProcess([], 0, b'', b''))
    with pytest.raises(SystemExit):
        launcher.loop(['NGH-010', 'NGH-011', 'NGH-012'])
    assert events == [('start', 'NGH-010'), ('down', 'shift-NGH-010'), ('note', 'NGH-010'),
                      ('start', 'NGH-011'), ('down', 'shift-NGH-011'), ('note', 'NGH-011')]   # NGH-012 never started
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert lines[-1] == {'event': 'stopped', 'reason': 'blocked', 'ticket': 'NGH-011'}


def _search_result(tasks):
    return subprocess.CompletedProcess(['tasks-cli'], 0, stdout=json.dumps({'results': tasks}).encode(), stderr=b'')


def test_project_tickets_sorts_by_number_not_search_order(monkeypatch):
    run = _fake_run([(['tasks-cli', 'search'], _search_result([
        {'task_id': 'NGH-009', 'number': 9, 'tags': []},
        {'task_id': 'NGH-005', 'number': 5, 'tags': []},
        {'task_id': 'NGH-007', 'number': 7, 'tags': None}]))])
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    assert launcher.project_tickets('NGH') == ['NGH-005', 'NGH-007', 'NGH-009']
    assert run.calls == [['tasks-cli', 'search', '--prefix', 'NGH', '--status', 'backlog', '--all']]


def test_project_tickets_skips_needs_human(monkeypatch):
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([(['tasks-cli', 'search'], _search_result([
        {'task_id': 'NGH-005', 'number': 5, 'tags': ['needs-human']},
        {'task_id': 'NGH-006', 'number': 6, 'tags': ['nightshift']}]))]))
    assert launcher.project_tickets('NGH') == ['NGH-006']


def test_loop_project_moves_each_started_ticket_to_in_progress_and_honours_max(monkeypatch, tmp_path, capsys):
    events = []

    def fake_start(argv):
        d = tmp_path / ('shift-' + argv[0])
        d.mkdir()
        events.append(('start', argv[0]))
        return d

    def fake_run(argv, **k):
        if argv[:2] == ['tasks-cli', 'search']:
            return _search_result([{'task_id': f'NGH-00{n}', 'number': n, 'tags': []} for n in (7, 5, 6)])
        events.append(tuple(argv) if argv[1] == 'move' else (argv[1], argv[2]))
        return subprocess.CompletedProcess([], 0, b'', b'')
    monkeypatch.setattr(launcher, 'start', fake_start)
    monkeypatch.setattr(launcher, 'wait_for_result', lambda d, m, **kw: {'status': 'done', 'stage': 'tested', 'summary': 'ok'})
    monkeypatch.setattr(launcher, 'down', lambda argv: events.append(('down', argv[0])))
    monkeypatch.setattr(launcher.subprocess, 'run', fake_run)
    launcher.loop(['--project', 'NGH', '--max', '2'])
    move = lambda t: ('tasks-cli', 'move', t, 'in-progress')  # noqa: E731
    assert events == [('start', 'NGH-005'), move('NGH-005'), ('down', 'shift-NGH-005'), ('note', 'NGH-005'),
                      ('start', 'NGH-006'), move('NGH-006'), ('down', 'shift-NGH-006'), ('note', 'NGH-006')]
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert lines[0] == {'event': 'selected', 'project': 'NGH', 'tickets': ['NGH-005', 'NGH-006']}


@pytest.mark.parametrize('argv', [['NGH-005', '--project', 'NGH'], []])
def test_loop_requires_exactly_one_of_tickets_or_project(argv):
    with pytest.raises(SystemExit):
        launcher.loop(argv)


# ── working-folder lock ───────────────────────────────────────────────────────

def _no_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    monkeypatch.setattr(launcher, 'install_profile', lambda state_dir: None)
    monkeypatch.setattr(launcher, 'launch', lambda *a: None)
    work = tmp_path / 'work'
    work.mkdir()
    return str(work.resolve())


def _lock(cwd, shift):
    path = launcher.lock_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'shift': shift, 'cwd': cwd}), encoding='utf-8')
    return path


def test_lock_path_is_lower_case_with_separators_replaced():
    assert launcher.lock_path('C:/Scripts') == launcher.LOCKS_DIR / 'c__scripts.json'
    assert launcher.lock_path('C:\\Git\\Night Shift').name == 'c__git_night shift.json'


def test_start_takes_the_folder_lock(monkeypatch, tmp_path):
    cwd = _no_launch(monkeypatch, tmp_path)
    state_dir = launcher.start(['--cwd', cwd, 'do it'])
    lock = json.loads(launcher.lock_path(cwd).read_text(encoding='utf-8'))
    assert lock['shift'] == state_dir.name and lock['cwd'] == cwd and lock['state_dir'] == str(state_dir)
    assert lock['launcher_pid'] == launcher.os.getpid() and lock['started']


def test_start_refuses_a_folder_held_by_a_live_shift(monkeypatch, tmp_path, capsys):
    cwd = _no_launch(monkeypatch, tmp_path)
    _shift(tmp_path, 'Nightshift-shift-live0001', {'Foreman': 1})
    path = _lock(cwd, 'Nightshift-shift-live0001')
    before = path.read_text(encoding='utf-8')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: True)
    with pytest.raises(SystemExit) as exc:
        launcher.start(['--cwd', cwd, 'do it'])
    assert exc.value.code == 3 and isinstance(exc.value, launcher.FolderLocked)
    err = capsys.readouterr().err
    assert 'Nightshift-shift-live0001' in err and cwd in err and 'nightshift down live0001' in err
    assert [d.name for d in (tmp_path / 'shifts').iterdir()] == ['Nightshift-shift-live0001']   # no new shift
    assert path.read_text(encoding='utf-8') == before


@pytest.mark.parametrize('holder_dir', [True, False])
def test_start_replaces_a_stale_lock(monkeypatch, tmp_path, holder_dir):
    cwd = _no_launch(monkeypatch, tmp_path)
    if holder_dir:
        _shift(tmp_path, 'Nightshift-shift-dead0002', {'Foreman': 1})   # crew gone
    _lock(cwd, 'Nightshift-shift-dead0002')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    state_dir = launcher.start(['--cwd', cwd, 'do it'])
    assert json.loads(launcher.lock_path(cwd).read_text(encoding='utf-8'))['shift'] == state_dir.name


def test_down_releases_only_its_own_lock(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    _shift(tmp_path, 'Nightshift-shift-mine0001', {'Foreman': 601})
    mine, other = _lock('C:/mine', 'Nightshift-shift-mine0001'), _lock('C:/other', 'Nightshift-shift-othr0001')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    monkeypatch.setattr(launcher, 'suite_result', lambda: ('passed', '1 passed'))
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([(['taskkill'], subprocess.CompletedProcess([], 0))]))
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {})
    launcher.down(['mine0001'])
    out = json.loads(capsys.readouterr().out)
    assert out['lock_released'] == ['C:/mine']
    assert not mine.exists() and other.exists()


def test_status_shows_the_folder_each_shift_holds(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    _shift(tmp_path, 'Nightshift-shift-hold0001', {'Foreman': 11})
    _shift(tmp_path, 'Nightshift-shift-free0001', {'Foreman': 12})
    _lock('C:/held', 'Nightshift-shift-hold0001')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    launcher.status(['--all'])
    holds = {r['shift']: r['holds'] for r in json.loads(capsys.readouterr().out)}
    assert holds == {'Nightshift-shift-hold0001': 'C:/held', 'Nightshift-shift-free0001': None}


def test_loop_stops_without_retry_when_the_folder_is_locked(monkeypatch, capsys):
    starts, notes, downs = [], [], []

    def fake_start(argv):
        starts.append(argv)
        raise launcher.FolderLocked(3, 'folder C:/x is held by live shift Nightshift-shift-busy0001')
    monkeypatch.setattr(launcher, 'start', fake_start)
    monkeypatch.setattr(launcher, 'down', lambda argv: downs.append(argv))
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: notes.append(argv) or subprocess.CompletedProcess([], 0, b'', b''))
    with pytest.raises(SystemExit) as exc:
        launcher.loop(['NGH-040', 'NGH-041'])
    assert exc.value.code == 2 and len(starts) == 1 and downs == []
    assert len(notes) == 1 and notes[0][:5] == ['tasks-cli', 'note', 'NGH-040', '--heading', 'Nightshift loop']
    assert 'held by live shift Nightshift-shift-busy0001' in notes[0][-1]
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert lines[0]['event'] == 'ended' and lines[0]['status'] == 'blocked' and lines[0]['attempt'] == 1
    assert lines[-1] == {'event': 'stopped', 'reason': 'blocked', 'ticket': 'NGH-040'}


def test_loop_lets_other_start_exits_through(monkeypatch):
    monkeypatch.setattr(launcher, 'start', lambda argv: (_ for _ in ()).throw(SystemExit('tasks-cli get failed')))
    with pytest.raises(SystemExit, match='tasks-cli get failed'):
        launcher.loop(['NGH-042'])


# ── owner notification (Mailkeep is always faked; nothing is ever sent) ───────

def _ok(payload):
    return subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b'')


def _fake_mail(monkeypatch, prepare=None, events=None):
    """Fake subprocess.run: mailkeep calls are recorded (with the body file's text), tasks-cli calls succeed."""
    mail = []

    def run(argv, **kwargs):
        if argv[0] != launcher.MAILKEEP:
            if events is not None:
                events.append(('tasks-cli', argv[1]))
            return subprocess.CompletedProcess([], 0, b'', b'')
        assert kwargs.get('capture_output') is True and kwargs.get('timeout') == 120
        call = {'argv': list(argv)}
        if argv[1] == 'prepare':
            call['body'] = Path(argv[argv.index('--body-file') + 1]).read_text(encoding='utf-8')
        mail.append(call)
        if events is not None:
            events.append(('mailkeep', argv[1]))
        if argv[1] == 'prepare':
            return prepare if prepare is not None else _ok({'id': 'd42'})
        if argv[1] == 'queue':
            return _ok({'ok': True})
        return _ok({'results': [{'id': argv[2], 'ok': True}]})
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher, 'notify_owner', _real_notify)
    return mail


def test_notify_owner_prepares_queues_and_sends_the_draft(monkeypatch):
    mail = _fake_mail(monkeypatch)
    out = launcher.notify_owner('Nightshift: blocked NGH-050', 'the body')
    mk = launcher.MAILKEEP
    assert [c['argv'] for c in mail] == [
        [mk, 'prepare', '--identity', 'owner_id', '--to', 'owner@example.com', '--subject', 'Nightshift: blocked NGH-050',
         '--body-file', mail[0]['argv'][9], '--execute'],
        [mk, 'queue', 'd42', '--execute'],
        [mk, 'send', 'd42', '--execute']]
    assert mail[0]['body'] == 'the body'
    assert out['sent'] is True and out['draft_id'] == 'd42' and out['error'] is None


@pytest.mark.parametrize('prepare', [_ok({'ok': True}), subprocess.CompletedProcess([], 1, b'', b'boom'),
                                     subprocess.CompletedProcess([], 0, b'not json', b'')])
def test_notify_owner_stops_at_a_failed_prepare_without_raising(monkeypatch, prepare):
    mail = _fake_mail(monkeypatch, prepare=prepare)
    out = launcher.notify_owner('s', 'b')
    assert [c['argv'][1] for c in mail] == ['prepare']
    assert out['sent'] is False and out['error']


def test_notify_owner_needs_send_to_confirm_the_draft(monkeypatch):
    _fake_mail(monkeypatch)
    real_run = launcher.subprocess.run
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: _ok({'results': [{'id': 'other', 'ok': True}]})
                        if argv[1] == 'send' else real_run(argv, **k))
    assert launcher.notify_owner('s', 'b')['sent'] is False


def _loop_shift(monkeypatch, tmp_path, outcomes):
    """Fake start/wait/down: each start makes shift-<n>; outcomes is an iterator of results."""
    count = []

    def fake_start(argv):
        d = tmp_path / f'shift-{len(count)}'
        d.mkdir()
        count.append(argv)
        return d
    monkeypatch.setattr(launcher, 'start', fake_start)
    monkeypatch.setattr(launcher, 'wait_for_result', lambda state_dir, stall, **kw: next(outcomes))
    monkeypatch.setattr(launcher, 'down', lambda argv: None)
    monkeypatch.setattr(launcher, 'close_terminal_windows', lambda prefix: None)


def test_loop_emails_once_when_blocked(monkeypatch, tmp_path):
    _loop_shift(monkeypatch, tmp_path, iter([{'status': 'blocked', 'summary': 'needs a key'}]))
    (tmp_path / 'shift-0').mkdir()
    (tmp_path / 'shift-0' / 'denials.json').write_text(
        json.dumps([{'time': 't', 'role': 'Coder', 'line': 'Bash denied'}]), encoding='utf-8')
    monkeypatch.setattr(launcher, 'start', lambda argv: tmp_path / 'shift-0')
    mail = _fake_mail(monkeypatch)
    with pytest.raises(SystemExit):
        launcher.loop(['NGH-051'])
    assert [c['argv'][1] for c in mail] == ['prepare', 'queue', 'send']
    argv = mail[0]['argv']
    assert argv[argv.index('--subject') + 1] == 'Nightshift: blocked NGH-051'
    body = mail[0]['body']
    assert 'needs a key' in body and str(tmp_path / 'shift-0' / 'handover.md') in body and 'Coder: Bash denied' in body


def test_loop_emails_once_after_retries_are_exhausted(monkeypatch, tmp_path):
    _loop_shift(monkeypatch, tmp_path, iter([{'status': 'stalled', 'summary': 'idle'}] * 3))
    mail = _fake_mail(monkeypatch)
    with pytest.raises(SystemExit):
        launcher.loop(['NGH-052', '--retries', '2'])
    assert [c['argv'][1] for c in mail] == ['prepare', 'queue', 'send']
    argv = mail[0]['argv']
    assert argv[argv.index('--subject') + 1] == 'Nightshift: stalled NGH-052'
    assert str(tmp_path / 'shift-2' / 'handover.md') in mail[0]['body'] and '(none)' in mail[0]['body']


def test_loop_emails_once_at_the_end_of_a_done_batch(monkeypatch, tmp_path):
    _loop_shift(monkeypatch, tmp_path, iter([{'status': 'done', 'stage': 'tested', 'summary': 'first'},
                                             {'status': 'done', 'stage': 'tested', 'summary': 'second'}]))
    events = []
    mail = _fake_mail(monkeypatch, events=events)
    launcher.loop(['NGH-053', 'NGH-054'])
    assert [e for e in events if e[0] == 'mailkeep'] == [('mailkeep', 'prepare'), ('mailkeep', 'queue'), ('mailkeep', 'send')]
    assert events[-3:] == [('mailkeep', 'prepare'), ('mailkeep', 'queue'), ('mailkeep', 'send')]   # none between tickets
    argv = mail[0]['argv']
    assert argv[argv.index('--subject') + 1] == 'Nightshift: finished NGH-054'
    body = mail[0]['body']
    for ticket, summary, shift in (('NGH-053', 'first', 'shift-0'), ('NGH-054', 'second', 'shift-1')):
        assert ticket in body and f'done: {summary}' in body and str(tmp_path / shift / 'handover.md') in body


def test_loop_emails_when_the_folder_is_locked(monkeypatch):
    monkeypatch.setattr(launcher, 'start',
                        lambda argv: (_ for _ in ()).throw(launcher.FolderLocked(3, 'folder C:/x is held')))
    mail = _fake_mail(monkeypatch)
    with pytest.raises(SystemExit):
        launcher.loop(['NGH-055'])
    argv = mail[0]['argv']
    assert argv[argv.index('--subject') + 1] == 'Nightshift: blocked NGH-055'
    assert 'no shift started' in mail[0]['body'] and 'folder C:/x is held' in mail[0]['body']


@pytest.mark.parametrize('status', ['blocked', 'done'])
def test_loop_no_notify_sends_nothing(monkeypatch, tmp_path, status):
    _loop_shift(monkeypatch, tmp_path, iter([{'status': status, 'stage': 'tested', 'summary': 's'}]))
    mail = _fake_mail(monkeypatch)
    try:
        launcher.loop(['NGH-056', '--no-notify'])
    except SystemExit:
        pass
    assert mail == []


# ── file claims ───────────────────────────────────────────────────────────────

def _claims_shift(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    return _shift(tmp_path, 'Nightshift-shift-clm00001', {})


def test_normalise_path_folds_case_and_separators_and_resolves_relative(monkeypatch, tmp_path):
    assert launcher.normalise_path('C:/scripts/x.py') == launcher.normalise_path('C:\\Scripts\\x.py')
    monkeypatch.chdir(tmp_path)
    assert launcher.normalise_path('sub/y.py') == launcher.normalise_path(str(tmp_path / 'sub' / 'y.py'))


def test_claim_writes_claims_json(monkeypatch, tmp_path, capsys):
    d = _claims_shift(monkeypatch, tmp_path)
    launcher.claim(['clm00001', 'coder-a', 'C:/scripts/a.py', 'C:\\Scripts\\A.py', 'C:/scripts/b.py'])
    out = json.loads(capsys.readouterr().out)
    a, b = launcher.normalise_path('C:/scripts/a.py'), launcher.normalise_path('C:/scripts/b.py')
    assert out == {'shift': d.name, 'role': 'coder-a', 'claimed': [a, b]}   # duplicates collapse
    claims = json.loads((d / 'claims.json').read_text(encoding='utf-8'))
    assert set(claims) == {a, b} and claims[a]['role'] == 'coder-a' and claims[a]['time']
    assert not (d / 'claims.lock').exists() and not list(d.glob('*.tmp'))


def test_claim_conflict_exits_4_and_writes_nothing(monkeypatch, tmp_path, capsys):
    d = _claims_shift(monkeypatch, tmp_path)
    launcher.claim(['clm00001', 'coder-a', 'C:/scripts/held.py'])
    before = (d / 'claims.json').read_text(encoding='utf-8')
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        launcher.claim(['clm00001', 'coder-b', 'C:/scripts/free.py', 'C:\\SCRIPTS\\held.py'])   # mixed free + held
    assert exc.value.code == 4 and isinstance(exc.value, launcher.FileClaimed)
    captured = capsys.readouterr()
    assert captured.out == ''
    assert f"{launcher.normalise_path('C:/scripts/held.py')} is already claimed by coder-a since " in captured.err
    assert (d / 'claims.json').read_text(encoding='utf-8') == before   # free.py not claimed either
    assert not (d / 'claims.lock').exists()


def test_claim_same_role_reclaims_case_insensitively_and_refreshes_time(monkeypatch, tmp_path, capsys):
    d = _claims_shift(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher.time, 'strftime', lambda fmt, *a: '2026-10-15 01:00:00')
    launcher.claim(['clm00001', 'Coder-A', 'C:/scripts/a.py'])
    monkeypatch.setattr(launcher.time, 'strftime', lambda fmt, *a: '2026-10-15 02:00:00')
    launcher.claim(['clm00001', 'coder-a', 'C:/scripts/a.py'])
    claim = launcher.load_claims(d)[launcher.normalise_path('C:/scripts/a.py')]
    assert claim == {'role': 'coder-a', 'time': '2026-10-15 02:00:00'}


def test_release_specific_paths_or_all_of_role_leaving_others_alone(monkeypatch, tmp_path, capsys):
    d = _claims_shift(monkeypatch, tmp_path)
    launcher.claim(['clm00001', 'coder-a', 'C:/scripts/a1.py', 'C:/scripts/a2.py', 'C:/scripts/a3.py'])
    launcher.claim(['clm00001', 'coder-b', 'C:/scripts/b.py'])
    capsys.readouterr()
    n = launcher.normalise_path
    launcher.release(['clm00001', 'CODER-A', 'C:/scripts/a1.py', 'C:/scripts/b.py'])   # b.py is not coder-a's
    assert json.loads(capsys.readouterr().out) == {'released': [n('C:/scripts/a1.py')]}
    assert set(launcher.load_claims(d)) == {n('C:/scripts/a2.py'), n('C:/scripts/a3.py'), n('C:/scripts/b.py')}
    launcher.release(['clm00001', 'coder-a'])
    assert json.loads(capsys.readouterr().out) == {'released': [n('C:/scripts/a2.py'), n('C:/scripts/a3.py')]}
    remaining = launcher.load_claims(d)
    assert list(remaining) == [n('C:/scripts/b.py')] and remaining[n('C:/scripts/b.py')]['role'] == 'coder-b'


def test_claims_lists_current_claims_or_empty(monkeypatch, tmp_path, capsys):
    _claims_shift(monkeypatch, tmp_path)
    launcher.claims_command(['clm00001'])
    assert json.loads(capsys.readouterr().out) == {}
    launcher.claim(['clm00001', 'coder-a', 'C:/scripts/a.py'])
    capsys.readouterr()
    launcher.claims_command(['latest'])
    listed = json.loads(capsys.readouterr().out)
    assert list(listed) == [launcher.normalise_path('C:/scripts/a.py')] and listed[launcher.normalise_path('C:/scripts/a.py')]['role'] == 'coder-a'


def test_claims_lock_waits_then_gives_up_but_clears_a_stale_lock(tmp_path, monkeypatch):
    lock = tmp_path / 'claims.lock'
    lock.write_text('', encoding='utf-8')
    monkeypatch.setattr(launcher.time, 'sleep', lambda s: None)
    with pytest.raises(SystemExit, match='still held'):
        with launcher.claims_locked(tmp_path, wait=0.05):
            pass
    assert lock.exists()                                             # someone else's live lock is not ours to remove
    old = launcher.time.time() - 60
    launcher.os.utime(lock, (old, old))
    with launcher.claims_locked(tmp_path):
        assert lock.exists()
    assert not lock.exists()


def test_handover_lists_outstanding_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'suite_result', lambda: ('passed', '1 passed'))
    monkeypatch.setattr(launcher, 'shift_commits', lambda cwd, since: [])
    record = {'roles': {}}
    report = launcher.handover_report(tmp_path, record)
    assert report[report.index('## Outstanding claims') + 2] == '(none)'
    launcher.save_claims(tmp_path, {'C:\\scripts\\a.py': {'role': 'coder-a', 'time': '2026-10-15 01:00:00'}})
    report = launcher.handover_report(tmp_path, record)
    assert '- C:\\scripts\\a.py — coder-a (since 2026-10-15 01:00:00)' in report
    assert report.index('## Auto-mode denials') < report.index('## Outstanding claims') < report.index('## Commits since shift start')
    assert (tmp_path / 'claims.json').exists()                       # down does not release claims


def _nightshift_cmd():
    """The command role_brief puts in place of {nightshift}: the suite Python and this repo's launcher."""
    return f'{launcher.SUITE_PYTHON} {launcher.REPO_DIR / "nightshift_shifts.py"}'


def test_role_brief_fills_shift_and_tells_crew_about_claims():
    coder = launcher.role_brief('Coder', 'S-coder', 'S-foreman', Path('C:/x/Nightshift-shift-abc'))
    foreman = launcher.role_brief('Foreman', 'S-foreman', 'S-coder', Path('C:/x/Nightshift-shift-abc'))
    ns = _nightshift_cmd()
    assert f'{ns} claim Nightshift-shift-abc S-coder' in coder
    assert f'{ns} release Nightshift-shift-abc S-coder' in coder
    assert f'{ns} claims Nightshift-shift-abc' in foreman and foreman.rstrip().endswith('verbatim:')
    assert '{nightshift}' not in coder and '{nightshift}' not in foreman
    assert '{shift}' not in launcher.role_brief('Coder', 'c', 'f')   # no state dir: filled with ''


# ── Windows Terminal profile ──────────────────────────────────────────────────

def test_install_profile_sets_crew_profile_and_keeps_backup(monkeypatch, tmp_path):
    settings = tmp_path / 'Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json'
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({'profiles': {'defaults': {}, 'list': [
        {'guid': '{41cc0613-92f0-4c15-967d-9490faec2f94}', 'name': 'old', 'colorScheme': 'One Half Light',
         'font': {'face': 'Cascadia Mono', 'size': 12}}]}}), encoding='utf-8')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    state = tmp_path / 'state'
    state.mkdir()
    launcher.install_profile(state)
    profile = json.loads(settings.read_text(encoding='utf-8'))['profiles']['list'][0]
    assert profile['name'] == launcher.PROFILE and profile['commandline'] == 'cmd.exe'
    assert profile['font'] == {'face': 'Cascadia Mono', 'size': 12}
    assert profile['antialiasingMode'] == 'grayscale' and profile['colorScheme'] == 'Campbell'
    assert (state / 'terminal-settings-before.json').exists()


# ── resume ────────────────────────────────────────────────────────────────────

def _ticket_shift(tmp_path, name, ticket, pid, mtime, result=None, handover=False):
    d = _shift(tmp_path, name, {'Foreman': pid, 'Coder': pid + 1})
    (d / 'ticket.txt').write_text(ticket + '\n', encoding='utf-8')
    launcher.os.utime(d / 'ticket.txt', (mtime, mtime))
    if result:
        (d / 'result.json').write_text(json.dumps({'status': result, 'stage': 'tested'}), encoding='utf-8')
    if handover:
        (d / 'handover.md').write_text('# Handover', encoding='utf-8')
    return d


def _resume_shifts(monkeypatch, tmp_path, live=()):
    """NGH-002 done; NGH-010 interrupted twice (newer shift is the one to continue); NGH-003 running; OP-001 died."""
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: pid in live)
    _ticket_shift(tmp_path, 'Nightshift-shift-done0001', 'NGH-002', 100, 1000, result='done', handover=True)
    _ticket_shift(tmp_path, 'Nightshift-shift-old00010', 'NGH-010', 200, 1000, result='stalled', handover=True)
    newer = _ticket_shift(tmp_path, 'Nightshift-shift-new00010', 'NGH-010', 300, 2000)
    _ticket_shift(tmp_path, 'Nightshift-shift-live0003', 'NGH-003', 400, 1000)
    op = _ticket_shift(tmp_path, 'Nightshift-shift-op000001', 'OP-001', 500, 1000, result='died', handover=True)
    _shift(tmp_path, 'Nightshift-shift-noticket', {'Foreman': 600})   # free-text shift: never resumed
    return newer, op


def test_resume_plan_skips_done_and_running_and_continues_from_the_latest_shift(monkeypatch, tmp_path):
    newer, op = _resume_shifts(monkeypatch, tmp_path, live=(400,))
    plan = launcher.resume_plan()
    assert [r['ticket'] for r in plan['resume']] == ['NGH-010', 'OP-001']
    ngh = plan['resume'][0]
    assert ngh['continue_from'] == str(newer) and ngh['last_shift'] == newer.name
    assert ngh['last_result'] == 'none' and ngh['reason'] == 'no handover'
    assert plan['resume'][1]['continue_from'] == str(op)
    assert plan['resume'][1]['reason'] == 'handover without done (result died)'
    assert {s['ticket']: s['reason'] for s in plan['skipped']} == {'NGH-002': 'done', 'NGH-003': 'running'}


def test_resume_plan_project_filter(monkeypatch, tmp_path):
    _resume_shifts(monkeypatch, tmp_path)
    plan = launcher.resume_plan('OP')
    assert [r['ticket'] for r in plan['resume']] == ['OP-001'] and plan['skipped'] == []


def test_resume_plan_sorts_ticket_numbers_numerically(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    for i, ticket in enumerate(['NGH-010', 'NGH-002', 'ABC-100']):
        _ticket_shift(tmp_path, f'Nightshift-shift-sort000{i}', ticket, 10 * i + 10, 1000)
    assert [r['ticket'] for r in launcher.resume_plan()['resume']] == ['ABC-100', 'NGH-002', 'NGH-010']


def test_stale_locks_lists_dead_and_missing_shifts_but_not_live_ones(monkeypatch, tmp_path):
    _resume_shifts(monkeypatch, tmp_path, live=(400,))
    live = _lock('C:/live', 'Nightshift-shift-live0003')
    dead = _lock('C:/dead', 'Nightshift-shift-op000001')
    gone = _lock('C:/gone', 'Nightshift-shift-vanished')
    assert sorted(p for p, _ in launcher.stale_locks()) == sorted([dead, gone])
    assert live.exists()


def _resume_fakes(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, 'start', lambda argv: pytest.fail('resume must go through loop'))
    monkeypatch.setattr(launcher, 'loop', lambda argv, continue_from=None: calls.append((argv, continue_from)))
    return calls


def test_resume_dry_run_prints_the_plan_and_changes_nothing(monkeypatch, tmp_path, capsys):
    _resume_shifts(monkeypatch, tmp_path, live=(400,))
    calls = _resume_fakes(monkeypatch)
    dead = _lock('C:/dead', 'Nightshift-shift-op000001')
    launcher.resume(['--dry-run'])
    out = json.loads(capsys.readouterr().out)
    assert out['dry_run'] is True and [r['ticket'] for r in out['resume']] == ['NGH-010', 'OP-001']
    assert out['stale_locks'] == [{'lock': str(dead), 'shift': 'Nightshift-shift-op000001', 'cwd': 'C:/dead'}]
    assert calls == [] and dead.exists()


def test_resume_clears_stale_locks_then_loops_in_ticket_order_with_continue_from(monkeypatch, tmp_path, capsys):
    newer, op = _resume_shifts(monkeypatch, tmp_path, live=(400,))
    calls = _resume_fakes(monkeypatch)
    live = _lock('C:/live', 'Nightshift-shift-live0003')
    dead = _lock('C:/dead', 'Nightshift-shift-op000001')
    launcher.resume(['--stall-minutes', '5', '--no-notify'])
    assert json.loads(capsys.readouterr().out)['dry_run'] is False
    assert not dead.exists() and live.exists()
    assert calls == [(['NGH-010', 'OP-001', '--stall-minutes', '5', '--no-notify'],
                      {'NGH-010': str(newer), 'OP-001': str(op)})]


def test_resume_with_nothing_to_rerun_does_not_loop(monkeypatch, tmp_path, capsys):
    _resume_shifts(monkeypatch, tmp_path, live=(400,))
    calls = _resume_fakes(monkeypatch)
    launcher.resume(['--project', 'NGH-none'])
    assert calls == [] and json.loads(capsys.readouterr().out)['resume'] == []


def test_loop_continue_from_seeds_the_first_attempt(monkeypatch, tmp_path):
    starts = []
    monkeypatch.setattr(launcher, 'start', lambda argv: starts.append(argv) or tmp_path)
    monkeypatch.setattr(launcher, 'wait_for_result',
                        lambda state_dir, stall, **kw: {'status': 'done', 'stage': 'tested', 'summary': ''})
    monkeypatch.setattr(launcher, 'down', lambda argv: None)
    monkeypatch.setattr(launcher, 'close_terminal_windows', lambda prefix: pytest.fail('must not touch real windows'))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess([], 0, b'', b''))
    launcher.loop(['NGH-010', 'NGH-011', '--no-notify'], continue_from={'NGH-010': 'C:/old'})
    assert starts == [['NGH-010', '--continue-from', 'C:/old'], ['NGH-011']]


# ── result ladder ─────────────────────────────────────────────────────────────

def test_rung_comparison_and_result_stage():
    assert launcher.STAGES == ('implemented', 'tested', 'deployed', 'validated')
    assert [launcher.stage_rank(s) for s in launcher.STAGES] == [0, 1, 2, 3]
    assert launcher.stage_rank('bogus') == 0 and launcher.stage_rank(None) == 0
    assert launcher.meets_rung('tested', 'tested') and launcher.meets_rung('validated', 'deployed')
    assert not launcher.meets_rung('implemented', 'tested') and not launcher.meets_rung('bogus', 'tested')
    assert launcher.meets_rung('bogus', 'implemented')
    assert launcher.result_stage({'status': 'done'}) == 'implemented'          # pre-ladder result
    assert launcher.result_stage({'stage': 'shipped'}) == 'implemented'
    assert launcher.result_stage({'stage': 'deployed'}) == 'deployed'


@pytest.mark.parametrize('text, expected', [
    ('Ticket X: do it\n\nno bar here', 'tested'),
    ('Ticket X\n\nrequires: deployed\nmore', 'deployed'),
    ('Ticket X\nrequires: shipped\n', 'tested'),            # not a rung
    ('Ticket X\nREQUIRES:   Validated  \n', 'validated'),   # case and spacing
    ('Ticket X\nthis requires: deployed\n', 'tested'),     # only at the start of a line
    ('', 'tested')])
def test_parse_requires(text, expected):
    assert launcher.parse_requires(text) == expected


def test_shift_requires_reads_requires_txt_or_defaults(tmp_path):
    assert launcher.shift_requires(tmp_path) == 'tested'
    (tmp_path / 'requires.txt').write_text('deployed\n', encoding='utf-8')
    assert launcher.shift_requires(tmp_path) == 'deployed'
    (tmp_path / 'requires.txt').write_text('nonsense', encoding='utf-8')
    assert launcher.shift_requires(tmp_path) == 'tested'


def _ladder_loop(monkeypatch, tmp_path, result, requires=None):
    d = tmp_path / 'shift-ladder'
    d.mkdir()
    if requires:
        (d / 'requires.txt').write_text(requires, encoding='utf-8')
    (d / 'denials.json').write_text(json.dumps([{'time': 't1', 'role': 'Coder', 'line': 'Bash denied'}]),
                                    encoding='utf-8')
    starts, notes = [], []
    monkeypatch.setattr(launcher, 'start', lambda argv: starts.append(argv) or d)
    monkeypatch.setattr(launcher, 'wait_for_result', lambda state_dir, stall, **kw: dict(result))
    monkeypatch.setattr(launcher, 'down', lambda argv: None)
    monkeypatch.setattr(launcher, 'close_terminal_windows', lambda prefix: None)
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: notes.append(argv[-1]) or subprocess.CompletedProcess([], 0, b'', b''))
    return starts, notes


@pytest.mark.parametrize('result, requires, stage, rung', [
    ({'status': 'done', 'stage': 'implemented', 'summary': 'coded'}, None, 'implemented', 'tested'),
    ({'status': 'done', 'summary': 'coded'}, None, 'implemented', 'tested'),          # no stage = implemented
    ({'status': 'done', 'stage': 'tested', 'summary': 'coded'}, 'deployed', 'tested', 'deployed')])
def test_loop_stops_short_when_done_is_below_the_rung(monkeypatch, tmp_path, capsys, result, requires, stage, rung):
    starts, notes = _ladder_loop(monkeypatch, tmp_path, result, requires)
    with pytest.raises(SystemExit) as exc:
        launcher.loop(['NGH-060', 'NGH-061', '--retries', '2'])
    assert exc.value.code == 2 and starts == [['NGH-060']]                     # no retry, next ticket never started
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    ended = next(l for l in lines if l['event'] == 'ended')
    assert ended['status'] == 'short' and ended['stage'] == stage and ended['requires'] == rung
    assert ended['summary'] == f'stage {stage} below required {rung}: coded'
    assert lines[-1] == {'event': 'stopped', 'reason': 'short', 'ticket': 'NGH-060'}
    assert 'status short' in notes[0] and f'Stage: {stage}.' in notes[0] and 'Denials: Coder: Bash denied' in notes[0]


def test_loop_counts_done_at_the_rung_as_done(monkeypatch, tmp_path, capsys):
    _, notes = _ladder_loop(monkeypatch, tmp_path, {'status': 'done', 'stage': 'tested', 'summary': 'ok'})
    launcher.loop(['NGH-062', '--no-notify'])
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [l['event'] for l in lines] == ['started', 'ended', 'finished'] and lines[1]['status'] == 'done'
    assert 'Stage: tested.' in notes[0] and 'Denials' not in notes[0]


def test_result_without_stage_is_implemented_and_short_in_status(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    old = _shift(tmp_path, 'Nightshift-shift-lad00001', {'Foreman': 11})
    (old / 'result.json').write_text(json.dumps({'status': 'done'}), encoding='utf-8')
    (old / 'requires.txt').write_text('tested', encoding='utf-8')   # started with the ladder
    ok = _shift(tmp_path, 'Nightshift-shift-lad00002', {'Foreman': 12})
    (ok / 'result.json').write_text(json.dumps({'status': 'done', 'stage': 'deployed'}), encoding='utf-8')
    (ok / 'requires.txt').write_text('deployed', encoding='utf-8')
    _shift(tmp_path, 'Nightshift-shift-lad00003', {'Foreman': 13})
    record = launcher.shift_record(old)
    assert record['stage'] == 'implemented' and record['requires'] == 'tested' and record['result'] == 'short'
    launcher.status(['--all'])
    rows = {r['shift']: (r['result'], r['stage'], r['requires']) for r in json.loads(capsys.readouterr().out)}
    assert rows == {old.name: ('short', 'implemented', 'tested'), ok.name: ('done', 'deployed', 'deployed'),
                    'Nightshift-shift-lad00003': ('none', None, 'tested')}


def test_pre_ladder_done_shift_stays_done_unless_it_has_requires_txt(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: False)
    old = _ticket_shift(tmp_path, 'Nightshift-shift-pre00001', 'NGH-080', 100, 1000, handover=True)
    (old / 'result.json').write_text(json.dumps({'status': 'done'}), encoding='utf-8')   # no stage, no requires.txt
    record = launcher.shift_record(old)
    assert record['result'] == 'done' and record['stage'] == 'implemented'
    plan = launcher.resume_plan()
    assert plan['resume'] == [] and plan['skipped'] == [{'ticket': 'NGH-080', 'last_shift': old.name, 'reason': 'done'}]
    (old / 'requires.txt').write_text('tested', encoding='utf-8')
    assert launcher.shift_record(old)['result'] == 'short'
    plan = launcher.resume_plan()
    assert [(r['ticket'], r['last_result']) for r in plan['resume']] == [('NGH-080', 'short')] and plan['skipped'] == []


def test_start_writes_requires_txt_and_ticket_plan_carries_it(monkeypatch, tmp_path, capsys):
    cwd = _no_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, 'ticket_prompt',
                        lambda t: (t, f'Ticket {t}: x\n\nrequires: deployed\n\nDo it.', None))
    launcher.start(['--dry-run', '--cwd', cwd, 'NGH-070'])
    plan = json.loads(capsys.readouterr().out)
    assert plan['ticket'] == 'NGH-070' and plan['requires'] == 'deployed'
    state_dir = launcher.start(['--cwd', cwd, 'NGH-070'])
    assert (state_dir / 'requires.txt').read_text(encoding='utf-8') == 'deployed'
    free = launcher.start(['--cwd', str(tmp_path), 'plain task'])
    assert (free / 'requires.txt').read_text(encoding='utf-8') == 'tested'


def test_start_requires_ignores_a_continued_handover(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(launcher, 'ticket_prompt', lambda t: (t, f'Ticket {t}: x', None))
    prev = tmp_path / 'old-shift'
    prev.mkdir()
    (prev / 'handover.md').write_text('# Handover\nrequires: validated\n', encoding='utf-8')
    launcher.start(['--dry-run', '--continue-from', str(prev), 'NGH-071'])
    assert json.loads(capsys.readouterr().out)['requires'] == 'tested'


def test_handover_shows_stage_and_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'suite_result', lambda: ('passed', '1 passed'))
    (tmp_path / 'requires.txt').write_text('deployed', encoding='utf-8')
    (tmp_path / 'result.json').write_text(json.dumps({
        'status': 'done', 'stage': 'tested', 'summary': 's',
        'evidence': [{'stage': 'tested', 'what': 'pytest test_nightshift_launcher.py', 'outcome': '9 passed'}]}),
        encoding='utf-8')
    report = launcher.handover_report(tmp_path, {'roles': {}})
    assert report[2:6] == ['Status: done', 'Stage: tested (required: deployed)', 'Evidence:',
                           '- tested: pytest test_nightshift_launcher.py — 9 passed']
    assert not any(line.lower().startswith('requires:') for line in report)
    (tmp_path / 'result.json').write_text(json.dumps({'status': 'blocked'}), encoding='utf-8')
    report = launcher.handover_report(tmp_path, {'roles': {}})
    assert 'Stage: implemented (required: deployed)' in report and 'Evidence: (none)' in report

# ── durable crew mailbox: msg send/sent, inbox, ack ──────────────────────────

def _mail_shift(monkeypatch, tmp_path, started=None):
    """A shift whose Foreman/Coder .json files carry a 'started' incarnation."""
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    d = _shift(tmp_path, 'Nightshift-shift-mail0001', {'Foreman': 31, 'Coder': 32})
    for role, value in (started or {'Foreman': 100.5, 'Coder': 200.25}).items():
        info = json.loads((d / f'{role}.json').read_text(encoding='utf-8'))
        (d / f'{role}.json').write_text(json.dumps({**info, 'started': value}), encoding='utf-8')
    return d


def _relaunch_coder(d, started=999.0):
    info = json.loads((d / 'Coder.json').read_text(encoding='utf-8'))
    (d / 'Coder.json').write_text(json.dumps({**info, 'started': started}), encoding='utf-8')


def _send(capsys, *argv):
    launcher.msg(['send', 'mail0001', *argv])
    return capsys.readouterr().out


def _rows(d):
    with launcher.contextlib.closing(launcher.sqlite3.connect(d / 'mailbox.sqlite3')) as conn:
        conn.row_factory = launcher.sqlite3.Row
        return [dict(r) for r in conn.execute('SELECT * FROM messages ORDER BY id')]


def test_msg_send_prints_header_and_stores_both_incarnations(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    assert _send(capsys, 'foreman', 'Coder', '--kind', 'assignment', '--asg', 'A1', '--body', 'do X') == \
        'msg:1 kind:assignment asg:A1\n'
    assert _send(capsys, 'Nightshift-shift-mail0001-coder', 'FOREMAN', '--kind', 'note', '--body', 'hi') == \
        'msg:2 kind:note asg:-\n'                                    # mesh identity and any case are accepted
    first, second = _rows(d)
    assert {k: first[k] for k in ('kind', 'sender', 'sender_incarnation', 'recipient', 'recipient_incarnation',
                                  'asg', 'body', 'state')} == {
        'kind': 'assignment', 'sender': 'Foreman', 'sender_incarnation': 100.5, 'recipient': 'Coder',
        'recipient_incarnation': 200.25, 'asg': 'A1', 'body': 'do X', 'state': 'stored'}
    assert len(first['created']) == 19 and second['sender'] == 'Coder' and second['asg'] is None


def test_msg_send_unknown_role_is_a_plain_exit(monkeypatch, tmp_path, capsys):
    _mail_shift(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        launcher.msg(['send', 'mail0001', 'Reviewer', 'Coder', '--kind', 'note', '--body', 'x'])
    assert not isinstance(exc.value, launcher.ProtocolViolation)
    assert 'not a member' in str(exc.value.code) and 'Foreman, Coder' in str(exc.value.code)


def test_incarnation_is_none_without_member_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    d = _shift(tmp_path, 'Nightshift-shift-mail0001', {})
    _send(capsys, 'Foreman', 'Coder', '--kind', 'note', '--body', 'early')
    row = _rows(d)[0]
    assert row['sender_incarnation'] is None and row['recipient_incarnation'] is None


def test_msg_sent_marks_stored_only_and_rejects_unknown_id(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'note', '--body', 'b')
    launcher.msg(['sent', 'mail0001', '1'])
    assert json.loads(capsys.readouterr().out)['state'] == 'sent'
    launcher.ack(['mail0001', 'Coder', '1'])
    capsys.readouterr()
    launcher.msg(['sent', 'mail0001', '1'])
    assert json.loads(capsys.readouterr().out)['state'] == 'acknowledged'   # acknowledged rows are left alone
    with pytest.raises(SystemExit, match='no message msg:9'):
        launcher.msg(['sent', 'mail0001', '9'])
    assert len(_rows(d)) == 1


def test_inbox_has_exact_shape_and_reads_without_creating_a_mailbox(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    launcher.inbox(['mail0001', 'Coder'])
    assert json.loads(capsys.readouterr().out) == {'inbound_unacked': [], 'outbound_unsent': [], 'active_assignments': []}
    assert not (d / 'mailbox.sqlite3').exists()
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A1', '--body', 'do X')
    _send(capsys, 'Coder', 'Foreman', '--kind', 'note', '--body', 'question')
    launcher.inbox(['mail0001', 'coder'])
    view = json.loads(capsys.readouterr().out)
    assert set(view) == {'inbound_unacked', 'outbound_unsent', 'active_assignments'}
    assert [r['id'] for r in view['inbound_unacked']] == [1]
    assert [(r['id'], r['header'], r['to']) for r in view['outbound_unsent']] == \
        [(2, 'msg:2 kind:note asg:-', 'Nightshift-shift-mail0001-foreman')]
    assert [(r['id'], r['stale']) for r in view['active_assignments']] == [(1, False)]
    launcher.msg(['sent', 'mail0001', '2'])
    capsys.readouterr()
    launcher.inbox(['mail0001', 'Coder'])
    assert json.loads(capsys.readouterr().out)['outbound_unsent'] == []


def test_ack_happy_path_returns_stored_body_then_already_acknowledged(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A1', '--body', 'the real task')
    launcher.msg(['sent', 'mail0001', '1'])
    capsys.readouterr()
    launcher.ack(['mail0001', 'Coder', '1'])
    out = json.loads(capsys.readouterr().out)
    assert out['ack'] == 'acknowledged' and out['row']['body'] == 'the real task' and out['row']['state'] == 'acknowledged'
    launcher.ack(['mail0001', 'Nightshift-shift-mail0001-coder', '1'])
    assert json.loads(capsys.readouterr().out)['ack'] == 'already acknowledged'
    launcher.inbox(['mail0001', 'Coder'])
    assert json.loads(capsys.readouterr().out)['inbound_unacked'] == []
    assert len(_rows(d)) == 1


def test_ack_from_stored_is_allowed(monkeypatch, tmp_path, capsys):
    _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'note', '--body', 'sender crashed before msg sent')
    launcher.ack(['mail0001', 'Coder', '1'])
    assert json.loads(capsys.readouterr().out)['row']['state'] == 'acknowledged'


def _assert_violation(d, capsys, argv, reason_part, original=None):
    with pytest.raises(SystemExit) as exc:
        launcher.ack(argv)
    assert isinstance(exc.value, launcher.ProtocolViolation) and exc.value.code == 5
    captured = capsys.readouterr()
    assert reason_part in captured.err and captured.out == ''
    note = _rows(d)[-1]
    assert note['kind'] == 'note' and note['recipient'] == 'Foreman' and note['state'] == 'stored'
    assert note['body'].startswith('protocol violation:') and reason_part in note['body']
    if original:
        assert _rows(d)[0] == original                               # the original row is untouched


def test_ack_by_wrong_recipient_exits_5_and_notes_foreman(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Coder', 'Foreman', '--kind', 'result', '--asg', 'A1', '--body', 'done')
    original = _rows(d)[0]
    _assert_violation(d, capsys, ['mail0001', 'Coder', '1'], 'addressed to Foreman, not Coder', original)
    assert _rows(d)[-1]['sender'] == 'Coder' and 'msg:1' in _rows(d)[-1]['body']


def test_ack_for_a_stale_incarnation_exits_5(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A1', '--body', 'old task')
    original = _rows(d)[0]
    _relaunch_coder(d)
    _assert_violation(d, capsys, ['mail0001', 'Coder', '1'], 'earlier Coder incarnation', original)


def test_ack_of_unknown_id_exits_5(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _assert_violation(d, capsys, ['mail0001', 'Coder', '42'], 'no message msg:42')
    assert len(_rows(d)) == 1


def test_active_assignment_goes_stale_and_a_result_clears_it(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A1', '--body', 'one')
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A2', '--body', 'two')
    _relaunch_coder(d)
    launcher.inbox(['mail0001', 'Foreman'])
    assert [(r['asg'], r['stale']) for r in json.loads(capsys.readouterr().out)['active_assignments']] == \
        [('A1', True), ('A2', True)]
    _send(capsys, 'Coder', 'Foreman', '--kind', 'result', '--asg', 'A1', '--body', 'done one')
    for role in ('Foreman', 'Coder'):
        launcher.inbox(['mail0001', role])
        assert [r['asg'] for r in json.loads(capsys.readouterr().out)['active_assignments']] == ['A2']


def test_handover_lists_unacknowledged_messages(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'suite_result', lambda: ('passed', '1 passed'))
    d = _mail_shift(monkeypatch, tmp_path)
    report = launcher.handover_report(d, {'roles': {}})
    assert report[report.index('## Unacknowledged messages') + 2] == '(none)'
    assert not (d / 'mailbox.sqlite3').exists()
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A1', '--body', 'x')
    _send(capsys, 'Coder', 'Foreman', '--kind', 'note', '--body', 'y')
    _send(capsys, 'Coder', 'Foreman', '--kind', 'note', '--body', 'z')
    launcher.msg(['sent', 'mail0001', '2'])
    launcher.ack(['mail0001', 'Foreman', '3'])
    report = launcher.handover_report(d, {'roles': {}})
    start = report.index('## Unacknowledged messages')
    assert report[start + 2:start + 4] == ['- msg:1 assignment A1 Foreman -> Coder [stored]',
                                           '- msg:2 note - Coder -> Foreman [sent]']
    assert report[start + 4] == ''
    assert report.index('## Outstanding claims') < start < report.index('## Commits since shift start')


def test_status_all_shows_message_counts(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: False)
    launcher.status(['--all'])
    assert json.loads(capsys.readouterr().out)[0]['messages'] == {'unsent': 0, 'unacked': 0}
    assert not (d / 'mailbox.sqlite3').exists()
    for body in ('a', 'b', 'c'):
        _send(capsys, 'Foreman', 'Coder', '--kind', 'note', '--body', body)
    launcher.msg(['sent', 'mail0001', '2'])
    launcher.ack(['mail0001', 'Coder', '3'])
    capsys.readouterr()
    launcher.status(['--all'])
    assert json.loads(capsys.readouterr().out)[0]['messages'] == {'unsent': 1, 'unacked': 2}


def test_main_dispatches_mailbox_commands(monkeypatch, tmp_path, capsys):
    _mail_shift(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, 'argv', ['nightshift', 'msg', 'send', 'mail0001', 'Foreman', 'Coder',
                                      '--kind', 'note', '--body', 'via main'])
    launcher.main()
    assert capsys.readouterr().out == 'msg:1 kind:note asg:-\n'
    monkeypatch.setattr(sys, 'argv', ['nightshift', 'inbox', 'mail0001', 'Coder'])
    launcher.main()
    assert json.loads(capsys.readouterr().out)['inbound_unacked'][0]['body'] == 'via main'


def test_briefs_tell_both_roles_to_use_the_mailbox():
    shift = Path('C:/x/Nightshift-shift-abc')
    briefs = {'Foreman': launcher.role_brief('Foreman', 'S-foreman', 'S-coder', shift),
              'Coder': launcher.role_brief('Coder', 'S-coder', 'S-foreman', shift)}
    ns = _nightshift_cmd()
    for me, brief in briefs.items():
        peer = 'Coder' if me == 'Foreman' else 'Foreman'
        assert f'{ns} msg send Nightshift-shift-abc {me} {peer} --kind' in brief
        assert f'{ns} msg sent Nightshift-shift-abc <id>' in brief
        assert f'{ns} ack Nightshift-shift-abc {me} <id>' in brief
        assert f'{ns} inbox Nightshift-shift-abc {me}' in brief
        assert '{nightshift}' not in brief
        assert 'header line' in brief and 'never on the SendMessage text' in brief
        assert 'exit code 5 means do not act' in brief and 'resumed crew' in brief and 'outbound_unsent' in brief
    assert briefs['Foreman'].rstrip().endswith('verbatim:')
    assert 'send READY from S-coder' in briefs['Coder']
    assert f'{ns} claim Nightshift-shift-abc S-coder' in briefs['Coder']


# ── build slot ────────────────────────────────────────────────────────────────

class _FakeChild:
    def __init__(self, pid=4242, code=0):
        self.pid, self.code = pid, code

    def wait(self):
        return self.code


def _build_shift(monkeypatch, tmp_path, git=None, code=0, running=lambda pid, started: False):
    """A shift dir plus faked git, Popen, process checks and sleep; returns (state_dir, workdir, seen)."""
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    d = _shift(tmp_path, 'Nightshift-shift-bld00001', {'Foreman': 11, 'Coder': 12})
    work = tmp_path / 'work'
    work.mkdir()
    seen = {'popen': [], 'sleeps': 0, 'run': []}
    git = git if git is not None else {'rev-parse': (0, b'abcdef1234567890\n'), 'status': (0, b'')}

    def run(argv, **kwargs):
        seen['run'].append(argv)
        if argv[0] == 'git':
            code_, out = git[argv[3]]
            return subprocess.CompletedProcess(argv, code_, stdout=out, stderr=b'')
        return subprocess.CompletedProcess(argv, 0, stdout=b'', stderr=b'')

    def popen(cmd, **kwargs):
        seen['popen'].append((cmd, kwargs))
        kwargs['stdout'].write(b'build output\n')
        return _FakeChild(code=code)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher.subprocess, 'Popen', popen)
    monkeypatch.setattr(launcher, 'process_start_epoch', lambda pid: 1000.0)
    monkeypatch.setattr(launcher, 'process_running', running)
    monkeypatch.setattr(launcher, 'BUILD_POLL_SECONDS', 0)
    return d, work, seen


def _hold_build_lock(work, pid=999, n=7):
    path = launcher.build_lock_path(str(work.resolve()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'shift': 'Nightshift-shift-other', 'n': n, 'pid': pid, 'started_epoch': 5.0,
                                'started': '2026-10-16 01:00:00'}), encoding='utf-8')
    return path


def test_build_records_evidence_and_log(monkeypatch, tmp_path, capsys):
    d, work, seen = _build_shift(monkeypatch, tmp_path)
    launcher.build(['bld00001', '--cwd', str(work), '--cmd', 'pytest -q'])
    record = json.loads(capsys.readouterr().out)
    assert set(record) == {'n', 'cmd', 'cwd', 'head', 'dirty', 'porcelain', 'started', 'ended', 'exit', 'log',
                           'pid', 'status'}
    assert record['n'] == 1 and record['cmd'] == 'pytest -q' and record['cwd'] == str(work.resolve())
    assert (record['head'], record['dirty'], record['porcelain']) == ('abcdef1234567890', False, [])
    assert (record['exit'], record['status'], record['pid']) == (0, 'passed', 4242)
    assert record['log'] == str(d / 'builds' / '1.log')
    assert (d / 'builds' / '1.log').read_bytes() == b'build output\n'
    assert json.loads((d / 'builds' / '1.json').read_text(encoding='utf-8')) == record
    cmd, kwargs = seen['popen'][0]
    assert cmd == 'pytest -q' and kwargs['shell'] is True and kwargs['cwd'] == str(work.resolve())
    assert kwargs['stderr'] is subprocess.STDOUT
    assert not launcher.build_lock_path(str(work.resolve())).exists()   # released
    launcher.build(['bld00001', '--cwd', str(work), '--cmd', 'pytest -q'])
    assert json.loads(capsys.readouterr().out)['n'] == 2


def test_build_exit_code_is_the_commands(monkeypatch, tmp_path, capsys):
    d, work, _ = _build_shift(monkeypatch, tmp_path, code=3)
    with pytest.raises(SystemExit) as err:
        launcher.build(['bld00001', '--cwd', str(work), '--cmd', 'make'])
    assert err.value.code == 3
    assert json.loads((d / 'builds' / '1.json').read_text(encoding='utf-8'))['status'] == 'failed'


@pytest.mark.parametrize('git, dirty, porcelain, head', [
    ({'rev-parse': (0, b'abc\n'), 'status': (0, b'')}, False, [], 'abc'),
    ({'rev-parse': (0, b'abc\n'), 'status': (0, b' M x.py\n?? new.txt\n')}, True, [' M x.py', '?? new.txt'], 'abc'),
    ({'rev-parse': (128, b''), 'status': (128, b'')}, None, [], None),
])
def test_build_dirty_detection(monkeypatch, tmp_path, capsys, git, dirty, porcelain, head):
    _, work, _ = _build_shift(monkeypatch, tmp_path, git=git)
    launcher.build(['bld00001', '--cwd', str(work), '--cmd', 'x'])
    record = json.loads(capsys.readouterr().out)
    assert (record['head'], record['dirty'], record['porcelain']) == (head, dirty, porcelain)


def test_build_waits_for_a_live_holder_then_runs(monkeypatch, tmp_path, capsys):
    d, work, seen = _build_shift(monkeypatch, tmp_path, running=lambda pid, started: pid == 999)
    lock = _hold_build_lock(work)

    def sleep(seconds):
        seen['sleeps'] += 1
        assert json.loads((d / 'builds' / '1.json').read_text(encoding='utf-8'))['status'] == 'waiting'
        lock.unlink()   # the other build finishes
    monkeypatch.setattr(launcher.time, 'sleep', sleep)
    launcher.build(['bld00001', '--cwd', str(work), '--cmd', 'x'])
    assert seen['sleeps'] == 1 and len(seen['popen']) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'passed'


def test_build_times_out_with_exit_6_naming_the_holder(monkeypatch, tmp_path, capsys):
    d, work, seen = _build_shift(monkeypatch, tmp_path, running=lambda pid, started: True)
    lock = _hold_build_lock(work)
    with pytest.raises(SystemExit) as err:
        launcher.build(['bld00001', '--cwd', str(work), '--cmd', 'x', '--timeout', '0'])
    assert err.value.code == 6
    assert 'build #7 of shift Nightshift-shift-other (pid 999' in capsys.readouterr().err
    assert seen['popen'] == [] and lock.exists()   # the holder's lock is untouched
    assert json.loads((d / 'builds' / '1.json').read_text(encoding='utf-8'))['status'] == 'timeout'


def test_build_replaces_a_stale_holder(monkeypatch, tmp_path, capsys):
    checked = []
    _, work, seen = _build_shift(monkeypatch, tmp_path,
                                 running=lambda pid, started: checked.append((pid, started)) or False)
    _hold_build_lock(work)
    launcher.build(['bld00001', '--cwd', str(work), '--cmd', 'x'])
    assert checked == [(999, 5.0)] and len(seen['popen']) == 1 and seen['sleeps'] == 0


def test_process_running_checks_pid_and_start_time(monkeypatch):
    listed = subprocess.CompletedProcess([], 0, stdout=b'"python.exe","999","Console","1","10,000 K"\r\n', stderr=b'')
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([(['tasklist'], listed)]))
    monkeypatch.setattr(launcher, 'process_start_epoch', lambda pid: 100.0)
    assert launcher.process_running(999) is True               # any image name, not just claude/node
    assert launcher.process_running(999, 101.0) is True
    assert launcher.process_running(999, 500.0) is False       # PID reused by a later process
    assert launcher.process_running(99) is False               # 999 listed, 99 is not
    assert launcher.process_running(None) is False


def test_cancel_running_build_kills_its_pid_and_ends_cancelled(monkeypatch, tmp_path, capsys):
    d, work, seen = _build_shift(monkeypatch, tmp_path, code=1)
    real_wait = _FakeChild.wait

    def wait(child):
        launcher.build(['--cancel', 'bld00001', '1'])   # from another process while the build runs
        return real_wait(child)
    monkeypatch.setattr(_FakeChild, 'wait', wait)
    with pytest.raises(SystemExit):
        launcher.build(['bld00001', '--cwd', str(work), '--cmd', 'x'])
    assert ['taskkill', '/F', '/T', '/PID', '4242'] in seen['run']
    assert not any('/IM' in argv for argv in seen['run'])
    assert json.loads((d / 'builds' / '1.json').read_text(encoding='utf-8'))['status'] == 'cancelled'


def test_cancel_finished_build_does_not_kill(monkeypatch, tmp_path, capsys):
    _, work, seen = _build_shift(monkeypatch, tmp_path)
    launcher.build(['bld00001', '--cwd', str(work), '--cmd', 'x'])
    capsys.readouterr()
    launcher.build(['--cancel', 'bld00001', '1'])
    out = json.loads(capsys.readouterr().out)
    assert out['cancelled'] is False and 'already finished' in out['message']
    assert not any(argv[0] == 'taskkill' for argv in seen['run'])


def test_handover_lists_builds(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'suite_result', lambda: ('passed', '1 passed'))
    record = {'roles': {}}
    report = launcher.handover_report(tmp_path, record)
    assert report[report.index('## Builds') + 2] == '(none)'
    (tmp_path / 'builds').mkdir()
    (tmp_path / 'builds' / '1.json').write_text(json.dumps(
        {'n': 1, 'status': 'passed', 'exit': 0, 'head': 'abcdef1234567890', 'dirty': False, 'cmd': 'pytest -q'}),
        encoding='utf-8')
    report = launcher.handover_report(tmp_path, record)
    assert '- #1 passed exit=0 head=abcdef12 dirty=False pytest -q' in report
    assert report.index('## Unacknowledged messages') < report.index('## Builds') \
        < report.index('## Commits since shift start')


def test_build_locks_are_invisible_to_shift_locks(monkeypatch, tmp_path):
    _, work, _ = _build_shift(monkeypatch, tmp_path)
    _hold_build_lock(work)
    assert launcher.build_lock_path(str(work)).name == launcher.lock_path(str(work)).name
    assert launcher.read_locks() == [] and launcher.stale_locks() == []


def test_main_dispatches_build(monkeypatch, tmp_path, capsys):
    _, work, seen = _build_shift(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, 'argv', ['nightshift', 'build', 'bld00001', '--cwd', str(work), '--cmd', 'x'])
    launcher.main()
    assert json.loads(capsys.readouterr().out)['status'] == 'passed'


def test_process_start_epoch_survives_wmic_timeout(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get('timeout'))
    monkeypatch.setattr(launcher.subprocess, 'run', slow)
    assert launcher.process_start_epoch(1234) is None


# ── PowerShell launchers parse ────────────────────────────────────────────────

@pytest.mark.parametrize('name', ['launch.ps1', 'launch-safe.ps1', 'lib-session-window.ps1'])
def test_powershell_launcher_parses_without_errors(name):
    # The session launchers stay in C:\scripts (they serve every tool, not just Nightshift).
    path = Path('C:/scripts') / name
    script = ('$errs = $null; [void][System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$errs); '
              '$errs | ForEach-Object { $_.ToString() }')
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', '& {' + script + '}', str(path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '', f'{name} parse errors:\n{result.stdout}'

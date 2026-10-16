"""Tests for the Nightshift shifts launcher (nightshift_shifts.py).

Run: C:/miniconda3/envs/python312/python.exe -m pytest C:/git/nightshift-shifts/test_nightshift_shifts.py -q
Nothing here launches Windows Terminal or agent sessions. Worktree integration
tests use real temporary Git repositories and small Python build commands.
"""
import json
import os
import re
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
    """No test reads or writes the real floor locks or legacy shifts."""
    monkeypatch.setattr(launcher, 'LOCKS_DIR', tmp_path / 'locks')
    monkeypatch.setattr(launcher, 'LEGACY_DIR', tmp_path / 'legacy', raising=False)
    monkeypatch.setattr(launcher, 'WORKTREES_DIR', tmp_path / 'worktrees')
    monkeypatch.setattr(launcher, 'FLOOR_FILE', tmp_path / 'floor.json')
    # The real user's home is itself a Git checkout; temp fixtures must not
    # discover that repository when they intentionally exercise non-git mode.
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(tmp_path.parent))


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
    assert _titles(argv) == ['Nightshift-shift-abc shift', 'Nightshift-shift-abc shift']
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
    assert _titles(argv) == ['NGH-019 shift', 'NGH-019 shift']
    assert argv.count('--suppressApplicationTitle') == 2
    assert argv[argv.index('--pane') + 1] == 'Foreman'   # the wrapper still gets the bare role label
    argv = launcher.wt_command('Nightshift-shift-abc', Path('C:/x'), agents=True, mode_flag='--start', cwd='C:/w')
    assert _titles(argv) == ['Nightshift-shift-abc shift', 'Nightshift-shift-abc shift']
    assert launcher.pane_title('NGH-019') == 'NGH-019 shift'


def test_start_dry_run_from_a_ticket_titles_panes_with_it(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    monkeypatch.setattr(launcher, 'ticket_prompt', lambda t: (t, 'Ticket NGH-019: x', str(tmp_path)))
    launcher.start(['--dry-run', 'NGH-019'])
    plan = json.loads(capsys.readouterr().out)
    assert {r: v['pane_title'] for r, v in plan['roles'].items()} == {'Foreman': 'NGH-019 shift',
                                                                     'Advisor': 'NGH-019 shift',
                                                                     'Coder': 'NGH-019 shift',
                                                                     'Apprentice': 'NGH-019 shift',
                                                                     'Tester': 'NGH-019 shift'}
    # every pane of the container, crew and empty slot alike, carries the one shift title (NGH-028)
    assert _titles(plan['wt_argv']) == ['NGH-019 shift'] * 8
    assert [s['pane_title'] for s in plan['slots']] == ['NGH-019 shift'] * 8


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
    import re
    keys = list(launcher.COMMANDS)
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


def test_default_manifest_is_foreman_advisor_coder_tester_apprentice():
    """NGH-027: the Advisor sits second, on its own model; NGH-029: the Apprentice fifth, on agy Gemini Flash."""
    crew = launcher.load_crew()
    assert crew == {'name': 'default', 'model': 'claude-opus-5-5', 'permission_mode': 'auto',
                    'contract': 'result.json', 'layout': '2x4', 'slots': 8, 'roles': [
                        {'role': 'Foreman', 'brief': 'foreman.md', 'persona': 'foreman.yaml', 'count': 1, 'human_contact': True, 'colour': 'red',
                         'tool': 'claude', 'model': 'claude-opus-5-5'},
                        {'role': 'Advisor', 'brief': 'advisor.md', 'persona': 'advisor.yaml', 'count': 1, 'human_contact': False,
                         'colour': 'purple', 'tool': 'claude', 'model': 'claude-fable-5-1'},
                        {'role': 'Coder', 'brief': 'coder.md', 'persona': 'coder.yaml', 'count': 1, 'human_contact': False, 'colour': 'green',
                         'tool': 'claude', 'model': 'claude-opus-5-5'},
                        {'role': 'Tester', 'brief': 'tester.md', 'persona': 'tester.yaml', 'count': 1, 'human_contact': False, 'colour': 'blue',
                         'tool': 'claude', 'model': 'claude-opus-5-5'},
                        {'role': 'Apprentice', 'brief': 'apprentice.md', 'persona': 'apprentice.yaml', 'count': 1, 'human_contact': False,
                         'colour': 'yellow', 'tool': 'agy', 'model': 'gemini-3.8-flash-high', 'permissions': 'skip'}]}
    members = launcher.crew_members(crew, 'S')
    assert [(m['label'], m['identity'], m['peer']) for m in members] == [
        ('Foreman', 'S-foreman', 'S-advisor, S-coder, S-tester, S-apprentice'),
        ('Advisor', 'S-advisor', 'S-foreman'),
        ('Coder', 'S-coder', 'S-foreman'),
        ('Tester', 'S-tester', 'S-foreman'),
        ('Apprentice', 'S-apprentice', 'S-foreman')]
    assert [(m['label'], m['model'], m['colour']) for m in members] == [
        ('Foreman', 'claude-opus-5-5', 'red'), ('Advisor', 'claude-fable-5-1', 'purple'),
        ('Coder', 'claude-opus-5-5', 'green'), ('Tester', 'claude-opus-5-5', 'blue'),
        ('Apprentice', 'gemini-3.8-flash-high', 'yellow')]


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


# ── personas above the protocol (NGH-032) ────────────────────────────────────

def test_every_default_role_brief_starts_with_its_persona():
    """The persona's identity comes before common.md; placeholders are filled across the whole text."""
    import yaml
    common = 'in a Nightshift crew.'   # common.md's opening line, after {role} is filled
    members = launcher.crew_members(launcher.load_crew(), 'Nightshift-shift-abc')
    for m in members:
        persona = yaml.safe_load((launcher.PERSONAS_DIR / m['persona']).read_text(encoding='utf-8'))
        identity = ' '.join(persona['identity'].split())
        brief = launcher.role_brief(m['role'], m['identity'], m['peer'], 'C:/x/Nightshift-shift-abc', m['brief'],
                                    m['persona'])
        assert brief.startswith(f"# {m['role']}\n"), m['role']
        assert identity in ' '.join(brief.split()), m['role']
        assert brief.index('## What good looks like') < brief.index('## How you work') < \
            brief.index('## What you never do') < brief.index('# Protocol') < brief.index(common), m['role']
        for placeholder in ('{identity}', '{shift}', '{nightshift}', '{role}', '{peer_name}', '{state_dir}'):
            assert placeholder not in brief, (m['role'], placeholder)
        assert m['identity'] in brief and 'Nightshift-shift-abc' in brief


def test_advisor_persona_names_the_search_order():
    advisor = ' '.join(launcher.role_brief('Advisor', 'S-advisor', 'S-foreman', 'C:/x/S', 'advisor.md').split())
    order = [advisor.index(s) for s in ('tasks-cli', 'memory store_search', 'confluence-cli', 'the web')]
    assert order == sorted(order) and order[-1] < advisor.index('# Protocol')


def _persona_dir(monkeypatch, tmp_path, files):
    personas = tmp_path / 'personas'
    personas.mkdir()
    for name, text in files.items():
        (personas / name).write_text(text, encoding='utf-8')
    monkeypatch.setattr(launcher, 'PERSONAS_DIR', personas)


PERSONA_CREW = ('roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                '  - {role: Coder, brief: coder.md, persona: %s}\n')


def test_manifest_persona_key_overrides_the_default(monkeypatch, tmp_path):
    _persona_dir(monkeypatch, tmp_path, {'coder.yaml': 'identity: plain coder\n',
                                         'careful.yaml': 'identity: careful {identity}\nnever: [guess]\n'})
    _crew_file(monkeypatch, tmp_path, 'p', PERSONA_CREW % 'careful.yaml')
    coder = launcher.crew_members(launcher.load_crew('p'), 'S')[1]
    assert coder['persona'] == 'careful.yaml'
    brief = launcher.role_brief('Coder', coder['identity'], coder['peer'], '', coder['brief'], coder['persona'])
    assert brief.startswith('# Coder\n\ncareful S-coder\n') and '- guess' in brief and 'plain coder' not in brief


@pytest.mark.parametrize('files, message', [({}, 'persona ghost.yaml for Coder'),
                                            ({'ghost.yaml': '- a list\n'}, 'not a YAML mapping')])
def test_load_crew_refuses_a_missing_or_unusable_persona(monkeypatch, tmp_path, files, message):
    _persona_dir(monkeypatch, tmp_path, files)
    _crew_file(monkeypatch, tmp_path, 'p', PERSONA_CREW % 'ghost.yaml')
    with pytest.raises(SystemExit, match=message):
        launcher.load_crew('p')


@pytest.mark.parametrize('text, message', [('never: do one thing\n', 'never must be a list of text'),
                                           ('how_you_work: [{a: b}]\n', 'how_you_work must be a list of text'),
                                           ('identity: [a, b]\n', 'identity must be text'),
                                           ('', 'not a YAML mapping')])
def test_load_crew_refuses_a_malformed_persona_named_or_default(monkeypatch, tmp_path, text, message):
    """A bare string list key would render one bullet per character; a broken default personas/<role>.yaml
    must fail at load_crew, not later at pane --start inside a live pane."""
    _persona_dir(monkeypatch, tmp_path, {'coder.yaml': 'identity: x\n' + text if text else '',
                                         'ghost.yaml': 'identity: x\n' + text if text else ''})
    _crew_file(monkeypatch, tmp_path, 'named', PERSONA_CREW % 'ghost.yaml')
    _crew_file(monkeypatch, tmp_path, 'dflt', 'roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                                              '  - {role: Coder, brief: coder.md}\n')
    for crew, persona in (('named', 'ghost.yaml'), ('dflt', 'coder.yaml')):
        with pytest.raises(SystemExit, match=f'crew {crew}: persona {persona} for Coder.*{message}'):
            launcher.load_crew(crew)
    with pytest.raises(SystemExit, match=message):
        launcher.persona_text('Coder', 'coder.yaml')


def test_load_crew_checks_only_the_persona_a_role_ends_up_with(monkeypatch, tmp_path):
    """A named persona replaces the default, so a broken personas/<role>.yaml it overrides must not fail the crew."""
    _persona_dir(monkeypatch, tmp_path, {'coder.yaml': 'never: do one thing\n', 'careful.yaml': 'identity: careful\n'})
    _crew_file(monkeypatch, tmp_path, 'p', PERSONA_CREW % 'careful.yaml')
    assert launcher.crew_members(launcher.load_crew('p'), 'S')[1]['persona'] == 'careful.yaml'


def test_persona_with_empty_or_absent_sections_still_loads_and_renders(monkeypatch, tmp_path):
    _persona_dir(monkeypatch, tmp_path, {'coder.yaml': 'good_looks_like: []\nhow_you_work:\nnever: [guess]\n'})
    _crew_file(monkeypatch, tmp_path, 'p', PERSONA_CREW % 'coder.yaml')
    launcher.load_crew('p')
    text = launcher.persona_text('Coder', 'coder.yaml')
    assert '- guess' in text and 'What good looks like' not in text and 'How you work' not in text


def test_role_without_a_persona_file_still_renders(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'rev', 'roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                                             '  - {role: Reviewer, brief: coder.md}\n')
    reviewer = launcher.crew_members(launcher.load_crew('rev'), 'S')[1]
    assert reviewer['persona'] is None
    brief = launcher.role_brief('Reviewer', 'S-reviewer', 'S-foreman', '', 'coder.md', reviewer['persona'],
                                cwd=tmp_path)
    assert brief.startswith('# Reviewer\n\n# Protocol\n') and brief.count('# Protocol\n') == 1
    assert 'You are Reviewer in a Nightshift crew.' in brief


def test_committed_personas_have_every_section_as_text_and_lists_of_text():
    """persona_text iterates the list keys: a bare string there would render one bullet per character."""
    import yaml
    files = sorted(launcher.PERSONAS_DIR.glob('*.yaml'))
    assert {p.stem for p in files} >= {'foreman', 'advisor', 'coder', 'tester', 'apprentice'}
    for path in files:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
        assert isinstance(data, dict) and isinstance(data.get('identity'), str) and data['identity'].strip(), path
        for key in ('good_looks_like', 'how_you_work', 'never'):
            assert isinstance(data.get(key), list) and data[key], (path, key)
            assert all(isinstance(i, str) and i.strip() for i in data[key]), (path, key)


@pytest.mark.parametrize('crew', sorted(p.stem for p in launcher.CREWS_DIR.glob('*.yaml')))
def test_every_shipped_crew_loads_and_renders_its_personas(crew):
    """Not only default.yaml: every manifest still loads, and each persona-bearing role's brief opens with it."""
    for m in launcher.crew_members(launcher.load_crew(crew), 'Nightshift-shift-abc'):
        brief = launcher.role_brief(m['role'], m['identity'], m['peer'], 'C:/x/Nightshift-shift-abc', m['brief'],
                                    m['persona'])
        assert not any(p in brief for p in ('{role}', '{identity}', '{peer_name}', '{state_dir}', '{shift}',
                                            '{nightshift}')), (crew, m['label'])
        if m['persona']:
            assert brief.startswith(f"# {m['role']}\n") and '\n# Protocol\n' in brief, (crew, m['label'])


@pytest.mark.parametrize('persona', ['team/careful.yaml', 'team\\careful.yaml'])
def test_manifest_persona_in_a_subfolder_with_either_slash(monkeypatch, tmp_path, persona):
    _persona_dir(monkeypatch, tmp_path, {})
    (tmp_path / 'personas' / 'team').mkdir()
    (tmp_path / 'personas' / 'team' / 'careful.yaml').write_text('identity: careful one\n', encoding='utf-8')
    _crew_file(monkeypatch, tmp_path, 'p', PERSONA_CREW % f"'{persona}'")
    coder = launcher.crew_members(launcher.load_crew('p'), 'S')[1]
    assert launcher.role_brief('Coder', coder['identity'], coder['peer'], '', coder['brief'],
                               coder['persona']).startswith('# Coder\n\ncareful one\n')


@pytest.mark.parametrize('tool, model', [('claude', 'claude-opus-5-5'), ('codex', 'gpt-5.5'),
                                         ('agy', 'gemini-3.8-flash-high'), ('copilot', 'gpt-5.5')])
def test_pane_start_carries_the_persona_to_every_cli_from_an_old_crew_json(monkeypatch, tmp_path, tool, model):
    """A crew.json written before NGH-032 has no persona key: --start falls back to personas/<role>.yaml,
    and the brief that reaches the CLI's argv (system prompt or initial prompt) opens with it."""
    import yaml
    shift = 'Nightshift-shift-pers0001'
    state = tmp_path / shift
    state.mkdir()
    (state / 'prompt.txt').write_text('THE TASK', encoding='utf-8')
    members = [_member(shift, 'Foreman', human=True, colour='red', peer=f'{shift}-tester'),
               _member(shift, 'Tester', colour='blue', tool=tool, model=model)]
    assert all('persona' not in m for m in members)
    (state / 'crew.json').write_text(json.dumps({'crew': 'default', 'members': members, 'slots': 2,
                                                 'permission_mode': 'auto'}), encoding='utf-8')
    seen = {}

    class Child:
        pid = 91

        def wait(self):
            return 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda cmd, **k: seen.setdefault('cmd', cmd) and Child())
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: None)
    for name in ('accept_trust_prompt', 'set_role_colour', 'register_name', 'set_codex_role_name',
                 'set_pane_background'):
        monkeypatch.setattr(launcher, name, lambda *a, **k: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda *a: True)
    monkeypatch.setattr(launcher, 'inject_command', lambda *a, **kw: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane('Tester', str(state), start=True, cwd=str(tmp_path))
    identity = ' '.join(yaml.safe_load((launcher.PERSONAS_DIR / 'tester.yaml').read_text(encoding='utf-8'))
                        ['identity'].split())
    carrier = [a for a in seen['cmd'] if '# Protocol' in a]
    assert len(carrier) == 1, tool
    assert carrier[0].startswith('# Tester\n') and identity in ' '.join(carrier[0].split()), tool
    assert f'{shift}-tester' in carrier[0] and '{identity}' not in carrier[0], tool


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
    assert [argv[i + 1] for i, a in enumerate(argv) if a == '--pane'] == order
    assert _titles(argv) == ['S shift'] * n
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
    assert plan['roles']['Coder-2'] == {'session_name': f'{s}-coder-2', 'pane_title': f'{s} shift', 'peer': f'{s}-foreman',
                                        'receives_prompt': False, 'first_message': launcher.START_MESSAGES['Coder'],
                                        'compaction': {'automatic': 'disabled',
                                                       'environment': {'DISABLE_AUTO_COMPACT': '1'}},
                                        'permission_flags': ['--permission-mode', plan['permission_mode']]}
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
    monkeypatch.setattr(launcher, 'set_role_colour', lambda child, colour, cwd: seen.update(colour=colour) or True)
    monkeypatch.setattr(launcher, 'inject_command', lambda *a, **kw: True)
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
    assert json.loads((state / 'Foreman.json').read_text(encoding='utf-8'))['tool'] == 'claude'


def _capture_role_launch(monkeypatch, tmp_path, role, cwd, *, member_role=None, tool=None):
    """Exercise pane argv assembly without starting a CLI, terminal or trust dialog."""
    state = tmp_path / 'state outside repo' / 'Nightshift-shift-deny0001'
    state.mkdir(parents=True)
    (state / 'prompt.txt').write_text('THE TASK', encoding='utf-8')
    if member_role or tool:
        members = launcher.crew_members(launcher.load_crew(), state.name)
        member = next(m for m in members if m['role'] == (member_role or role))
        member['label'] = role
        if tool:
            member['tool'] = tool
        (state / 'crew.json').write_text(json.dumps({'crew': 'default', 'members': [member]}), encoding='utf-8')
    seen = {}

    class Child:
        pid = 42

        def wait(self):
            return 0

    monkeypatch.setattr(launcher.subprocess, 'Popen',
                        lambda cmd, **kw: seen.update(cmd=cmd, cwd=kw['cwd']) or Child())
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda *a, **kw: pytest.fail('unexpected subprocess.run'))
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda *a: True)
    monkeypatch.setattr(launcher, 'inject_command', lambda pid, message, **kw: seen.update(opening=message) or True)
    monkeypatch.setattr(launcher, 'register_name', lambda *a: None)
    monkeypatch.setattr(launcher, 'set_codex_role_name', lambda *a: None)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane(role, str(state), start=True, cwd=cwd)
    return seen, state


def _launch_deny_rules(command):
    if '--settings' not in command:
        return []
    settings = command[command.index('--settings') + 1]
    return json.loads(settings)['permissions'].get('deny', [])


@pytest.mark.parametrize('repo_name,escaped_name', [('other-worktree', 'other-worktree'),
                                                 ('repo with spaces', 'repo with spaces'),
                                                 ('repo [live]', r'repo \[live\]')])
def test_foreman_edit_denies_use_actual_cwd_and_leave_external_state_writable(monkeypatch, tmp_path, repo_name, escaped_name):
    repo = tmp_path / repo_name
    repo.mkdir()
    # Resolve a noncanonical spelling, not the launcher repo or its parent directory.
    cwd = str(repo / '..' / repo_name)
    seen, state = _capture_role_launch(monkeypatch, tmp_path, 'Foreman', cwd)
    command = seen['cmd']
    rules = _launch_deny_rules(command)
    absolute = tmp_path.resolve().as_posix() + '/' + escaped_name
    if repo.drive:
        absolute = '/' + repo.drive[0].lower() + absolute[len(repo.drive):]
    pattern = '/' + absolute + '/**'
    # Claude checks all built-in editors against Edit(path); separate Write/NotebookEdit/
    # MultiEdit path rules would be inert (Claude permissions reference).
    assert rules == [f'Edit({pattern})']
    assert json.loads(command[command.index('--settings') + 1]) == {'permissions': {'deny': rules}}
    assert seen['cwd'] == cwd
    assert command[command.index('--permission-mode') + 1] == 'auto'
    assert launcher.START_MESSAGES['Foreman'] not in command
    assert seen['opening'] == launcher.START_MESSAGES['Foreman']
    assert command[command.index('--append-system-prompt') + 1].endswith('THE TASK')
    # The only denied tree is repo; result.json/handover.md remain outside it.
    assert not state.resolve().is_relative_to(repo.resolve())
    assert all(str(state) not in rule for rule in rules)
    assert not any(arg in command for arg in ('--allowedTools', '--dangerously-skip-permissions'))


@pytest.mark.parametrize('role', ['Coder', 'Tester'])
def test_coder_and_tester_launches_do_not_inherit_foreman_edit_denies(monkeypatch, tmp_path, role):
    seen, _ = _capture_role_launch(monkeypatch, tmp_path, role, str(tmp_path))
    assert not _launch_deny_rules(seen['cmd'])
    assert '--disallowedTools' not in seen['cmd']
    assert seen['cmd'][seen['cmd'].index('--permission-mode') + 1] == 'auto'


@pytest.mark.parametrize('label,member_role,denied', [('Captain', 'Foreman', True), ('Foreman', 'Coder', False)])
def test_foreman_edit_denial_follows_member_role_not_pane_label(monkeypatch, tmp_path, label, member_role, denied):
    seen, _ = _capture_role_launch(monkeypatch, tmp_path, label, str(tmp_path), member_role=member_role)
    assert bool(_launch_deny_rules(seen['cmd'])) is denied


@pytest.mark.parametrize('tool', ['codex', 'copilot', 'agy'])
def test_non_claude_foreman_has_no_claude_settings_argument(monkeypatch, tmp_path, tool):
    seen, _ = _capture_role_launch(monkeypatch, tmp_path, 'Foreman', str(tmp_path), tool=tool)
    assert '--settings' not in seen['cmd']


@pytest.mark.parametrize('brief_file', ['foreman.md', 'foreman-cli.md', 'foreman-agy-coder.md'])
def test_foreman_brief_forbids_repo_edits_and_suite_substitution(brief_file):
    brief = launcher.role_brief('Foreman', 'S-foreman', 'S-coder, S-tester', 'C:/state/S', brief_file).lower()
    assert 'never edit repository files yourself' in brief
    assert 'never run the test suite yourself' in brief
    if brief_file != 'foreman-agy-coder.md':
        assert 'ordinary' in brief and 'apprentice' in brief
        assert re.search(r'tester owns (?:verification and )?test-file edits', brief)
    assert 'coder' in brief and 'assignment' in brief and 'verification' in brief and 'tester' in brief
    assert re.search(r'foreman commits (?:the passed|exactly that passed) change', brief)
    assert 'commit only what tester passes' in brief
    assert 'coder owns the launcher and implementation commits' not in brief


@pytest.mark.parametrize('brief_file', ['foreman.md', 'foreman-cli.md'])
def test_foreman_worktree_contract_orders_review_commit_clean_test_then_land(brief_file):
    brief = (launcher.BRIEFS_DIR / brief_file).read_text(encoding='utf-8').lower()
    contract = brief.split('shift contract:', 1)[1].split('worktree and landing', 1)[0]
    stages = ['tester first passes the reviewed revision',
              'foreman commits exactly that passed change',
              'then tester must run', 'exact clean head',
              'when worktree.json exists', 'foreman then lands only that unchanged']
    positions = [contract.index(stage) for stage in stages]
    assert positions == sorted(positions)
    assert 'legacy/direct/non-git shifts skip land' in contract
    assert 'foreman never substitutes its own suite run' in contract
    landing = brief.split('worktree and landing', 1)[1].split('result ladder:', 1)[0]
    assert re.search(r'exit 2.*dispatch tester.*rerun `land`', landing)
    assert re.search(r'exit 4.*coder to resolve.*tester pass.*foreman commits.*tester verifies', landing)
    assert '--keep-conflicts' in landing and '--hold' in landing


# ── per-role tool: agy (Gemini) Coder (NGH-012) ───────────────────────────────

MIXED = """\
model: claude-sonnet-5-5
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true}
  - {role: Coder, brief: coder.md, tool: agy, model: gemini-x}
  - {role: Reviewer, brief: coder.md}
"""


def test_manifest_tool_defaults_to_claude_and_roles_take_their_own_model(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'mixed', MIXED)
    crew = launcher.load_crew('mixed')
    assert [(r['role'], r['tool'], r['model']) for r in crew['roles']] == [
        ('Foreman', 'claude', 'claude-sonnet-5-5'), ('Coder', 'agy', 'gemini-x'), ('Reviewer', 'claude', 'claude-sonnet-5-5')]
    members = launcher.crew_members(crew, 'S')
    assert [(m['tool'], m['model']) for m in members] == [
        ('claude', 'claude-sonnet-5-5'), ('agy', 'gemini-x'), ('claude', 'claude-sonnet-5-5')]


def test_manifest_rejects_an_unknown_tool_naming_the_allowed_ones(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'bad', 'roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                                             '  - {role: Coder, brief: coder.md, tool: opencode}\n')
    with pytest.raises(SystemExit, match="tool 'opencode' for Coder is not supported; use one of: claude, agy, codex, copilot"):
        launcher.load_crew('bad')


@pytest.mark.parametrize('tool, permissions, flags', [
    ('claude', 'skip', ['--dangerously-skip-permissions']),
    ('claude', 'auto', ['--permission-mode', 'auto']),
    ('claude', 'default', []),
    ('claude', 'plan', ['--permission-mode', 'plan']),   # a crew-level permission_mode passes through to Claude
    ('claude', None, ['--permission-mode', 'auto']),
    ('agy', 'skip', ['--dangerously-skip-permissions']),
    ('agy', 'auto', ['--dangerously-skip-permissions']),
    ('agy', 'default', []),
    ('codex', 'skip', ['--dangerously-bypass-approvals-and-sandbox']),
    ('codex', 'auto', ['--dangerously-bypass-approvals-and-sandbox']),
    ('codex', 'default', []),
    ('copilot', 'skip', ['--allow-all']),
    ('copilot', 'auto', ['--allow-all']),
    ('copilot', 'default', []),
    ('agy', 'plan', [])])
def test_permission_flags_map_each_value_per_tool(tool, permissions, flags):
    assert launcher.permission_flags(tool, permissions) == flags


def test_cli_commands_take_their_permission_flags_from_the_mapping():
    agy = launcher.agy_command('m', 'B', 'G', permissions='default')
    assert '--dangerously-skip-permissions' not in agy and '--mode' not in agy
    codex = launcher.codex_command('m', 'B', 'G', permissions='default')
    assert '--dangerously-bypass-approvals-and-sandbox' not in codex and codex[1] == '--no-daemon'
    copilot = launcher.copilot_command('m', 'B', 'G', permissions='skip')
    assert copilot[1] == '--allow-all'


def test_manifest_permissions_key_is_optional_and_checked(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'perm', """\
name: perm
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true}
  - {role: Coder, brief: coder.md, tool: agy, permissions: default}
""")
    crew = launcher.load_crew('perm')
    assert 'permissions' not in crew['roles'][0] and crew['permission_mode'] == 'auto'   # defaults to the crew's
    assert crew['roles'][1]['permissions'] == 'default'
    assert launcher.crew_members(crew, 'S')[1]['permissions'] == 'default'
    _crew_file(monkeypatch, tmp_path, 'bad', """\
name: bad
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true}
  - {role: Coder, brief: coder.md, permissions: yolo}
""")
    with pytest.raises(SystemExit, match="permissions 'yolo' for Coder must be one of: skip, auto, default"):
        launcher.load_crew('bad')


@pytest.mark.parametrize('name', ['default', 'apprentice-gemini', 'apprentice-codex', 'apprentice-copilot'])
def test_apprentice_runs_with_permissions_skip_and_claude_roles_keep_auto(name):
    crew = launcher.load_crew(name)
    roles = {r['role']: r for r in crew['roles']}
    assert roles['Apprentice']['permissions'] == 'skip'
    assert crew['permission_mode'] == 'auto'
    assert all('permissions' not in r for label, r in roles.items() if label != 'Apprentice')


def test_dry_run_shows_each_panes_resolved_permission_flags(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', '--crew', 'apprentice-codex', 'task'])
    roles = json.loads(capsys.readouterr().out)['roles']
    assert roles['Apprentice']['permission_flags'] == ['--dangerously-bypass-approvals-and-sandbox']
    assert roles['Coder']['permission_flags'] == ['--permission-mode', 'auto']


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_crew_carries_permissions_and_pane_uses_them(tmp_path, injected, monkeypatch, capsys):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--crew', 'apprentice-gemini'])
    member = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert member['permissions'] == 'skip'
    seen = {}

    class Child:
        pid = 42

        def wait(self):
            return 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda cmd, **k: seen.setdefault('cmd', cmd) and Child())
    monkeypatch.setattr(launcher, 'register_name', lambda pid, name: None)
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: None)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: True)
    launcher.pane('Apprentice', str(d), start=True, cwd=str(tmp_path), slot=3)
    assert seen['cmd'][:4] == [str(launcher.AGY_EXE), '--model', 'gemini-3.8-flash-high',
                               '--dangerously-skip-permissions']


@pytest.mark.usefixtures('_container_isolated')
def test_pane_claude_role_with_permissions_default_gets_no_permission_flag(tmp_path, monkeypatch):
    state = tmp_path / CONT_SHIFT
    state.mkdir()
    coder = {**_member(CONT_SHIFT, 'Coder'), 'permissions': 'default'}
    members = [_member(CONT_SHIFT, 'Foreman', human=True, colour='red', peer=f'{CONT_SHIFT}-coder'), coder]
    (state / 'crew.json').write_text(json.dumps({'crew': 'default', 'members': members, 'slots': 4,
                                                 'permission_mode': 'auto'}), encoding='utf-8')
    seen = {}

    class Child:
        pid = 42

        def wait(self):
            return 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda cmd, **k: seen.setdefault('cmd', cmd) and Child())
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda child: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda child, colour, cwd: True)
    monkeypatch.setattr(launcher, 'inject_command', lambda *a, **kw: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: True)
    launcher.pane('Coder', str(state), start=True, cwd=str(tmp_path))
    assert '--permission-mode' not in seen['cmd'] and '--dangerously-skip-permissions' not in seen['cmd']


def test_agy_command_argv_is_exact():
    assert launcher.agy_command('gemini-x', 'BRIEF', 'GO') == [
        str(launcher.AGY_EXE), '--model', 'gemini-x', '--dangerously-skip-permissions', '-i', 'BRIEF\n\nGO']
    assert launcher.AGY_EXE.parts[-4:] == ('Microsoft', 'WinGet', 'Links', 'agy.exe')


def test_register_name_calls_agents_cli_and_swallows_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: calls.append((argv, k['timeout'])))
    launcher.register_name(42, 'S-coder')
    assert calls == [(['agents-cli', 'name', '42', 'S-coder'], 15)]

    def boom(argv, **k):
        raise subprocess.TimeoutExpired(argv, 15)
    monkeypatch.setattr(launcher.subprocess, 'run', boom)
    launcher.register_name(42, 'S-coder')      # no exception
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: (_ for _ in ()).throw(OSError('no agents-cli')))
    launcher.register_name(42, 'S-coder')


def test_pane_start_agy_coder_runs_agy_and_registers_its_name(monkeypatch, tmp_path):
    state = tmp_path / 'Nightshift-shift-agy00001'
    state.mkdir()
    (state / 'prompt.txt').write_text('THE TASK', encoding='utf-8')
    seen = {}

    class Child:
        pid = 77

        def wait(self):
            return 0
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setattr(launcher.subprocess, 'Popen',
                        lambda cmd, **k: seen.update(cmd=cmd, cwd=k['cwd'], env=k['env']) or Child())
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: seen.setdefault('runs', []).append(argv))
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda child, tool='claude': seen.update(trust=tool))
    monkeypatch.setattr(launcher, 'set_role_colour', lambda *a: pytest.fail('/color is Claude-only'))
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane('Coder', str(state), start=True, cwd=str(tmp_path), crew='gemini-coder')
    s = 'Nightshift-shift-agy00001'
    brief = launcher.role_brief('Coder', f'{s}-coder', f'{s}-foreman', str(state), 'coder-agy.md',
                                cwd=str(tmp_path))
    assert seen['cmd'] == launcher.agy_command('gemini-3.8-flash-high', brief, launcher.START_MESSAGES['Coder'])
    assert seen['cwd'] == str(tmp_path) and 'CLAUDECODE' not in seen['env'] and seen['env']['FORCE_COLOR'] == '3'
    assert seen['runs'] == [['agents-cli', 'name', '77', f'{s}-coder']] and seen['trust'] == 'agy'
    record = json.loads((state / 'Coder.json').read_text(encoding='utf-8'))
    assert record['tool'] == 'agy' and record['model'] == 'gemini-3.8-flash-high' and record['name'] == f'{s}-coder'


def test_gemini_coder_crew_selects_the_agy_briefs_and_fills_them():
    crew = launcher.load_crew('gemini-coder')
    assert [(r['role'], r['brief'], r['tool'], r['model'], r['colour']) for r in crew['roles']] == [
        ('Foreman', 'foreman-agy-coder.md', 'claude', 'claude-opus-5-5', 'red'),
        ('Coder', 'coder-agy.md', 'agy', 'gemini-3.8-flash-high', 'green')]
    foreman = launcher.role_brief('Foreman', 'S-foreman', 'S-coder', 'C:/x/S', 'foreman-agy-coder.md')
    coder = launcher.role_brief('Coder', 'S-coder', 'S-foreman', 'C:/x/S', 'coder-agy.md')
    for brief in (foreman, coder):
        assert not any(p in brief for p in ('{role}', '{identity}', '{peer_name}', '{state_dir}', '{shift}',
                                            '{nightshift}'))
        assert 'CODER-DONE' in brief and 'CODER-BLOCKED' in brief and 'never launch Claude sessions' in brief
    assert "agents-cli wait S-coder --match 'CODER-DONE|CODER-BLOCKED' --timeout 30m" in foreman
    assert 'agents-cli send S-coder' in foreman and 'agents-cli tail S-coder' in foreman
    assert foreman.rstrip().endswith('verbatim:')
    assert 'nightshift_shifts.py claim S S-coder' in coder and 'Exit code 4' in coder
    assert 'nightshift_shifts.py ack S S-coder <id>' in coder and 'agents-cli tail' in coder


def test_pid_alive_accepts_agy(monkeypatch):
    csv = '"agy.exe","900","Console","1","1 K"\r\n'
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda argv, **k: subprocess.CompletedProcess([], 0, stdout=csv.encode(), stderr=b''))
    assert launcher.pid_alive(900) is True


def test_shift_record_reports_each_role_tool(monkeypatch, tmp_path):
    d = _shift(tmp_path, 'Nightshift-shift-tool0001', {'Foreman': 1, 'Coder': 2})
    info = json.loads((d / 'Coder.json').read_text(encoding='utf-8'))
    (d / 'Coder.json').write_text(json.dumps(dict(info, tool='agy')), encoding='utf-8')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: True)
    roles = launcher.shift_record(d)['roles']
    assert roles['Coder']['tool'] == 'agy' and roles['Foreman']['tool'] == 'claude'


def test_start_dry_run_gemini_coder_shows_the_agy_coder(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([]))
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('Popen called'))
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', '--crew', 'gemini-coder', 'task'])
    plan = json.loads(capsys.readouterr().out)
    assert plan['crew'] == 'gemini-coder'
    coder = plan['roles']['Coder']
    assert (coder['tool'], coder['model'], coder['exe']) == ('agy', 'gemini-3.8-flash-high', str(launcher.AGY_EXE))
    assert 'tool' not in plan['roles']['Foreman'] and 'model' not in plan['roles']['Foreman']


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
    assert 'COPILOT_DISABLE_TERMINAL_TITLE' not in env and 'COPILOT_SETUP_TERMINAL' not in env


def test_crew_env_sets_copilot_title_and_setup_opt_outs_only_for_copilot():
    """Copilot retitles the pane and can block on the multi-line setup prompt; only that tool opts out."""
    base = {'PATH': 'p', 'TERM': 'dumb', 'NO_COLOR': '1'}
    copilot = launcher.crew_env(base, tool='copilot')
    assert copilot['COPILOT_DISABLE_TERMINAL_TITLE'] == '1'
    assert copilot['COPILOT_SETUP_TERMINAL'] == 'false'
    assert copilot['PATH'] == 'p' and 'NO_COLOR' not in copilot
    # A parent value must not leave the prompt enabled or the title opt-out off.
    forced = launcher.crew_env(
        {**base, 'COPILOT_DISABLE_TERMINAL_TITLE': '0', 'COPILOT_SETUP_TERMINAL': 'true'}, tool='copilot')
    assert forced['COPILOT_DISABLE_TERMINAL_TITLE'] == '1'
    assert forced['COPILOT_SETUP_TERMINAL'] == 'false'
    for kwargs in ({}, {'tool': 'claude'}, {'tool': 'codex'}, {'tool': 'agy'}):
        env = launcher.crew_env(dict(base), **kwargs)
        assert 'COPILOT_DISABLE_TERMINAL_TITLE' not in env
        assert 'COPILOT_SETUP_TERMINAL' not in env


def test_crew_env_disables_claude_prompt_suggestions_only_for_claude():
    """A pre-typed guessed prompt in a crew pane reads as an owner instruction; Claude panes opt out."""
    base = {'PATH': 'p', 'CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION': 'true'}
    assert launcher.crew_env(dict(base))['CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION'] == 'false'
    assert launcher.crew_env(dict(base), tool='claude', role='Coder')['CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION'] == 'false'
    for tool in ('codex', 'copilot', 'agy'):
        assert 'CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION' not in launcher.crew_env(dict(base), tool=tool)


# ── ticket -> prompt + folder ─────────────────────────────────────────────────

def _fake_run(outputs):
    """Return a subprocess.run stand-in that replays canned (argv-prefix -> result) answers."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        for prefix, result in outputs:
            if argv[:len(prefix)] == prefix:
                return result if callable(result) is False else result(argv)
        if argv[:1] == ['git'] and argv[3:] == ['rev-parse', '--show-toplevel']:
            return subprocess.CompletedProcess(argv, 128, b'', b'not a git repository')
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


def test_start_dry_run_default_crew_is_the_five_pane_golden(monkeypatch, tmp_path, capsys):
    """Golden for the default crew (Foreman, Advisor, Coder, Tester, Apprentice; NGH-024, NGH-027, NGH-029) in the
    2x4 container (slots 1..5 crew, slots 6..8 empty shells for spawn): must not move."""
    _fixed_uuid(monkeypatch)
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    launcher.start(['--dry-run', 'task'])
    plan = json.loads(capsys.readouterr().out)
    shift = 'Nightshift-shift-12345678'
    state = str(Path('C:/nightshift/shifts') / shift)
    cwd = str(work.resolve())
    py, script = sys.executable, str(Path(launcher.__file__).resolve())

    def pane_args(role, slot):
        return ['--profile', 'Nightshift Crew', '--title', f'{shift} shift', '--suppressApplicationTitle', '-d', cwd,
                py, script, '--pane', role, '--state-dir', state, '--start', '--cwd', cwd, '--slot', str(slot)]

    def empty_args(slot):
        return ['--profile', 'Nightshift Crew', '--title', f'{shift} shift', '--suppressApplicationTitle', '-d', cwd,
                'cmd.exe', '/k', py, script, '--slot', str(slot), '--state-dir', state]
    assert plan == {
        'dry_run': True, 'shift': shift, 'state_dir': state, 'cwd': cwd, 'ticket': None,
        'prompt_file': str(Path(state) / 'prompt.txt'), 'model': 'claude-opus-5-5', 'profile': 'Nightshift Crew',
        'permission_mode': 'auto', 'lock': str(launcher.lock_path(cwd)),
        'roles': {
            'Foreman': {'session_name': f'{shift}-foreman', 'pane_title': f'{shift} shift',
                        'peer': f'{shift}-advisor, {shift}-coder, {shift}-tester, {shift}-apprentice',
                        'receives_prompt': True,
                        'first_message': 'Begin the assignment: the task text is at the end of your role instructions, '
                                         'after "The human task for this shift follows, verbatim:".',
                        'compaction': {'automatic': 'permitted',
                                       'environment': {'DISABLE_AUTO_COMPACT': '0', 'DISABLE_COMPACT': '0'}},
                        'permission_flags': ['--permission-mode', 'auto']},
            'Advisor': {'session_name': f'{shift}-advisor', 'pane_title': f'{shift} shift', 'peer': f'{shift}-foreman',
                        'receives_prompt': False, 'first_message': launcher.START_MESSAGES['Advisor'],
                        'compaction': {'automatic': 'disabled', 'environment': {'DISABLE_AUTO_COMPACT': '1'}},
                        'model': 'claude-fable-5-1', 'permission_flags': ['--permission-mode', 'auto']},
            'Coder': {'session_name': f'{shift}-coder', 'pane_title': f'{shift} shift', 'peer': f'{shift}-foreman',
                      'receives_prompt': False,
                      'first_message': 'Begin your Coder role: send READY to Foreman and await assignments.',
                      'compaction': {'automatic': 'disabled', 'environment': {'DISABLE_AUTO_COMPACT': '1'}},
                      'permission_flags': ['--permission-mode', 'auto']},
            'Tester': {'session_name': f'{shift}-tester', 'pane_title': f'{shift} shift', 'peer': f'{shift}-foreman',
                       'receives_prompt': False,
                       'first_message': 'Begin your Tester role: send READY to Foreman and await verification '
                                        'assignments.',
                       'compaction': {'automatic': 'disabled', 'environment': {'DISABLE_AUTO_COMPACT': '1'}},
                       'permission_flags': ['--permission-mode', 'auto']},
            'Apprentice': {'session_name': f'{shift}-apprentice', 'pane_title': f'{shift} shift',
                           'peer': f'{shift}-foreman', 'receives_prompt': False,
                               'first_message': 'Begin your primary coding role: send READY to Foreman and await '
                                            'assignments.',
                           'tool': 'agy', 'model': 'gemini-3.8-flash-high', 'exe': str(launcher.AGY_EXE),
                           'compaction': {'automatic': 'unsupported', 'environment': {},
                                          'reason': 'No verified disable switch in installed Antigravity help or official CLI documentation.'},
                           'command': [str(launcher.AGY_EXE), '--model', 'gemini-3.8-flash-high',
                                       '--dangerously-skip-permissions', '-i', '<brief>\n\n<first message>'],
                           'permission_flags': ['--dangerously-skip-permissions']}},
        'slots': [{'slot': 1, 'role': 'Foreman', 'pane_title': f'{shift} shift'},
                  {'slot': 2, 'role': 'Advisor', 'pane_title': f'{shift} shift'},
                  {'slot': 3, 'role': 'Coder', 'pane_title': f'{shift} shift'},
                  {'slot': 4, 'role': 'Tester', 'pane_title': f'{shift} shift'},
                  {'slot': 5, 'role': 'Apprentice', 'pane_title': f'{shift} shift'},
                  {'slot': 6, 'role': None, 'pane_title': f'{shift} shift'},
                  {'slot': 7, 'role': None, 'pane_title': f'{shift} shift'},
                  {'slot': 8, 'role': None, 'pane_title': f'{shift} shift'}],
        'wt_argv': ['wt.exe', '-w', '-1', '--pos', '0,0', '--maximized', 'new-tab', *pane_args('Foreman', 1),
                    ';', 'split-pane', '-H', '-s', '0.5', *pane_args('Apprentice', 5),
                    ';', 'move-focus', 'up',
                    ';', 'split-pane', '-V', '-s', '0.75', *pane_args('Advisor', 2),
                    ';', 'split-pane', '-V', '-s', '0.6667', *pane_args('Coder', 3),
                    ';', 'split-pane', '-V', '-s', '0.5', *pane_args('Tester', 4),
                    ';', 'move-focus', 'down',
                    ';', 'split-pane', '-V', '-s', '0.75', *empty_args(6),
                    ';', 'split-pane', '-V', '-s', '0.6667', *empty_args(7),
                    ';', 'split-pane', '-V', '-s', '0.5', *empty_args(8),
                    ';', 'focus-pane', '-t', '0'],
        'prompt': 'task'}
    assert list(plan) == ['dry_run', 'shift', 'state_dir', 'cwd', 'ticket', 'prompt_file', 'model', 'profile',
                          'permission_mode', 'lock', 'roles', 'slots', 'wt_argv', 'prompt']   # key order too


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
    argvs = []
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: argvs.append(argv) or sent.append(k['input'])
                        or subprocess.CompletedProcess([], 0, b'{}', b''))
    monkeypatch.setattr(launcher.time, 'sleep', lambda s: None)
    assert launcher.inject_command(1, '/color red') is True and sent == [b'/color red']
    assert argvs == [['agents-cli', '_attach', 'inject', '1', '1']]   # the text goes on stdin, never in argv
    monkeypatch.setattr(launcher, 'peek_lines', lambda pid: ['❯ never empty'])
    assert launcher.inject_command(1, '/color red', tries=3) is False and sent == [b'/color red']


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
    launcher.down(['dead0001', '--close'])
    out = json.loads(capsys.readouterr().out)
    handover = (d / 'handover.md').read_text(encoding='utf-8')
    assert 'last words of 501' in handover and 'last words of 502' in handover and 'Ticket X: do it' in handover
    assert sorted(killed) == ['1501', '1502', '501', '502']          # both claude pids and both wrappers
    assert closed == [(777, 0x10)] and out['window_closed'] is True   # WM_CLOSE to that hwnd only
    assert 'Status: done' in handover and 'Summary: did the thing' in handover and '- a.py' in handover
    assert 'abc1234 First change' in handover and 'def5678 Second change' in handover
    assert 'teardown does not run tests' in handover
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
    assert '(no result.json)' in handover and '(no launch.json)' in handover and '## Test evidence' in handover


@pytest.mark.parametrize('ticket, windows, expected', [
    ('NGH-023', {10: 'NGH-023 shift', 11: 'NGH-099 shift', 12: 'other'}, 10),        # the shift title (NGH-028)
    (None, {13: 'Nightshift-shift-titl0001 shift', 14: 'NGH-023 shift'}, 13),        # free-text shift title
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
    launcher.down(['titl0001', '--close'])
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

def test_foreman_brief_carries_shift_contract_with_state_dir(tmp_path):
    brief = launcher.role_brief('Foreman', 'f', 'c', Path('C:/x/shift-1'), cwd=tmp_path)
    assert 'C:\\x\\shift-1\\result.json' in brief.replace('/', '\\') and '"done" or "blocked"' in brief
    assert 'test_nightshift_shifts.py' not in brief


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


def test_loop_emails_once_when_blocked(monkeypatch, tmp_path, notified):
    _loop_shift(monkeypatch, tmp_path, iter([{'status': 'blocked', 'summary': 'needs a key'}]))
    (tmp_path / 'shift-0').mkdir()
    (tmp_path / 'shift-0' / 'denials.json').write_text(
        json.dumps([{'time': 't', 'role': 'Coder', 'line': 'Bash denied'}]), encoding='utf-8')
    monkeypatch.setattr(launcher, 'start', lambda argv: tmp_path / 'shift-0')
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: _ok({}))
    with pytest.raises(SystemExit):
        launcher.loop(['NGH-051', '--notify'])
    assert len(notified) == 1
    subject, body = notified[0]
    assert subject == 'Nightshift: blocked NGH-051'
    assert 'needs a key' in body and str(tmp_path / 'shift-0' / 'handover.md') in body and 'Coder: Bash denied' in body


def test_loop_emails_once_after_retries_are_exhausted(monkeypatch, tmp_path, notified):
    _loop_shift(monkeypatch, tmp_path, iter([{'status': 'stalled', 'summary': 'idle'}] * 3))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: _ok({}))
    with pytest.raises(SystemExit):
        launcher.loop(['NGH-052', '--retries', '2', '--notify'])
    assert len(notified) == 1
    subject, body = notified[0]
    assert subject == 'Nightshift: stalled NGH-052'
    assert str(tmp_path / 'shift-2' / 'handover.md') in body and '(none)' in body


def test_loop_emails_once_at_the_end_of_a_done_batch(monkeypatch, tmp_path, notified):
    _loop_shift(monkeypatch, tmp_path, iter([{'status': 'done', 'stage': 'tested', 'summary': 'first'},
                                             {'status': 'done', 'stage': 'tested', 'summary': 'second'}]))
    events = []
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: events.append(len(notified)) or _ok({}))
    launcher.loop(['NGH-053', 'NGH-054', '--notify'])
    assert events == [0, 0]   # no notification between tickets
    assert len(notified) == 1
    subject, body = notified[0]
    assert subject == 'Nightshift: finished NGH-054'
    for ticket, summary, shift in (('NGH-053', 'first', 'shift-0'), ('NGH-054', 'second', 'shift-1')):
        assert ticket in body and f'done: {summary}' in body and str(tmp_path / shift / 'handover.md') in body


def test_loop_emails_when_the_folder_is_locked(monkeypatch, notified):
    monkeypatch.setattr(launcher, 'start',
                        lambda argv: (_ for _ in ()).throw(launcher.FolderLocked(3, 'folder C:/x is held')))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: _ok({}))
    with pytest.raises(SystemExit):
        launcher.loop(['NGH-055', '--notify'])
    assert len(notified) == 1
    subject, body = notified[0]
    assert subject == 'Nightshift: blocked NGH-055'
    assert 'no shift started' in body and 'folder C:/x is held' in body


@pytest.mark.parametrize('flags', [[], ['--no-notify'], ['--notify', '--no-notify']])
@pytest.mark.parametrize('status', ['blocked', 'done', 'stalled', 'died', 'short'])
def test_loop_default_and_no_notify_send_nothing(monkeypatch, tmp_path, notified, status, flags):
    _loop_shift(monkeypatch, tmp_path, iter([{'status': status, 'stage': 'tested', 'summary': 's'}]))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: _ok({}))
    if status == 'done':
        launcher.loop(['NGH-056', '--retries', '0'] + flags)
    else:
        with pytest.raises(SystemExit) as exc:
            launcher.loop(['NGH-056', '--retries', '0'] + flags)
        assert exc.value.code == 2
    assert notified == []


@pytest.mark.parametrize('flags', [[], ['--no-notify']])
def test_loop_folder_lock_is_silent_without_notify(monkeypatch, notified, flags):
    monkeypatch.setattr(launcher, 'start',
                        lambda argv: (_ for _ in ()).throw(launcher.FolderLocked(3, 'folder C:/x is held')))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: _ok({}))
    with pytest.raises(SystemExit) as exc:
        launcher.loop(['NGH-055'] + flags)
    assert exc.value.code == 2 and notified == []


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


@pytest.mark.parametrize('owner', ['coder-b', 'CODER-A'])
@pytest.mark.parametrize('existing', [False, True])
def test_claim_other_live_shift_conflict_is_all_or_nothing(monkeypatch, tmp_path, capsys, owner, existing):
    d = _claims_shift(monkeypatch, tmp_path)
    other = _shift(tmp_path, 'Nightshift-shift-other001', {'Foreman': 101})
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: pid == 101)
    held = launcher.normalise_path('C:/scripts/held.py')
    timestamp = '2026-10-15 01:02:03'
    launcher.save_claims(other, {held: {'role': owner, 'time': timestamp}})
    if existing:
        launcher.save_claims(d, {launcher.normalise_path('C:/scripts/own.py'):
                                 {'role': 'coder-a', 'time': 'old'}})
    before = (d / 'claims.json').read_bytes() if existing else None
    other_before = (other / 'claims.json').read_bytes()

    with pytest.raises(launcher.FileClaimed) as exc:
        launcher.claim(['clm00001', 'coder-a', 'C:/scripts/free.py', 'C:\\SCRIPTS\\held.py'])

    assert exc.value.code == 4
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == f'{held} is already claimed by {owner} (shift {other.name}) since {timestamp}\n'
    assert ((d / 'claims.json').read_bytes() if (d / 'claims.json').exists() else None) == before
    assert (other / 'claims.json').read_bytes() == other_before
    assert not (d / 'claims.lock').exists() and not list(d.glob('*.tmp'))


def test_claim_ignores_dead_shift_but_preserves_other_shift_claims(monkeypatch, tmp_path, capsys):
    d = _claims_shift(monkeypatch, tmp_path)
    dead = _shift(tmp_path, 'Nightshift-shift-dead0001', {'Foreman': 101})
    live = _shift(tmp_path, 'Nightshift-shift-live0001', {'Tester': 202})
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: pid == 202)
    held = launcher.normalise_path('C:/scripts/held.py')
    other_path = launcher.normalise_path('C:/scripts/other.py')
    launcher.save_claims(dead, {held: {'role': 'coder-b', 'time': 'old'}})
    launcher.save_claims(live, {other_path: {'role': 'coder-b', 'time': 'new'}})
    before = {p: (p / 'claims.json').read_bytes() for p in (dead, live)}

    launcher.claim(['clm00001', 'coder-a', 'C:/scripts/held.py', 'C:/scripts/free.py'])

    assert json.loads(capsys.readouterr().out)['claimed'] == [held, launcher.normalise_path('C:/scripts/free.py')]
    assert launcher.load_claims(d)[held]['role'] == 'coder-a'
    assert {p: (p / 'claims.json').read_bytes() for p in (dead, live)} == before


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
    """The command role_brief puts in place of {nightshift}: the suite Python and this repo's launcher, with forward
    slashes (Git Bash strips unquoted backslashes)."""
    return f'{launcher.SUITE_PYTHON} {(launcher.REPO_DIR / "nightshift_shifts.py").as_posix()}'


def test_role_brief_fills_shift_and_tells_crew_about_claims():
    coder = launcher.role_brief('Coder', 'S-coder', 'S-foreman', Path('C:/x/Nightshift-shift-abc'))
    foreman = launcher.role_brief('Foreman', 'S-foreman', 'S-coder', Path('C:/x/Nightshift-shift-abc'))
    ns = _nightshift_cmd()
    assert f'{ns} claim Nightshift-shift-abc S-coder' in coder
    assert f'{ns} release Nightshift-shift-abc S-coder' in coder
    assert f'{ns} claims Nightshift-shift-abc' in foreman and foreman.rstrip().endswith('verbatim:')
    assert '{nightshift}' not in coder and '{nightshift}' not in foreman
    assert '{shift}' not in launcher.role_brief('Coder', 'c', 'f')   # no state dir: filled with ''
    # c2 (NGH-027): no backslash in the launcher command, which Git Bash would strip when unquoted
    for brief in (coder, foreman, launcher.role_brief('Advisor', 'S-advisor', 'S-foreman',
                                                      Path('C:/x/Nightshift-shift-abc'), 'advisor.md')):
        assert ns in brief and '\\' not in ns



# ── Tester role in the default crew (NGH-024) ─────────────────────────────────

def test_tester_brief_fills_identity_and_states_the_report_format():
    tester = launcher.role_brief('Tester', 'S-tester', 'S-foreman', Path('C:/x/Nightshift-shift-abc'), 'tester.md')
    ns = _nightshift_cmd()
    assert 'S-tester' in tester and 'S-foreman' in tester
    assert 'TESTED-PASS' in tester and 'TESTED-FAIL' in tester and 'builds/' in tester
    assert f'{ns} build Nightshift-shift-abc --cwd' in tester
    assert f'{ns} msg send Nightshift-shift-abc S-tester Foreman' in tester
    assert f'{ns} claim Nightshift-shift-abc S-tester' in tester
    assert not any(p in tester for p in ('{role}', '{identity}', '{peer_name}', '{state_dir}', '{shift}', '{nightshift}'))


def test_pane_start_launches_the_default_tester_in_blue(monkeypatch, tmp_path):
    state = tmp_path / 'Nightshift-shift-test0001'
    state.mkdir()
    (state / 'prompt.txt').write_text('THE TASK', encoding='utf-8')
    seen = {}

    class Child:
        pid = 43

        def wait(self):
            return 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda cmd, **k: seen.setdefault('cmd', cmd) and Child())
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda child: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda child, colour, cwd: seen.update(colour=colour) or True)
    monkeypatch.setattr(launcher, 'inject_command', lambda *a, **kw: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane('Tester', str(state), start=True, cwd=str(tmp_path))
    cmd = seen['cmd']
    assert cmd[cmd.index('--name') + 1] == 'Nightshift-shift-test0001-tester' and seen['colour'] == 'blue'
    brief = cmd[cmd.index('--append-system-prompt') + 1]
    assert 'THE TASK' not in brief and 'TESTED-PASS' in brief and 'Nightshift-shift-test0001-foreman' in brief
    record = json.loads((state / 'Tester.json').read_text(encoding='utf-8'))
    assert record['peer'] == 'Nightshift-shift-test0001-foreman'


def test_shift_record_and_handover_list_the_tester(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    d = _shift(tmp_path, 'Nightshift-shift-trio0001', {'Foreman': 701, 'Coder': 702, 'Tester': 703})
    members = launcher.crew_members(launcher.load_crew(), d.name)
    (d / 'crew.json').write_text(json.dumps({'crew': 'default', 'members': members}), encoding='utf-8')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid: True)
    assert list(launcher.shift_record(d)['roles']) == ['Foreman', 'Coder', 'Tester']
    monkeypatch.setattr(launcher, 'pane_tail', lambda pid, lines=40: [f'last words of {pid}'])
    monkeypatch.setattr(launcher, 'suite_result', lambda: ('passed', '1 passed'))
    monkeypatch.setattr(launcher, 'shift_commits', lambda cwd, since: [])
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess([], 0, stdout=b'', stderr=b''))
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {})
    launcher.down([d.name])
    handover = (d / 'handover.md').read_text(encoding='utf-8')
    assert '## Tester (Nightshift-shift-trio0001-tester, pid 703, alive)' in handover and 'last words of 703' in handover

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


@pytest.mark.parametrize('flags, forwarded', [([], []), (['--no-notify'], []), (['--notify'], ['--notify']),
                                           (['--notify', '--no-notify'], [])])
def test_resume_clears_stale_locks_then_loops_in_ticket_order_with_continue_from(
        monkeypatch, tmp_path, capsys, flags, forwarded):
    newer, op = _resume_shifts(monkeypatch, tmp_path, live=(400,))
    calls = _resume_fakes(monkeypatch)
    live = _lock('C:/live', 'Nightshift-shift-live0003')
    dead = _lock('C:/dead', 'Nightshift-shift-op000001')
    launcher.resume(['--stall-minutes', '5'] + flags)
    assert json.loads(capsys.readouterr().out)['dry_run'] is False
    assert not dead.exists() and live.exists()
    assert calls == [(['NGH-010', 'OP-001', '--stall-minutes', '5'] + forwarded,
                      {'NGH-010': str(newer), 'OP-001': str(op)})]


@pytest.mark.parametrize('flags, expected', [([], 0), (['--no-notify'], 0), (['--notify'], 1)])
@pytest.mark.parametrize('status', ['done', 'blocked'])
def test_resume_notifications_are_opt_in(monkeypatch, tmp_path, notified, flags, expected, status):
    _resume_shifts(monkeypatch, tmp_path, live=(400,))
    _loop_shift(monkeypatch, tmp_path, iter([{'status': status, 'stage': 'tested', 'summary': 's'}] * 2))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: _ok({}))
    if status == 'done':
        launcher.resume(flags)
    else:
        with pytest.raises(SystemExit) as exc:
            launcher.resume(flags)
        assert exc.value.code == 2
    assert len(notified) == expected


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


def test_active_assignment_goes_stale_and_only_current_dispatch_result_clears_it(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A1', '--body', 'one')
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A2', '--body', 'two')
    _relaunch_coder(d)
    launcher.inbox(['mail0001', 'Foreman'])
    assert [(r['asg'], r['stale']) for r in json.loads(capsys.readouterr().out)['active_assignments']] == \
        [('A1', True), ('A2', True)]
    _send(capsys, 'Coder', 'Foreman', '--kind', 'result', '--asg', 'A1', '--body', 'done one')
    # A replacement cannot complete the previous incarnation's dispatch.
    assert [r['asg'] for r in launcher.inbox_view(d, 'Foreman')['active_assignments']] == ['A1', 'A2']
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A1', '--body', 'recover one')
    _send(capsys, 'Coder', 'Foreman', '--kind', 'result', '--asg', 'A1', '--body', 'done recovered one')
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
        mailbox_identity = me if me == 'Foreman' else 'S-coder'
        assert f'{ns} msg send Nightshift-shift-abc {mailbox_identity} {peer} --kind' in brief
        assert f'{ns} msg sent Nightshift-shift-abc <id>' in brief
        assert f'{ns} ack Nightshift-shift-abc {mailbox_identity} <id>' in brief
        assert f'{ns} inbox Nightshift-shift-abc {mailbox_identity}' in brief
        assert '{nightshift}' not in brief
        assert 'header line' in brief and 'never on the SendMessage text' in brief
        assert 'exit code 5 means do not act' in brief and 'resumed crew' in brief and 'outbound_unsent' in brief
    assert briefs['Foreman'].rstrip().endswith('verbatim:')
    assert 'send READY from S-coder' in briefs['Coder']
    assert f'{ns} claim Nightshift-shift-abc S-coder' in briefs['Coder']


@pytest.mark.parametrize('role,brief_file', [
    ('Coder', 'coder.md'), ('Coder', 'coder-cli.md'), ('Coder', 'coder-agy.md'),
    ('Tester', 'tester.md'), ('Tester', 'tester-cli.md'),
    ('Advisor', 'advisor.md'), ('Advisor', 'advisor-cli.md'),
])
def test_custom_member_brief_uses_own_identity_for_every_mailbox_command(role, brief_file):
    """A spawned label can borrow a role's brief without addressing that role's original member."""
    shift = 'Nightshift-shift-abc'
    identity = f'{shift}-helper-2'
    brief = launcher.role_brief(role, identity, f'{shift}-foreman',
                                Path('C:/state with spaces') / shift, brief_file)
    ns = _nightshift_cmd()
    commands = re.findall(rf'{re.escape(ns)} (msg send|ack|inbox) {shift} ([^\s`]+)', brief)
    assert commands, brief_file
    assert {command for command, _ in commands} >= {'msg send', 'ack'}
    if brief_file not in ('coder-agy.md', 'advisor-cli.md'):
        assert 'inbox' in {command for command, _ in commands}
    assert all(sender == identity for _, sender in commands), (brief_file, commands)
    assert f'{ns} msg send {shift} {identity} Foreman --kind' in brief
    assert '{identity}' not in brief


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


# ── 1x4 container (NGH-025): slots, empty slot shells, spawn, down --hold/--close ──────
# Nothing here launches wt.exe, a cmd shell, agents-cli or Claude: every call out is faked.

CONT_SHIFT = 'Nightshift-shift-cont0001'


@pytest.fixture
def _container_isolated(monkeypatch, tmp_path):
    """No test touches the real locks or shifts folders, and nothing reaches a real process."""
    monkeypatch.setattr(launcher, 'LOCKS_DIR', tmp_path / 'locks')
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    monkeypatch.setattr(launcher, 'plan_worktree', lambda *a: None)
    monkeypatch.setattr(launcher, 'notify_owner', lambda subject, body: {'sent': True, 'draft_id': 'fake'})
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('Popen called'))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: pytest.fail(f'unexpected subprocess: {argv}'))


def _cells(argv):
    """The wt.exe argv split at ';' into its sub-commands."""
    cells, cur = [], []
    for a in argv[6:]:
        if a == ';':
            cells.append(cur)
            cur = []
        else:
            cur.append(a)
    return cells + [cur]


PAIR = """\
name: pair
layout: 1x4
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true, colour: red}
  - {role: Coder, brief: coder.md}
"""


# ── load_crew: layouts and slot counts ────────────────────────────────────────

@pytest.mark.usefixtures('_container_isolated')
def test_default_crew_is_the_1x4_container():
    crew = launcher.load_crew()
    assert crew['layout'] == '2x4' and crew['slots'] == 8
    assert launcher.LAYOUT_SLOTS == {'split': None, '1x4': 4, '2x4': 8}


@pytest.mark.usefixtures('_container_isolated')
def test_split_layout_has_no_slots(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'flat', PAIR.replace('layout: 1x4', 'layout: split'))
    assert launcher.load_crew('flat')['slots'] is None
    _crew_file(monkeypatch, tmp_path, 'bare', PAIR.replace('layout: 1x4\n', ''))
    crew = launcher.load_crew('bare')
    assert crew['layout'] == 'split' and crew['slots'] is None


@pytest.mark.usefixtures('_container_isolated')
def test_1x4_refuses_more_panes_than_slots(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'big', PAIR.replace('{role: Coder, brief: coder.md}',
                                                          '{role: Coder, brief: coder.md, count: 4}'))
    with pytest.raises(SystemExit, match='5 panes do not fit layout 1x4 \\(4 slots\\)'):
        launcher.load_crew('big')
    _crew_file(monkeypatch, tmp_path, 'four', PAIR.replace('{role: Coder, brief: coder.md}',
                                                           '{role: Coder, brief: coder.md, count: 3}'))
    assert launcher.load_crew('four')['slots'] == 4   # exactly full is fine


@pytest.mark.usefixtures('_container_isolated')
def test_unknown_layout_names_the_supported_ones(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'odd', PAIR.replace('layout: 1x4', 'layout: 2x2'))
    with pytest.raises(SystemExit, match="'2x2' is not supported; use one of: split, 1x4, 2x4"):
        launcher.load_crew('odd')


# ── wt_command with slots ─────────────────────────────────────────────────────

def _slot_argv(members, ticket='NGH-025'):
    return launcher.wt_command(CONT_SHIFT, Path('C:/x/state'), agents=True, mode_flag='--start', cwd='C:/w',
                               members=members, ticket=ticket, slots=4)


@pytest.mark.usefixtures('_container_isolated')
def test_wt_three_members_fill_slots_1_to_3_and_slot_4_is_an_empty_shell():
    argv = _slot_argv(['Foreman', 'Coder', 'Tester'])
    assert _titles(argv) == ['NGH-025 shift'] * 4
    cells = _cells(argv)
    assert [c[0] for c in cells] == ['new-tab', 'split-pane', 'split-pane', 'split-pane', 'focus-pane']
    assert [c[c.index('-s') + 1] for c in cells[1:4]] == ['0.75', '0.6667', '0.5']   # four equal columns
    for n, cell in enumerate(cells[:3], 1):
        assert cell[cell.index('--slot') + 1] == str(n) and '--pane' in cell
    empty = cells[3]
    script = str(Path(launcher.__file__).resolve())
    i = empty.index('cmd.exe')
    assert empty[i:] == ['cmd.exe', '/k', sys.executable, script, '--slot', '4', '--state-dir', str(Path('C:/x/state'))]
    assert '--pane' not in empty and '--profile' in empty and '--suppressApplicationTitle' in empty


@pytest.mark.usefixtures('_container_isolated')
def test_wt_two_members_get_two_empty_slots_not_the_two_pane_layout():
    argv = _slot_argv(['Foreman', 'Coder'], ticket=None)
    assert _titles(argv) == [f'{CONT_SHIFT} shift'] * 4
    cells = _cells(argv)
    assert 'move-focus' not in argv and cells[-1][:3] == ['focus-pane', '-t', '0']
    assert [c[c.index('--slot') + 1] for c in cells[:4]] == ['1', '2', '3', '4']
    assert [('--pane' in c) for c in cells[:4]] == [True, True, False, False]


@pytest.mark.usefixtures('_container_isolated')
def test_wt_without_slots_has_no_slot_args():
    argv = launcher.wt_command(CONT_SHIFT, Path('C:/x'), agents=True, mode_flag='--start', cwd='C:/w',
                               members=['Foreman', 'Coder', 'Tester'])
    assert '--slot' not in argv and len(_titles(argv)) == 3


# ── start --dry-run shows the slots ───────────────────────────────────────────

@pytest.mark.usefixtures('_container_isolated')
def test_dry_run_default_crew_fills_five_slots_with_crew(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', 'task'])
    plan = json.loads(capsys.readouterr().out)
    s = 'Nightshift-shift-12345678'
    assert plan['slots'] == [{'slot': 1, 'role': 'Foreman', 'pane_title': f'{s} shift'},
                             {'slot': 2, 'role': 'Advisor', 'pane_title': f'{s} shift'},
                             {'slot': 3, 'role': 'Coder', 'pane_title': f'{s} shift'},
                             {'slot': 4, 'role': 'Tester', 'pane_title': f'{s} shift'},
                             {'slot': 5, 'role': 'Apprentice', 'pane_title': f'{s} shift'},
                             {'slot': 6, 'role': None, 'pane_title': f'{s} shift'},
                             {'slot': 7, 'role': None, 'pane_title': f'{s} shift'},
                             {'slot': 8, 'role': None, 'pane_title': f'{s} shift'}]
    slot_cells = [c for c in _cells(plan['wt_argv']) if '--slot' in c]
    assert [(c[c.index('--slot') + 1], c[c.index('--pane') + 1] if '--pane' in c else None) for c in slot_cells] == [
        ('1', 'Foreman'), ('5', 'Apprentice'), ('2', 'Advisor'), ('3', 'Coder'),
        ('4', 'Tester'), ('6', None), ('7', None), ('8', None)]
    assert plan['wt_argv'].count('cmd.exe') == 3   # three empty slot shells after the Apprentice in the bottom row
    assert not (tmp_path / 'shifts').exists()


@pytest.mark.usefixtures('_container_isolated')
def test_dry_run_two_role_crew_shows_two_crew_and_two_empty_slots(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    _crew_file(monkeypatch, tmp_path, 'pair', PAIR)
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', '--crew', 'pair', 'NGH-025-free-text'])
    plan = json.loads(capsys.readouterr().out)
    assert [(c['slot'], c['role']) for c in plan['slots']] == [(1, 'Foreman'), (2, 'Coder'), (3, None), (4, None)]
    assert _titles(plan['wt_argv'])[2:] == [c['pane_title'] for c in plan['slots'][2:]]
    assert plan['crew'] == 'pair'


@pytest.mark.usefixtures('_container_isolated')
def test_dry_run_split_crew_has_no_slots_key(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    _crew_file(monkeypatch, tmp_path, 'flat', PAIR.replace('layout: 1x4', 'layout: split'))
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', '--crew', 'flat', 'task'])
    assert 'slots' not in json.loads(capsys.readouterr().out)


# ── slot records ──────────────────────────────────────────────────────────────

@pytest.mark.usefixtures('_container_isolated')
def test_write_and_read_slot(tmp_path):
    assert launcher.read_slot(tmp_path, 1) is None
    launcher.write_slot(tmp_path, 1, cmd_pid=99)
    record = launcher.read_slot(tmp_path, 1)
    assert record['slot'] == 1 and record['cmd_pid'] == 99 and record['role'] is None and 'started' in record
    launcher.write_slot(tmp_path, 1, role='Advisor')
    assert launcher.read_slot(tmp_path, 1)['role'] == 'Advisor'
    (tmp_path / 'slots' / '2.json').write_text('{broken', encoding='utf-8')
    assert launcher.read_slot(tmp_path, 2) is None
    (tmp_path / 'slots' / '3.json').write_text('[1, 2]', encoding='utf-8')
    assert launcher.read_slot(tmp_path, 3) is None


@pytest.mark.usefixtures('_container_isolated')
def test_slot_shell_records_its_parent_cmd(tmp_path):
    launcher.slot_shell(4, str(tmp_path))
    record = launcher.read_slot(tmp_path, 4)
    assert record['cmd_pid'] == os.getppid() and record['role'] is None and record['slot'] == 4


@pytest.mark.usefixtures('_container_isolated')
def test_main_with_only_slot_runs_the_slot_shell(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(launcher, 'slot_shell', lambda slot, state_dir: seen.append((slot, state_dir)))
    monkeypatch.setattr(launcher, 'pane', lambda *a, **k: pytest.fail('pane called'))
    monkeypatch.setattr(sys, 'argv', ['nightshift_shifts.py', '--slot', '4', '--state-dir', str(tmp_path)])
    launcher.main()
    assert seen == [(4, str(tmp_path))]


@pytest.mark.usefixtures('_container_isolated')
def test_main_passes_slot_to_the_pane_wrapper(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(launcher, 'pane', lambda *a: seen.append(a))
    monkeypatch.setattr(launcher, 'slot_shell', lambda *a: pytest.fail('slot_shell called'))
    monkeypatch.setattr(sys, 'argv', ['nightshift_shifts.py', '--pane', 'Coder', '--state-dir', str(tmp_path),
                                      '--start', '--slot', '2'])
    launcher.main()
    assert seen and seen[0][0] == 'Coder' and seen[0][-1] == 2


# ── pane() reads crew.json (spawned members) ─────────────────────────────────

def _member(shift, role, label=None, human=False, colour='green', peer=None, tool='claude', model='claude-opus-5-5'):
    label = label or role
    return {'role': role, 'label': label, 'identity': f'{shift}-{label.lower()}', 'brief': f'{role.lower()}.md'
            if role in ('Foreman', 'Coder', 'Tester') else 'coder.md', 'colour': colour, 'human_contact': human,
            'tool': tool, 'model': model, 'peer': peer or f'{shift}-foreman'}


@pytest.mark.usefixtures('_container_isolated')
def test_pane_start_takes_a_spawned_member_from_crew_json(monkeypatch, tmp_path):
    state = tmp_path / CONT_SHIFT
    state.mkdir()
    (state / 'prompt.txt').write_text('THE TASK', encoding='utf-8')
    members = [_member(CONT_SHIFT, 'Foreman', human=True, colour='red', peer=f'{CONT_SHIFT}-coder'), _member(CONT_SHIFT, 'Coder'),
               _member(CONT_SHIFT, 'Advisor', colour='yellow', model='claude-sonnet-5-5')]
    (state / 'crew.json').write_text(json.dumps({'crew': 'default', 'members': members, 'slots': 4,
                                                 'permission_mode': 'plan'}), encoding='utf-8')
    seen = {}

    class Child:
        pid = 42

        def wait(self):
            return 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda cmd, **k: seen.setdefault('cmd', cmd) and Child())
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda child: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda child, colour, cwd: seen.update(colour=colour) or True)
    monkeypatch.setattr(launcher, 'inject_command', lambda *a, **kw: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    monkeypatch.setattr(launcher, 'load_crew', lambda *a, **k: pytest.fail('manifest read despite crew.json'))
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: True)
    launcher.write_slot(state, 3, cmd_pid=2503)   # spawned: typed into slot 3's shell, which it returns to
    launcher.pane('Advisor', str(state), start=True, cwd=str(tmp_path), slot=3)
    cmd = seen['cmd']
    assert cmd[cmd.index('--name') + 1] == f'{CONT_SHIFT}-advisor' and cmd[cmd.index('--model') + 1] == 'claude-sonnet-5-5'
    assert cmd[cmd.index('--permission-mode') + 1] == 'plan' and seen['colour'] == 'yellow'
    slot = launcher.read_slot(state, 3)
    assert slot['role'] == 'Advisor' and slot['identity'] == f'{CONT_SHIFT}-advisor' and slot['wrapper_pid'] == os.getpid()
    assert slot['cmd_pid'] == 2503
    with pytest.raises(SystemExit, match='not a member of crew default'):
        launcher.pane('Nobody', str(state), start=True, cwd=str(tmp_path))


# ── spawn ─────────────────────────────────────────────────────────────────────

def _container(tmp_path, members=('Foreman', 'Coder'), slots=4, hwnd=777, free=(3, 4), extra_slots=None):
    """A live 1x4 shift: crew.json, launch.json, a <Role>.json and slot record per member, empty slots in `free`."""
    d = tmp_path / 'shifts' / CONT_SHIFT
    d.mkdir(parents=True)
    crew = [_member(CONT_SHIFT, r, human=(r == 'Foreman'), colour='red' if r == 'Foreman' else 'green') for r in members]
    (d / 'crew.json').write_text(json.dumps({'crew': 'default', 'members': crew, 'slots': slots,
                                             'permission_mode': 'auto', 'model': 'claude-opus-5-5',
                                             'cwd': 'C:/git/proj'}), encoding='utf-8')
    (d / 'launch.json').write_text(json.dumps({'argv': [], 'hwnd': hwnd}), encoding='utf-8')
    for n, role in enumerate(members, 1):
        (d / f'{role}.json').write_text(json.dumps({'pid': 500 + n, 'wrapper_pid': 1500 + n, 'started': 1.0,
                                                    'name': f'{CONT_SHIFT}-{role.lower()}', 'cwd': 'C:/git/proj'}),
                                        encoding='utf-8')
        launcher.write_slot(d, n, role=role, cmd_pid=2500 + n)
    for n in free:
        launcher.write_slot(d, n, cmd_pid=2500 + n)
    for n, fields in (extra_slots or {}).items():
        launcher.write_slot(d, n, **fields)
    return d


@pytest.fixture
def injected(monkeypatch):
    """Fake the outside world for spawn: window 777 open, every cmd pid running, crew pids alive, inject ok."""
    class Calls(list):
        rc, out, err = 0, b'', b''
    calls = Calls()
    calls.inputs = []
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {777: 'NGH-025 Foreman'})
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: True)
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: True)

    def run(argv, **k):
        calls.append(argv)
        assert argv[:3] == ['agents-cli', '_attach', 'inject'], argv
        assert len(argv) == 5, 'inject accepts PID and enter only; command text belongs on stdin'
        assert isinstance(k['input'], bytes)
        calls.inputs.append(k['input'])
        return subprocess.CompletedProcess(argv, calls.rc, calls.out, calls.err)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    return calls


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_into_slot_3_of_a_two_role_crew(tmp_path, injected, capsys):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Tester', '--slot', '3'])
    out = json.loads(capsys.readouterr().out)
    assert out == {'shift': CONT_SHIFT, 'slot': 3, 'role': 'Tester', 'identity': f'{CONT_SHIFT}-tester', 'tool': 'claude'}
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    assert [m['label'] for m in crew['members']] == ['Foreman', 'Coder', 'Tester']
    tester = crew['members'][-1]
    assert tester['brief'] == 'tester.md' and tester['peer'] == f'{CONT_SHIFT}-foreman' and tester['human_contact'] is False
    assert tester['model'] == 'claude-opus-5-5' and crew['slots'] == 4 and crew['cwd'] == 'C:/git/proj'
    assert len(injected) == 1
    argv = injected[0]
    assert argv[3:5] == ['2503', '1']   # slot 3's cmd pid
    assert injected.inputs[0].decode('utf-8') == (
        f'"{sys.executable}" "{Path(launcher.__file__).resolve()}" --pane Tester --state-dir "{d}" '
        '--start --cwd "C:/git/proj" --slot 3')
    slot = launcher.read_slot(d, 3)
    assert slot['role'] == 'Tester' and slot['identity'] == f'{CONT_SHIFT}-tester' and slot['cmd_pid'] == 2503
    assert launcher.read_slot(d, 4)['role'] is None


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_takes_the_first_free_slot(tmp_path, injected, capsys):
    d = _container(tmp_path, members=('Foreman', 'Coder', 'Tester'), free=(4,))
    launcher.spawn([CONT_SHIFT, '--role', 'Helper'])
    out = json.loads(capsys.readouterr().out)
    assert out['slot'] == 4 and injected[0][3] == '2504'
    helper = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert helper['brief'] == 'coder.md' and helper['colour'] == 'yellow'   # not a manifest role, no helper.md
    assert helper['persona'] == 'coder.yaml'   # NGH-032: like the brief, no helper.yaml falls back to the Coder's


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_gemini_runs_agy(tmp_path, injected, capsys):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Coder2', '--cli', 'gemini'])
    assert json.loads(capsys.readouterr().out)['tool'] == 'agy'
    member = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert member['tool'] == 'agy' and member['model'] == launcher.AGY_MODEL
    assert member['brief'] == 'coder.md' and member['persona'] == 'coder.yaml'


@pytest.mark.usefixtures('_container_isolated')
@pytest.mark.parametrize('cli', ['opencode'])
def test_spawn_refuses_unwired_clis(tmp_path, injected, cli):
    d = _container(tmp_path)
    before = (d / 'crew.json').read_text(encoding='utf-8')
    with pytest.raises(SystemExit, match=f'--cli {cli} is not yet wired'):
        launcher.spawn([CONT_SHIFT, '--role', 'Helper', '--cli', cli])
    assert (d / 'crew.json').read_text(encoding='utf-8') == before and injected == []


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_refuses_when_the_window_is_gone(tmp_path, injected, monkeypatch):
    _container(tmp_path)
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {888: 'other'})
    with pytest.raises(SystemExit, match='window is gone'):
        launcher.spawn([CONT_SHIFT, '--role', 'Tester'])
    assert injected == []


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_refuses_without_launch_json(tmp_path, injected):
    d = _container(tmp_path)
    (d / 'launch.json').unlink()
    with pytest.raises(SystemExit, match='window is gone'):
        launcher.spawn([CONT_SHIFT, '--role', 'Tester'])


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_refuses_a_shift_without_a_slot_grid(tmp_path, injected):
    _container(tmp_path, slots=None)
    with pytest.raises(SystemExit, match='no slot grid'):
        launcher.spawn([CONT_SHIFT, '--role', 'Tester'])


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_refuses_when_no_slot_is_free(tmp_path, injected):
    _container(tmp_path, members=('Foreman', 'Coder'), free=(), extra_slots={3: {'role': 'Tester', 'cmd_pid': 2503},
                                                                         4: {'role': 'Advisor', 'cmd_pid': 2504}})
    with pytest.raises(SystemExit, match='no free slot in shift .* \\(4 slots, all taken\\)'):
        launcher.spawn([CONT_SHIFT, '--role', 'Helper'])
    assert injected == []


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_refuses_a_taken_or_out_of_range_slot(tmp_path, injected):
    _container(tmp_path)
    with pytest.raises(SystemExit, match='slot 2 is taken'):
        launcher.spawn([CONT_SHIFT, '--role', 'Tester', '--slot', '2'])
    with pytest.raises(SystemExit, match='slot 5 is not in 1..4'):
        launcher.spawn([CONT_SHIFT, '--role', 'Tester', '--slot', '5'])
    assert injected == []


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_skips_a_slot_whose_cmd_has_exited(tmp_path, injected, monkeypatch, capsys):
    _container(tmp_path)
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: pid != 2503)
    launcher.spawn([CONT_SHIFT, '--role', 'Tester'])
    assert json.loads(capsys.readouterr().out)['slot'] == 4


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_refuses_a_live_role_in_any_case(tmp_path, injected):
    _container(tmp_path)
    with pytest.raises(SystemExit, match='role Coder is already live .*pid 502'):
        launcher.spawn([CONT_SHIFT, '--role', 'coder'])
    assert injected == []


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_refuses_a_role_still_starting_in_a_slot(tmp_path, injected):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Tester'])   # slot 3; Tester.json not written yet
    with pytest.raises(SystemExit, match='already live .*starting in slot 3'):
        launcher.spawn([CONT_SHIFT, '--role', 'Tester'])
    assert len(injected) == 1 and not (d / 'Tester.json').exists()


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_restarts_a_gone_member_under_its_own_label(tmp_path, injected, monkeypatch, capsys):
    d = _container(tmp_path)
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: pid != 502)   # Coder has exited
    launcher.spawn([CONT_SHIFT, '--role', 'coder'])
    out = json.loads(capsys.readouterr().out)
    assert out['role'] == 'Coder' and out['slot'] == 2   # back into its own slot, ahead of empty slot 3
    assert injected[0][3] == '2502'
    labels =[m['label'] for m in json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members']]
    assert labels == ['Foreman', 'Coder']   # replaced, not duplicated


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_rejects_a_bad_role_name(tmp_path, injected):
    _container(tmp_path)
    with pytest.raises(SystemExit, match='must be letters, digits or _'):
        launcher.spawn([CONT_SHIFT, '--role', 'x; del *'])


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_inject_failure_leaves_crew_and_slot_as_they_were(tmp_path, injected):
    d = _container(tmp_path)
    before = (d / 'crew.json').read_text(encoding='utf-8')
    injected.rc, injected.err = 3, b'no console'
    with pytest.raises(SystemExit, match='could not type into slot 3 \\(cmd pid 2503\\): no console'):
        launcher.spawn([CONT_SHIFT, '--role', 'Tester'])
    assert (d / 'crew.json').read_text(encoding='utf-8') == before
    assert launcher.read_slot(d, 3)['role'] is None


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_non_default_crew_passes_crew_to_the_wrapper(tmp_path, injected):
    d = _container(tmp_path)
    saved = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    (d / 'crew.json').write_text(json.dumps({**saved, 'crew': 'pair'}), encoding='utf-8')
    launcher.spawn([CONT_SHIFT, '--role', 'Tester'])
    assert injected.inputs[0].decode('utf-8').endswith('--slot 3 --crew pair')


# ── slot reuse (NGH-028): dead members' slots, incarnation history, status ───

@pytest.mark.usefixtures('_container_isolated')
def test_spawn_takes_a_dead_members_slot_before_a_higher_empty_one(tmp_path, injected, monkeypatch, capsys):
    d = _container(tmp_path, members=('Foreman', 'Coder', 'Tester'), free=(4,))
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: pid != 502)   # Coder (slot 2) has exited
    launcher.spawn([CONT_SHIFT, '--role', 'Advisor'])
    assert json.loads(capsys.readouterr().out)['slot'] == 2 and injected[0][3] == '2502'
    assert launcher.read_slot(d, 2)['role'] == 'Advisor' and launcher.read_slot(d, 4)['role'] is None
    advisor = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert advisor['persona'] == 'advisor.yaml'


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_never_takes_a_live_members_slot(tmp_path, injected, capsys):
    _container(tmp_path, members=('Foreman', 'Coder', 'Tester'), free=(4,))
    for n in (1, 2, 3):
        with pytest.raises(SystemExit, match=f'slot {n} is taken'):
            launcher.spawn([CONT_SHIFT, '--role', 'Advisor', '--slot', str(n)])
    launcher.spawn([CONT_SHIFT, '--role', 'Advisor'])
    assert json.loads(capsys.readouterr().out)['slot'] == 4 and len(injected) == 1


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_accepts_an_explicit_dead_slot(tmp_path, injected, monkeypatch, capsys):
    _container(tmp_path, members=('Foreman', 'Coder', 'Tester'), free=(4,))
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: pid != 503)   # Tester (slot 3) has exited
    launcher.spawn([CONT_SHIFT, '--role', 'Advisor', '--slot', '3'])
    assert json.loads(capsys.readouterr().out)['slot'] == 3


@pytest.mark.usefixtures('_container_isolated')
def test_respawn_of_a_dead_role_prefers_its_own_slot_over_a_lower_dead_one(tmp_path, injected, monkeypatch, capsys):
    d = _container(tmp_path, members=('Foreman', 'Coder', 'Tester'), free=(4,))
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: pid not in (501, 503))   # Foreman, Tester gone
    launcher.spawn([CONT_SHIFT, '--role', 'Tester'])
    assert json.loads(capsys.readouterr().out)['slot'] == 3
    # Tester.json is the previous incarnation's until the new wrapper writes: slot 3 is starting, not dead
    assert launcher.slot_state(d, 3) == 'starting'
    with pytest.raises(SystemExit, match='already live .*starting in slot 3'):
        launcher.spawn([CONT_SHIFT, '--role', 'Tester'])
    assert len(injected) == 1


def _run_pane(monkeypatch, state, role, slot, calls):
    """pane() in start mode with the child exiting at once; extra subprocess.run calls are recorded."""
    class Child:
        pid = 42

        def wait(self):
            return 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda cmd, **k: Child())
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda child: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda child, colour, cwd: True)
    monkeypatch.setattr(launcher, 'inject_command', lambda *a, **kw: True)
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('a container pane must not wait for Enter'))
    launcher.pane(role, str(state), start=True, cwd=str(state), slot=slot)


@pytest.mark.usefixtures('_container_isolated')
def test_role_json_keeps_the_history_of_earlier_incarnations(tmp_path, monkeypatch):
    d = _container(tmp_path)
    (d / 'prompt.txt').write_text('task', encoding='utf-8')
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: True)
    first = json.loads((d / 'Coder.json').read_text(encoding='utf-8'))
    _run_pane(monkeypatch, d, 'Coder', 2, [])
    second = json.loads((d / 'Coder.json').read_text(encoding='utf-8'))
    assert second['history'] == [first] and second['started'] > first['started'] and second['pid'] == 42
    _run_pane(monkeypatch, d, 'Coder', 2, [])
    third = json.loads((d / 'Coder.json').read_text(encoding='utf-8'))
    assert third['history'] == [first, {k: v for k, v in second.items() if k != 'history'}]
    assert launcher.incarnation(d, 'Coder') == third['started']   # mailbox fencing follows the current one


@pytest.mark.usefixtures('_container_isolated')
def test_container_pane_returns_to_its_slot_shell_when_the_member_exits(tmp_path, monkeypatch):
    d = _container(tmp_path)
    (d / 'prompt.txt').write_text('task', encoding='utf-8')
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: True)
    calls = []
    _run_pane(monkeypatch, d, 'Coder', 2, calls)   # slot 2 has a running cmd_pid: back to that prompt
    assert calls == []


@pytest.mark.usefixtures('_container_isolated')
def test_wt_launched_container_pane_becomes_a_slot_shell_when_the_member_exits(tmp_path, monkeypatch):
    d = _container(tmp_path)
    (d / 'prompt.txt').write_text('task', encoding='utf-8')
    launcher.write_slot(d, 2, role='Coder', identity=f'{CONT_SHIFT}-coder')   # wt ran the wrapper: no cmd_pid
    calls = []
    _run_pane(monkeypatch, d, 'Coder', 2, calls)
    assert calls == [['cmd.exe', '/k', sys.executable, str(Path(launcher.__file__).resolve()),
                      '--slot', '2', '--state-dir', str(d)]]
    launcher.slot_shell(2, str(d))   # what that cmd.exe runs: the dead role stays in the slot record
    record = launcher.read_slot(d, 2)
    assert record['role'] == 'Coder' and record['cmd_pid'] == os.getppid()


@pytest.mark.usefixtures('_container_isolated')
def test_status_lists_slot_states(tmp_path, monkeypatch):
    d = _container(tmp_path, members=('Foreman', 'Coder'), slots=5, free=(4, 5),
                   extra_slots={3: {'role': 'Tester', 'cmd_pid': 2503, 'spawned': 2.0}})
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: pid != 502)   # Coder has exited
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: pid != 2505)   # slot 5's cmd closed
    record = launcher.shift_record(d)
    assert record['slots'] == [
        {'slot': 1, 'role': 'Foreman', 'state': 'live', 'reusable': False},
        {'slot': 2, 'role': 'Coder', 'state': 'dead', 'reusable': True},
        {'slot': 3, 'role': 'Tester', 'state': 'starting', 'reusable': False},
        {'slot': 4, 'role': None, 'state': 'empty', 'reusable': True},
        {'slot': 5, 'role': None, 'state': 'gone', 'reusable': False}]


@pytest.mark.usefixtures('_container_isolated')
def test_status_of_a_split_shift_has_no_slots(tmp_path, monkeypatch):
    d = _container(tmp_path, slots=None, free=())
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: True)
    assert 'slots' not in launcher.shift_record(d)


# ── down: --hold / --close and kill ordering ─────────────────────────────────

@pytest.fixture
def down_world(monkeypatch, tmp_path):
    """A 1x4 shift with Foreman/Coder/Tester live; records taskkill, release, print and WM_CLOSE in order."""
    d = _container(tmp_path, members=('Foreman', 'Coder', 'Tester'), free=(4,))
    (d / 'prompt.txt').write_text('Ticket NGH-025: container', encoding='utf-8')
    events = []
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: True)
    monkeypatch.setattr(launcher, 'pane_tail', lambda pid, lines=40: ['tail'])
    monkeypatch.setattr(launcher, 'suite_result', lambda *a, **k: ('passed', '1 passed'))
    monkeypatch.setattr(launcher, 'shift_commits', lambda *a, **k: [], raising=False)
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {777: 'NGH-025 Foreman'})
    monkeypatch.setattr(launcher, 'own_ancestors', lambda: {os.getpid(), 1501, 9999})   # we run in Foreman's wrapper

    def run(argv, **k):
        if argv[0] == 'taskkill':
            events.append(('kill', int(argv[-1])))
        return subprocess.CompletedProcess(argv, 0, b'', b'')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    real_release = launcher.release_lock
    monkeypatch.setattr(launcher, 'release_lock', lambda name: events.append(('release',)) or real_release(name))
    monkeypatch.setattr(launcher, 'print', lambda *a, **k: events.append(('print', a[0])), raising=False)
    monkeypatch.setattr(launcher.ctypes.windll.user32, 'PostMessageW',
                        lambda h, m, w, l: events.append(('close', h.value, m)))
    return d, events


@pytest.mark.usefixtures('_container_isolated')
def test_down_explicit_hold_kills_its_own_tree_last(down_world):
    d, events = down_world
    launcher.down([CONT_SHIFT, '--hold'])
    kinds = [e[0] for e in events]
    assert 'close' not in kinds
    printed = json.loads(next(e[1] for e in events if e[0] == 'print'))
    assert printed['held'] is True and printed['window_closed'] is False
    assert sorted(printed['stopped']) == ['Coder', 'Foreman', 'Tester']
    first_kills = events[:kinds.index('release')]
    assert sorted(p for _, p in first_kills) == [502, 503, 1502, 1503]   # Coder and Tester, claude + wrapper
    assert events[kinds.index('release') + 1][0] == 'print'
    assert events[kinds.index('print') + 1:] == [('kill', 501), ('kill', 1501)]   # Foreman's tree, after the output
    assert (d / 'handover.md').exists()


@pytest.mark.usefixtures('_container_isolated')
@pytest.mark.parametrize('flags', [[], ['--close']])
def test_down_default_and_explicit_close_post_wm_close_after_output_before_own_kill(down_world, flags):
    _, events = down_world
    launcher.down([CONT_SHIFT, *flags])
    kinds = [e[0] for e in events]
    printed = json.loads(events[kinds.index('print')][1])
    assert printed['held'] is False and printed['window_closed'] is True
    assert kinds.index('release') < kinds.index('print') < kinds.index('close') < kinds.index('kill', kinds.index('print'))
    assert ('close', 777, 0x10) in events
    assert events[-2:] == [('kill', 501), ('kill', 1501)]


@pytest.mark.usefixtures('_container_isolated')
def test_down_from_outside_the_crew_kills_everyone_before_output(down_world, monkeypatch):
    _, events = down_world
    monkeypatch.setattr(launcher, 'own_ancestors', lambda: {os.getpid()})
    launcher.down([CONT_SHIFT, '--hold'])
    kinds = [e[0] for e in events]
    assert kinds[-1] == 'print' and kinds.count('kill') == 6 and kinds.index('release') > 5


@pytest.mark.usefixtures('_container_isolated')
def test_down_close_with_window_already_gone(down_world, monkeypatch):
    _, events = down_world
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {})
    launcher.down([CONT_SHIFT, '--close'])
    printed = json.loads(next(e[1] for e in events if e[0] == 'print'))
    assert printed['window_closed'] is False and printed['held'] is False
    assert 'close' not in [e[0] for e in events]


@pytest.mark.usefixtures('_container_isolated')
def test_down_hold_and_close_are_exclusive(down_world):
    with pytest.raises(SystemExit):
        launcher.down([CONT_SHIFT, '--hold', '--close'])


@pytest.mark.usefixtures('_container_isolated')
def test_loop_ends_each_shift_with_down_close(monkeypatch, tmp_path):
    d = tmp_path / 'shifts' / 'shift-NGH-040'
    d.mkdir(parents=True)
    downs = []
    monkeypatch.setattr(launcher.ctypes.windll.kernel32, 'SetConsoleTitleW', lambda t: None)
    monkeypatch.setattr(launcher, 'close_terminal_windows', lambda prefix: None)
    monkeypatch.setattr(launcher, 'start', lambda argv: d)
    monkeypatch.setattr(launcher, 'wait_for_result', lambda state_dir, stall, **kw: {'status': 'done', 'stage': 'tested'})
    monkeypatch.setattr(launcher, 'down', lambda argv: downs.append(argv))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess([], 0, b'', b''))
    launcher.loop(['NGH-040', '--no-notify'])
    assert downs == [['shift-NGH-040', '--close']]


# ── own_ancestors ─────────────────────────────────────────────────────────────

@pytest.mark.usefixtures('_container_isolated')
def test_own_ancestors_walks_parents_and_stops_on_a_cycle(monkeypatch):
    me = os.getpid()
    monkeypatch.setattr(launcher, 'parent_pids', lambda: {me: 20, 20: 30, 30: 20, 40: me})
    assert launcher.own_ancestors() == {me, 20, 30}
    monkeypatch.setattr(launcher, 'parent_pids', lambda: {})
    assert launcher.own_ancestors() == {me}


@pytest.mark.usefixtures('_container_isolated')
def test_parent_pids_sees_this_process_and_its_parent():
    parents = launcher.parent_pids()   # read-only Toolhelp snapshot
    assert parents.get(os.getpid()) == os.getppid()


# ── Advisor role in the default crew (NGH-027) ────────────────────────────────

_PLACEHOLDERS = ('{role}', '{identity}', '{peer_name}', '{state_dir}', '{shift}', '{nightshift}')


def _default_member(label, shift='S'):
    return next(m for m in launcher.crew_members(launcher.load_crew(), shift) if m['label'] == label)


def test_advisor_brief_fills_identity_and_states_its_answer_format():
    state = Path('C:/x/Nightshift-shift-abc')
    advisor = launcher.role_brief('Advisor', 'S-advisor', 'S-foreman', state, 'advisor.md')
    assert 'S-advisor' in advisor and 'S-foreman' in advisor and 'Nightshift-shift-abc' in advisor
    assert not any(p in advisor for p in _PLACEHOLDERS)
    assert (state / 'bus.jsonl').as_posix() in advisor and 'mailbox' in advisor
    assert 'DECISION:' in advisor and 'WHY:' in advisor and 'ASK-HUMAN:' in advisor
    assert 'S-advisor Foreman --kind result' in advisor
    assert re.search(r'never edits? (any )?files', advisor, re.IGNORECASE)
    assert 'never launch Claude sessions' in advisor   # common.md still leads


def test_foreman_brief_names_the_advisor():
    foreman = launcher.role_brief('Foreman', 'S-foreman', 'S-advisor, S-coder, S-tester',
                                  Path('C:/x/Nightshift-shift-abc'))
    assert 'Advisor' in foreman and 'adv-' in foreman
    assert 'Foreman Advisor --kind assignment' in foreman
    assert foreman.rstrip().endswith('verbatim:')


def test_advisor_opening_is_dormant_with_no_ready():
    advisor = _default_member('Advisor')
    opening = launcher.opening_message(advisor)
    assert opening == launcher.START_MESSAGES['Advisor']
    # no instruction to send READY (a "do not send READY" is the point of a dormant opening)
    assert not re.search(r'(?<!not )send READY', opening, re.IGNORECASE)
    assert re.search(r'wait', opening, re.IGNORECASE) and 'Foreman' in opening
    assert opening !='Begin your Advisor role: send READY to Foreman and await assignments.'


def test_pane_start_launches_the_default_advisor_on_its_own_model_in_purple(monkeypatch, tmp_path):
    state = tmp_path / 'Nightshift-shift-advi0001'
    state.mkdir()
    (state / 'prompt.txt').write_text('THE TASK', encoding='utf-8')
    seen = {}

    class Child:
        pid = 44

        def wait(self):
            return 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda cmd, **k: seen.setdefault('cmd', cmd) and Child())
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda child: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda child, colour, cwd: seen.update(colour=colour) or True)
    monkeypatch.setattr(launcher, 'inject_command', lambda *a, **kw: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane('Advisor', str(state), start=True, cwd=str(tmp_path))
    cmd = seen['cmd']
    assert cmd[cmd.index('--name') + 1] == 'Nightshift-shift-advi0001-advisor'
    assert cmd[cmd.index('--model') + 1] == 'claude-fable-5-1' and seen['colour'] == 'purple'
    brief = cmd[cmd.index('--append-system-prompt') + 1]
    assert 'THE TASK' not in brief and 'ASK-HUMAN:' in brief


# ── msg send: the Advisor talks only to the human contact ────────────────────

def _advisor_shift(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'shifts')
    name = 'Nightshift-shift-advm0001'
    d = tmp_path / 'shifts' / name
    d.mkdir(parents=True)
    members = launcher.crew_members(launcher.load_crew(), name)
    (d / 'crew.json').write_text(json.dumps({'crew': 'default', 'members': members}), encoding='utf-8')
    return d


@pytest.mark.parametrize('sender, recipient', [('Coder', 'Advisor'), ('Advisor', 'Tester'), ('Tester', 'Advisor'),
                                               ('Advisor', 'Coder')])
def test_msg_send_refuses_advisor_traffic_without_the_human_contact(monkeypatch, tmp_path, capsys, sender, recipient):
    d = _advisor_shift(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        launcher.msg(['send', d.name, sender, recipient, '--kind', 'note', '--body', 'psst'])
    assert capsys.readouterr().out == ''
    assert not (d / 'mailbox.sqlite3').exists() or _rows(d) == []


def test_msg_send_stores_foreman_advisor_traffic_both_ways(monkeypatch, tmp_path, capsys):
    d = _advisor_shift(monkeypatch, tmp_path)
    launcher.msg(['send', d.name, 'Foreman', 'Advisor', '--kind', 'assignment', '--asg', 'adv-1', '--body', 'which?'])
    launcher.msg(['send', d.name, f'{d.name}-advisor', 'foreman', '--kind', 'result', '--asg', 'adv-1',
                  '--body', 'DECISION: A\nWHY: cheaper'])
    assert capsys.readouterr().out == 'msg:1 kind:assignment asg:adv-1\nmsg:2 kind:result asg:adv-1\n'
    with pytest.raises(SystemExit):
        launcher.msg(['send', d.name, 'Coder', 'Advisor', '--kind', 'note', '--body', 'psst'])
    assert [(r['sender'], r['recipient'], r['kind']) for r in _rows(d)] == [('Foreman', 'Advisor', 'assignment'),
                                                                          ('Advisor', 'Foreman', 'result')]
    # the rest of the crew is unaffected by the guard
    launcher.msg(['send', d.name, 'Coder', 'Foreman', '--kind', 'note', '--body', 'READY'])
    assert len(_rows(d)) == 3


# ── spawn of a manifest role absent from crew.json ───────────────────────────

@pytest.mark.usefixtures('_container_isolated')
def test_spawn_of_a_manifest_role_takes_its_brief_colour_and_model(tmp_path, injected, capsys):
    d = _container(tmp_path)   # crew 'default', but crew.json holds only Foreman and Coder
    launcher.spawn([CONT_SHIFT, '--role', 'Advisor'])
    assert json.loads(capsys.readouterr().out)['slot'] == 3
    advisor = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert (advisor['brief'], advisor['colour'], advisor['tool'], advisor['model']) == \
        ('advisor.md', 'purple', 'claude', 'claude-fable-5-1')
    assert advisor['peer'] == f'{CONT_SHIFT}-foreman' and advisor['human_contact'] is False
    launcher.spawn([CONT_SHIFT, '--role', 'Tester'])
    tester = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert (tester['brief'], tester['colour'], tester['model']) == ('tester.md', 'blue', 'claude-opus-5-5')


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_of_a_manifest_role_on_another_tool_keeps_brief_and_colour_not_model(tmp_path, injected, capsys):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Advisor', '--cli', 'gemini'])
    advisor = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert (advisor['brief'], advisor['colour'], advisor['tool'], advisor['model']) == \
        ('advisor.md', 'purple', 'agy', launcher.AGY_MODEL)


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_of_a_role_not_in_the_shifts_manifest_is_unchanged(monkeypatch, tmp_path, injected, capsys):
    _crew_file(monkeypatch, tmp_path, 'pair', PAIR)
    d = _container(tmp_path)
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    (d / 'crew.json').write_text(json.dumps({**crew, 'crew': 'pair'}), encoding='utf-8')
    launcher.spawn([CONT_SHIFT, '--role', 'Advisor'])
    advisor = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert (advisor['colour'], advisor['model']) == ('yellow', 'claude-opus-5-5')   # pair has no Advisor


# ── start --dry-run names a Claude role's model only when it is not the crew model (adv-1) ──

MODELS = """\
name: models
model: claude-opus-5-5
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true}
  - {role: Advisor, brief: advisor.md, model: claude-fable-5-1}
  - {role: Coder, brief: coder.md, model: claude-opus-5-5}
  - {role: Tester, brief: tester.md, tool: agy, model: gemini-3.8-flash-high}
"""


def test_dry_run_shows_model_only_for_claude_roles_off_the_crew_model(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    _crew_file(monkeypatch, tmp_path, 'models', MODELS)
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', '--crew', 'models', 'task'])
    roles = json.loads(capsys.readouterr().out)['roles']
    assert 'model' not in roles['Foreman'] and 'tool' not in roles['Foreman']   # crew model by default
    assert 'model' not in roles['Coder'] and 'tool' not in roles['Coder']       # crew model named explicitly
    assert roles['Advisor']['model'] == 'claude-fable-5-1' and 'tool' not in roles['Advisor'] \
        and 'exe' not in roles['Advisor']
    tester = roles['Tester']   # a non-Claude role keeps its tool, model and exe
    assert (tester['tool'], tester['model'], tester['exe']) == ('agy', 'gemini-3.8-flash-high', str(launcher.AGY_EXE))


def test_dry_run_default_crew_names_only_the_advisors_model(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', 'task'])
    roles = json.loads(capsys.readouterr().out)['roles']
    assert {label: r.get('model') for label, r in roles.items()} == {
        'Foreman': None, 'Advisor': 'claude-fable-5-1', 'Coder': None, 'Tester': None,
        'Apprentice': 'gemini-3.8-flash-high'}   # a non-Claude role always names its model


# ── respawn attribute preservation, down --close handover safety, inbox incarnation ──

@pytest.mark.usefixtures('_container_isolated')
def test_respawn_preserves_existing_member_model_brief_colour_and_tool(tmp_path, injected, monkeypatch, capsys):
    d = _container(tmp_path, members=('Foreman', 'Advisor', 'Coder'), slots=4, free=(4,))
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    crew['members'][1].update(brief='advisor.md', colour='purple', tool='claude', model='claude-fable-5-1')
    crew['members'][2].update(brief='coder-agy.md', colour='green', tool='agy', model='gemini-3.8-flash-high')
    (d / 'crew.json').write_text(json.dumps(crew), encoding='utf-8')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: pid not in (502, 503))
    launcher.spawn([CONT_SHIFT, '--role', 'advisor'])
    launcher.spawn([CONT_SHIFT, '--role', 'coder'])
    after = {m['label']: m for m in json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members']}
    assert (after['Advisor']['role'], after['Advisor']['brief'], after['Advisor']['colour'],
            after['Advisor']['tool'], after['Advisor']['model']) == \
        ('Advisor', 'advisor.md', 'purple', 'claude', 'claude-fable-5-1')
    assert (after['Coder']['role'], after['Coder']['brief'], after['Coder']['colour'],
            after['Coder']['tool'], after['Coder']['model']) == \
        ('Coder', 'coder-agy.md', 'green', 'agy', 'gemini-3.8-flash-high')


@pytest.mark.usefixtures('_container_isolated')
def test_down_close_after_down_hold_preserves_handover_from_when_crew_was_live(down_world, monkeypatch):
    d, _ = down_world
    launcher.down([CONT_SHIFT, '--hold'])
    saved = (d / 'handover.md').read_text(encoding='utf-8')
    assert 'tail' in saved and '(pane not readable)' not in saved
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: False)
    monkeypatch.setattr(launcher, 'pane_tail', lambda pid, lines=40: [])
    launcher.down([CONT_SHIFT, '--close'])
    assert (d / 'handover.md').read_text(encoding='utf-8') == saved


def test_inbox_excludes_stale_messages_from_an_earlier_incarnation(monkeypatch, tmp_path, capsys):
    d = _advisor_shift(monkeypatch, tmp_path)
    (d / 'Coder.json').write_text(json.dumps({'pid': 10, 'started': 100.0}), encoding='utf-8')
    launcher.msg(['send', d.name, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'asg-old', '--body', 'first'])
    (d / 'Coder.json').write_text(json.dumps({'pid': 20, 'started': 200.0}), encoding='utf-8')
    launcher.msg(['send', d.name, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'asg-new', '--body', 'second'])
    capsys.readouterr()
    launcher.inbox([d.name, 'Coder'])
    view = json.loads(capsys.readouterr().out)
    assert [m['asg'] for m in view['inbound_unacked']] == ['asg-new']


# ── Codex & Copilot crews (Foreman, Advisor, Coder, Tester, Apprentice in 2x4) ──

def test_codex_and_codex_copilot_manifests_parse_five_roles_in_2x4():
    codex = launcher.load_crew('codex')
    assert codex['layout'] == '2x4' and codex['slots'] == 8 and codex['model'] == 'gpt-6.1-sol'
    assert [(r['role'], r['tool'], r['model'], r.get('reasoning')) for r in codex['roles']] == [
        ('Foreman', 'codex', 'gpt-6.1-sol', 'high'),
        ('Advisor', 'codex', 'gpt-6-astra', 'high'),
        ('Coder', 'codex', 'gpt-6-astra', 'high'),
        ('Tester', 'codex', 'gpt-6-astra', 'high'),
        ('Apprentice', 'codex', 'gpt-5.6-luna', 'high')]   # only Foreman on Sol (owner, 16 Oct)
    copilot = launcher.load_crew('codex-copilot')
    for crew, coder_model in ((codex, 'gpt-6-astra'), (copilot, 'gpt-6.1-sol')):
        advisor = next(r for r in crew['roles'] if r['role'] == 'Advisor')
        coder = next(r for r in crew['roles'] if r['role'] == 'Coder')
        assert (advisor['model'], advisor['reasoning'], coder['model']) == ('gpt-6-astra', 'high', coder_model)
    assert copilot['roles'][-1]['tool'] == 'copilot' and copilot['roles'][-1]['model'] == 'gemini-3.8-flash'


def test_codex_and_copilot_commands_and_account_env():
    cmd = launcher.codex_command('gpt-5.6-luna', 'BRIEF', 'FIRST', reasoning='xhigh')
    assert cmd == [str(launcher.CODEX_EXE), '--dangerously-bypass-approvals-and-sandbox', '--no-daemon',
                   '-c', 'tui.terminal_title=[]',
                   '-c', 'tui.status_line=["thread-title","model-with-reasoning","context-remaining"]',
                   '--model', 'gpt-5.6-luna',
                   '-c', 'model_reasoning_effort=xhigh', 'BRIEF\n\nFIRST']
    cop = launcher.copilot_command('gemini-3.8-flash-high', 'BRIEF', 'FIRST')
    assert cop == [str(launcher.COPILOT_EXE), '--allow-all', '--no-auto-update', '--model', 'gemini-3.8-flash-high',
                   '-i', 'BRIEF\n\nFIRST']
    env = launcher.crew_env({'PATH': 'x', 'TERM': 'dumb'}, tool='codex', account=2)
    assert 'TERM' not in env and env['CODEX_HOME'].endswith('.codex-second')


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_codex_and_copilot_use_their_default_models(tmp_path, injected, capsys):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--cli', 'codex'])
    assert json.loads(capsys.readouterr().out)['tool'] == 'codex'
    launcher.spawn([CONT_SHIFT, '--role', 'Helper', '--cli', 'copilot'])
    assert json.loads(capsys.readouterr().out)['tool'] == 'copilot'
    members = {m['label']: m for m in json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members']}
    assert (members['Apprentice']['tool'], members['Apprentice']['model']) == ('codex', 'gpt-6.1-sol')
    assert (members['Helper']['tool'], members['Helper']['model']) == ('copilot', 'gemini-3.8-flash')


# ── Codex role identity, colours and supplied ticket headers (NGH-033) ──────

@pytest.mark.parametrize('permissions, bypass', [('default', False), ('auto', True), ('skip', True)])
def test_codex_footer_is_ordered_and_preserves_role_permissions(permissions, bypass):
    command = launcher.codex_command('gpt-6.1-sol', 'brief', 'first', reasoning='xhigh', permissions=permissions)
    configs = [command[i + 1] for i, arg in enumerate(command) if arg == '-c']
    status = next(c.split('=', 1)[1] for c in configs if c.startswith('tui.status_line='))
    assert json.loads(status) == ['thread-title', 'model-with-reasoning', 'context-remaining']
    assert 'tui.terminal_title=[]' in configs and 'model_reasoning_effort=xhigh' in configs
    assert not any(c.startswith('tui.theme=') for c in configs)
    assert ('--dangerously-bypass-approvals-and-sandbox' in command) is bypass
    assert command[-1] == 'brief\n\nfirst'


def test_role_schemes_preserve_user_settings_and_campbell_palette(monkeypatch, tmp_path):
    settings = tmp_path / 'Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json'
    settings.parent.mkdir(parents=True)
    campbell = {'name': 'Campbell', 'background': '#101010', 'foreground': '#DDDDDD', 'red': '#990000'}
    unrelated = {'name': 'Personal', 'background': '#123456'}
    original = {'profiles': {'defaults': {'opacity': 91}, 'list': [{'guid': 'personal', 'name': 'Mine'}]},
                'schemes': [campbell, unrelated, {'name': 'Nightshift red', 'background': '#BADBAD'}],
                'keybindings': [{'command': 'copy', 'keys': 'ctrl+c'}]}
    before = json.dumps(original)
    settings.write_text(before, encoding='utf-8')
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    state = tmp_path / 'state'
    state.mkdir()
    launcher.install_profile(state)
    config = json.loads(settings.read_text(encoding='utf-8'))
    assert (state / 'terminal-settings-before.json').read_text(encoding='utf-8') == before
    assert config['profiles']['defaults'] == original['profiles']['defaults']
    assert config['profiles']['list'][0] == original['profiles']['list'][0]
    assert config['keybindings'] == original['keybindings']
    assert set(launcher.ROLE_BACKGROUNDS) == {'red', 'purple', 'green', 'blue', 'yellow'}
    assert len(set(launcher.ROLE_BACKGROUNDS.values())) == 5
    schemes = {s['name']: s for s in config['schemes']}
    assert schemes['Campbell'] == campbell and schemes['Personal'] == unrelated
    for colour, background in launcher.ROLE_BACKGROUNDS.items():
        role = schemes['Nightshift ' + colour]
        assert role['background'] == background
        assert role['foreground'] == campbell['foreground'] and role['red'] == campbell['red']
        assert all(k in role for k in ('black', 'brightWhite', 'cursorColor', 'selectionBackground'))
    launcher.install_profile(state)
    assert json.loads(settings.read_text(encoding='utf-8')) == config


@pytest.mark.parametrize('crew', ['codex', 'default', 'apprentice-codex', 'codex-copilot', 'apprentice-gemini'])
def test_start_selects_role_schemes_only_for_codex_members_and_no_empty_slots(monkeypatch, tmp_path, capsys, crew):
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('unexpected launch'))
    launcher.start(['--dry-run', '--no-worktree', '--cwd', str(tmp_path), '--crew', crew, 'task'])
    plan = json.loads(capsys.readouterr().out)
    members = {m['label']: m for m in launcher.crew_members(launcher.load_crew(crew), plan['shift'])}
    assert 'codex_colours' in launcher.wt_command.__code__.co_varnames
    for cell in _cells(plan['wt_argv']):
        if '--slot' not in cell:
            continue
        member = members.get(cell[cell.index('--pane') + 1]) if '--pane' in cell else None
        # Codex and Copilot start cells take a role scheme; Claude, agy and empty slots stay untinted.
        if member and member['tool'] in ('codex', 'copilot'):
            assert cell[cell.index('--colorScheme') + 1] == 'Nightshift ' + member['colour']
        else:
            assert '--colorScheme' not in cell
    for member in members.values():
        command = plan['roles'][member['label']].get('command')
        if member['tool'] == 'copilot':
            assert command[command.index('-i') - 2:command.index('-i')] == [
                '--name', f"{member['label']} ({member['identity']})"]
        elif command:
            assert '--name' not in command


def test_wt_role_colour_mapping_uses_labels_and_does_not_tint_trial_mode():
    kwargs = dict(agents=True, members=['Foreman', 'Coder-2'], codex_colours={'Coder-2': 'purple'}, slots=4)
    start = launcher.wt_command('S', Path('C:/state'), mode_flag='--start', **kwargs)
    cells = [c for c in _cells(start) if '--pane' in c]
    assert '--colorScheme' not in cells[0]
    assert cells[1][cells[1].index('--colorScheme') + 1] == 'Nightshift purple'
    trial = launcher.wt_command('S', Path('C:/state'), mode_flag='--live', **kwargs)
    assert '--colorScheme' not in trial


@pytest.mark.parametrize('from_file', [False, True])
@pytest.mark.parametrize('prompt, ticket', [
    ('Ticket NGH-033: supplied task\nBody with café', 'NGH-033'),
    ('Ticket OP-123: supplied task', 'OP-123'),
    ('Task\nTicket NGH-033: later header', None),
    ('\nTicket NGH-033: leading blank', None),
    (' Ticket NGH-033: indented', None),
    ('ticket NGH-033: lowercase', None),
    ('Ticket ngh-033: lowercase id', None),
    ('Ticket NGH-33: id allowed by TICKET_RE', 'NGH-33'),
    ('Ticket NGH-033 task', None),
    ('Ticket NGH-033x: invalid suffix', None),
    ('plain task', None),
])
def test_supplied_first_line_ticket_controls_title_without_fetching_task(monkeypatch, tmp_path, capsys,
                                                                        from_file, prompt, ticket):
    cwd = _no_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, 'ticket_prompt', lambda *a: pytest.fail('supplied prompt fetched a task'))
    if from_file:
        path = tmp_path / 'task.txt'
        path.write_text(prompt, encoding='utf-8')
        args = ['--prompt-file', str(path)]
    else:
        args = [prompt]
    launcher.start(['--dry-run', '--cwd', cwd, *args])
    plan = json.loads(capsys.readouterr().out)
    expected_prompt = prompt.replace('\r\n', '\n') if from_file else prompt
    assert plan['ticket'] == ticket and plan['prompt'] == expected_prompt
    expected = (ticket or plan['shift']) + ' shift'
    assert set(_titles(plan['wt_argv'])) == {expected}
    state = launcher.start(['--cwd', cwd, *args])
    assert (state / 'prompt.txt').read_text(encoding='utf-8') == prompt.replace('\r\n', '\n')
    if ticket:
        assert (state / 'ticket.txt').read_text(encoding='utf-8').strip() == ticket
        assert _bus(state)[0]['ticket'] == ticket
    else:
        assert not (state / 'ticket.txt').exists()


@pytest.mark.parametrize('from_file', [False, True])
def test_crlf_first_line_ticket_is_extracted_without_changing_supplied_task(monkeypatch, tmp_path, capsys,
                                                                          from_file):
    prompt = 'Ticket NGH-033: task\r\nMore text'
    cwd = _no_launch(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, 'ticket_prompt', lambda *a: pytest.fail('supplied prompt fetched a task'))
    if from_file:
        path = tmp_path / 'task.txt'
        path.write_bytes(prompt.encode('utf-8'))
        args = ['--prompt-file', str(path)]
    else:
        args = [prompt]
    launcher.start(['--dry-run', '--cwd', cwd, *args])
    plan = json.loads(capsys.readouterr().out)
    assert plan['ticket'] == 'NGH-033'
    assert plan['prompt'] == (prompt.replace('\r\n', '\n') if from_file else prompt)
    assert set(_titles(plan['wt_argv'])) == {'NGH-033 shift'}


def test_ticket_is_derived_before_continuation_handover(monkeypatch, tmp_path, capsys):
    cwd = _no_launch(monkeypatch, tmp_path)
    previous = tmp_path / 'previous'
    previous.mkdir()
    (previous / 'handover.md').write_text('Ticket OP-999: older work', encoding='utf-8')
    launcher.start(['--dry-run', '--cwd', cwd, '--continue-from', str(previous), 'task without a ticket'])
    plan = json.loads(capsys.readouterr().out)
    assert plan['ticket'] is None and 'Ticket OP-999:' in plan['prompt']
    launcher.start(['--dry-run', '--cwd', cwd, '--continue-from', str(previous), 'Ticket NGH-033: new work'])
    assert json.loads(capsys.readouterr().out)['ticket'] == 'NGH-033'


def _codex_composer(prompt='› Ask Codex to do anything', footer='GPT-6.1-Sol high · 90% left', busy=True):
    return (['Working (10s • esc to interrupt)', '', ''] if busy else ['']) + [prompt, '', '  ' + footer]


@pytest.mark.parametrize('footer', [
    'GPT-6.1-Sol high · C:\\git\\proj · 90% left',
    '01a143cf-596c-7c22-85bc-9a516b197913 · GPT-6.1-Sol high · 90% left',
    '01a143cf…', '01a143cf...', '01a143cf',
    'Fix flaky account tests…',
    'Tester (Nightshift-shift-e18f053a-tester) · GPT-6.1-Sol high · 90% left',
])
@pytest.mark.parametrize('busy', [False, True])
@pytest.mark.parametrize('prompt', ['›', '› Ask Codex to do anything'])
def test_codex_empty_composer_accepts_busy_and_unnamed_clipped_footer(footer, busy, prompt):
    lines = _codex_composer(prompt, footer, busy) + ['', ' ']
    assert launcher.codex_prompt_is_empty(lines)


@pytest.mark.parametrize('lines', [
    [], None, '›', [None], ['›', '', 1],
    _codex_composer('› user draft'),
    _codex_composer('› Ask Codex to do anything plus draft'),
    _codex_composer('› Try a different suggested action'),
    ['›', 'wrapped draft', '', '  GPT-6.1-Sol high'],
    ['› Ask Codex to do anything', '', '› real current draft', '', '  GPT-6.1-Sol high'],
    ['›', '', '', '', '', '', '', '', '  GPT-6.1-Sol high'],
    ['›', '', 'arbitrary output'],
    ['output', '›', '  GPT-6.1-Sol high'],
    ['Do you trust this folder?', '›', '', '  GPT-6.1-Sol high'],
    ['Select an option', '›', '', '  01a143cf…'],
    ['1. Allow once', '›', '', '  01a143cf…'],
    ['Press Enter to confirm', '›', '', '  GPT-6.1-Sol high'],
    ['❯', '', '  GPT-6.1-Sol high'],
])
def test_codex_composer_rejects_drafts_historical_prompts_dialogs_and_unreadable_input(lines):
    assert not launcher.codex_prompt_is_empty(lines)


def _rename_world(monkeypatch, frames=None, poll=None):
    frames = iter(frames if frames is not None else [_codex_composer()] * 2)
    seen = {'peeks': [], 'sent': [], 'sleeps': []}
    child = types.SimpleNamespace(pid=4321, poll=poll or (lambda: None))
    monkeypatch.setattr(launcher.time, 'monotonic', lambda: 0.0)
    monkeypatch.setattr(launcher.time, 'sleep', seen['sleeps'].append)

    def peek(pid, timeout=10):
        seen['peeks'].append((pid, timeout))
        return next(frames)

    def run(argv, **kw):
        seen['sent'].append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    monkeypatch.setattr(launcher, 'peek_lines', peek)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    return child, seen


def test_codex_rename_reads_current_empty_composer_twice_and_sends_once(monkeypatch):
    child, seen = _rename_world(monkeypatch)
    assert launcher.set_codex_role_name(child, 'Coder-2', 'Nightshift-S-coder-2')
    assert len(seen['peeks']) == 2 and not seen['sleeps']
    assert seen['sent'] == [(['agents-cli', '_attach', 'inject', '4321', '1'],
                            {'input': '/rename Coder-2 (Nightshift-S-coder-2)'.encode('utf-8'),
                             'capture_output': True, 'timeout': 10})]


def test_codex_rename_can_wait_for_first_readable_composer(monkeypatch):
    child, seen = _rename_world(monkeypatch, [[], _codex_composer(), _codex_composer()])
    assert launcher.set_codex_role_name(child, 'Tester', 'S-tester')
    assert len(seen['peeks']) == 3 and len(seen['sent']) == 1 and seen['sleeps'] == [0.5]


def test_codex_rename_stops_after_requested_attempt_count(monkeypatch):
    child, seen = _rename_world(monkeypatch, [[], [], []])
    assert not launcher.set_codex_role_name(child, 'Tester', 'S-tester', tries=3)
    assert len(seen['peeks']) == 3 and not seen['sent']


@pytest.mark.parametrize('frames', [
    [_codex_composer(), _codex_composer('› draft arrived')],
    [[], [], []],
    [_codex_composer('› busy with draft')] * 3,
])
def test_codex_rename_never_injects_into_draft_or_changed_second_read(monkeypatch, frames):
    child, seen = _rename_world(monkeypatch, frames)
    assert not launcher.set_codex_role_name(child, 'Tester', 'S-tester', tries=1)
    assert not seen['sent']


@pytest.mark.parametrize('polls', [[0], [None, 0], [None, None, 0]])
def test_codex_rename_checks_child_before_each_read_and_before_injection(monkeypatch, polls):
    values = iter(polls)
    child, seen = _rename_world(monkeypatch, poll=lambda: next(values))
    assert not launcher.set_codex_role_name(child, 'Tester', 'S-tester')
    assert not seen['sent'] and len(seen['peeks']) == len(polls) - 1


@pytest.mark.parametrize('failure', ['nonzero', 'timeout', 'oserror'])
def test_codex_rename_never_retries_uncertain_injection(monkeypatch, failure):
    child, seen = _rename_world(monkeypatch)

    def run(argv, **kw):
        seen['sent'].append(argv)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, kw['timeout'])
        if failure == 'oserror':
            raise OSError('injection unavailable')
        return subprocess.CompletedProcess(argv, 1, b'', b'failed')

    monkeypatch.setattr(launcher.subprocess, 'run', run)
    assert not launcher.set_codex_role_name(child, 'Tester', 'S-tester')
    assert len(seen['sent']) == 1 and len(seen['peeks']) == 2


def test_codex_rename_deadline_includes_peek_time_and_bounds_io(monkeypatch):
    child, seen = _rename_world(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(launcher.time, 'monotonic', lambda: clock[0])

    def slow_peek(pid, timeout=10):
        seen['peeks'].append(timeout)
        clock[0] += timeout
        return []

    monkeypatch.setattr(launcher, 'peek_lines', slow_peek)
    monkeypatch.setattr(launcher.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert not launcher.set_codex_role_name(child, 'Tester', 'S-tester', tries=100)
    assert clock[0] == 30 and seen['peeks'] == [10, 10, 9.0] and not seen['sent']


@pytest.mark.parametrize('failure', [OSError('peek unavailable'), subprocess.TimeoutExpired('peek', 1)])
def test_codex_rename_stops_safely_on_peek_error(monkeypatch, failure):
    child, seen = _rename_world(monkeypatch)

    def peek(*a, **k):
        raise failure

    monkeypatch.setattr(launcher, 'peek_lines', peek)
    assert not launcher.set_codex_role_name(child, 'Tester', 'S-tester')
    assert not seen['sent']


@pytest.mark.parametrize('rc, payload', [(1, b'{"lines":["unsafe"]}'), (0, b'not json'),
                                       (0, b'[]'), (0, b'{"error":"unavailable"}')])
def test_peek_lines_rejects_failed_or_invalid_console_reads(monkeypatch, rc, payload):
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda *a, **k: subprocess.CompletedProcess([], rc, payload, b''))
    assert launcher.peek_lines(4321, timeout=0.1) == []


@pytest.mark.parametrize('failure', [None, 'get', 'set', 'oserror', 'redirected'])
def test_pane_background_enables_vt_and_emits_osc_only_with_valid_console(monkeypatch, capsys, failure):
    seen = []

    def get_mode(handle, mode):
        if failure == 'oserror':
            raise OSError('no console')
        mode._obj.value = 2
        return failure != 'get'

    def set_mode(handle, mode):
        seen.append((handle, mode))
        return failure != 'set'

    kernel = types.SimpleNamespace(GetStdHandle=lambda n: 1234, GetConsoleMode=get_mode, SetConsoleMode=set_mode)
    monkeypatch.setattr(launcher.ctypes.windll, 'kernel32', kernel)
    monkeypatch.setattr(launcher, 'sys', types.SimpleNamespace(stdout=types.SimpleNamespace(
        isatty=lambda: failure != 'redirected')))
    assert launcher.set_pane_background('#172339') is (failure is None)
    assert capsys.readouterr().out == ('\033]11;#172339\033\\' if failure is None else '')
    if failure is None:
        assert seen == [(1234, 6)]


@pytest.mark.usefixtures('_container_isolated')
@pytest.mark.parametrize('tool', ['codex', 'claude', 'copilot'])
@pytest.mark.parametrize('failure', [None, 'popen', 'wait'])
def test_pane_role_background_and_reset_survive_exit_and_launch_errors(monkeypatch, tmp_path, tool, failure):
    d = _container(tmp_path)
    saved = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    saved['members'][1].update(tool=tool, colour='blue', model='test-model')
    (d / 'crew.json').write_text(json.dumps(saved), encoding='utf-8')
    events = []

    class Child:
        pid = 42

        def wait(self):
            events.append(('wait',))
            if failure == 'wait':
                raise OSError('wait failed')
            return 0

    def popen(command, **kw):
        events.append(('popen', command))
        if failure == 'popen':
            raise OSError('launch failed')
        return Child()

    monkeypatch.setattr(launcher.subprocess, 'Popen', popen)
    monkeypatch.setattr(launcher, 'set_pane_background', lambda colour: events.append(('background', colour)))
    monkeypatch.setattr(launcher, 'register_name', lambda *a: events.append(('register',)))
    monkeypatch.setattr(launcher, 'set_codex_role_name', lambda *a: events.append(('rename', a[1], a[2])))
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: events.append(('trust',)))
    monkeypatch.setattr(launcher, 'set_role_colour', lambda *a: events.append(('colour', a[1])) or True)
    monkeypatch.setattr(launcher, 'inject_command', lambda *a, **kw: events.append(('opening', a[1])) or True)
    monkeypatch.setattr(launcher, 'process_running', lambda *a: True)
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('reused slot should return'))
    if failure:
        with pytest.raises(OSError):
            launcher.pane('Coder', str(d), start=True, cwd=str(tmp_path), slot=2)
    else:
        launcher.pane('Coder', str(d), start=True, cwd=str(tmp_path), slot=2)
    backgrounds = [e[1] for e in events if e[0] == 'background']
    # Copilot gets the same OSC 11 tint and Campbell restore as Codex, including when launch or wait fails.
    assert backgrounds == (
        [launcher.ROLE_BACKGROUNDS['blue'], '#0C0C0C'] if tool in ('codex', 'copilot') else ['#0C0C0C'])
    assert events[0][0] == 'background' and events[1][0] == 'popen'
    if tool == 'codex' and failure != 'popen':
        assert ('rename', 'Coder', f'{CONT_SHIFT}-coder') in events
        assert events[-1] == ('background', '#0C0C0C')
    else:
        assert not any(e[0] == 'rename' for e in events)
    if tool == 'copilot':
        assert events[-1] == ('background', '#0C0C0C')
        assert not any('/rename' in str(arg) for e in events if e[0] == 'popen' for arg in e[1])
    if tool == 'claude' and failure != 'popen':
        assert ('trust',) in events and ('colour', 'blue') in events
        command = events[1][1]
        assert not any('/theme' in arg or '/rename' in arg for arg in command)


# ── Apprentice (NGH-029): slot 5, per-CLI manifests, spawn --crew ────────────

@pytest.mark.parametrize('name, tool, model, account', [
    ('apprentice-gemini', 'agy', 'gemini-3.8-flash-high', None),
    ('apprentice-codex', 'codex', 'gpt-5.6-luna', 2),
    ('apprentice-copilot', 'copilot', 'gemini-3.8-flash', None)])
def test_apprentice_manifests_are_the_default_crew_plus_their_apprentice(name, tool, model, account):
    crew = launcher.load_crew(name)
    default = launcher.load_crew()
    assert crew['layout'] == '2x4' and crew['slots'] == 8 and crew['model'] == 'claude-opus-5-5'
    assert crew['roles'][:4] == default['roles'][:4]   # the same Claude Foreman, Advisor, Coder, Tester
    apprentice = crew['roles'][4]
    assert (apprentice['role'], apprentice['brief'], apprentice['colour'], apprentice['tool'], apprentice['model'],
            apprentice.get('account')) == ('Apprentice', 'apprentice.md', 'yellow', tool, model, account)
    assert len(crew['roles']) == 5


def test_default_crew_leaves_three_empty_slots():
    crew = launcher.load_crew()
    assert crew['slots'] - sum(r['count'] for r in crew['roles']) == 3


def test_apprentice_brief_fills_every_placeholder_and_relays_the_verdict(tmp_path):
    brief = launcher.role_brief('Apprentice', 'S-apprentice', 'S-foreman', tmp_path / 'S',
                                'apprentice.md', cwd=tmp_path)
    assert 'S-apprentice' in brief and 'S-foreman' in brief and ' S ' in brief
    assert launcher.SUITE_PYTHON in brief and '{' not in brief and '}' not in brief
    assert 'TESTED-FAIL' in brief and brief.rstrip().endswith('on its own line.')
    assert 'APPRENTICE-DONE' in brief and 'APPRENTICE-BLOCKED' in brief


def test_apprentice_opening_message_asks_for_ready():
    member = {'role': 'Apprentice', 'human_contact': False}
    assert launcher.opening_message(member) == \
        'Begin your primary coding role: send READY to Foreman and await assignments.'


@pytest.mark.parametrize('brief_file', ['foreman.md', 'foreman-cli.md'])
def test_foreman_brief_routes_ordinary_work_to_apprentice_and_reserves_senior(brief_file):
    brief = launcher.role_brief('Foreman', 'S-foreman', 'S-coder', 'C:/x/S', brief_file)
    assert 'Apprentice' in brief and 'ordinary' in brief and 'first' in brief
    for term in ('cross-cutting', 'concurrency', 'security', 'second Tester failure',
                 'APPRENTICE-NEEDS-SENIOR', 'GUIDANCE', 'TAKEOVER', '900', 'same-asg'):
        assert term in brief
    assert 'fallback' in brief and 'no Apprentice' in brief
    assert 'assign every nit, typo or missing-test fix to Coder' not in brief
    assert 'Delegate every repository change to Coder' not in brief


def test_foreman_brief_splits_work_between_coder_and_apprentice():
    brief = launcher.role_brief('Foreman', 'S-foreman', 'S-coder', 'C:/x/S', 'foreman.md')
    assert 'Apprentice' in brief and "disjoint from Coder's claims" in brief
    assert "agents-cli wait <identity> --match '<ROLE>-DONE|<ROLE>-BLOCKED' --timeout 30m" in brief


def test_foreman_brief_drives_every_non_claude_member_through_mailbox_and_agents_cli():
    brief = launcher.role_brief('Foreman', 'S-foreman', 'S-coder', 'C:/x/S', 'foreman.md')
    para = brief.split('Non-Claude members:', 1)[1].split('\n\n', 1)[0]
    assert 'is not `claude`' in para and 'C:/x/S/crew.json' in para and 'whatever its role' in para
    steps = ['msg send', 'agents-cli send <identity>', 'msg sent', 'agents-cli wait <identity>',
             'agents-cli tail <identity>', 'ack']
    positions = [para.index(step) for step in steps]
    assert positions == sorted(positions)
    assert 'CODER-, TESTER- or APPRENTICE-' in para and 'TESTED-PASS' in para and 'TESTED-FAIL' in para
    assert 'Non-Claude members:' not in (launcher.BRIEFS_DIR / 'foreman-cli.md').read_text(encoding='utf-8')


def test_foreman_brief_records_copilot_credit_usage_before_result():
    brief = launcher.role_brief('Foreman', 'S-foreman', 'S-coder', 'C:/x/S', 'foreman.md')
    contract = brief.split('Shift contract:', 1)[1]
    assert 'C:/x/S/<label>-usage.json' in contract and '/usage' in contract
    assert ('"usage": {"budget": <shift max_credits>, "total_used": <sum>, "panes": '
            '{"<label>": {"credits_used": <n>, "cap": <its share>}}}') in contract
    assert 'C:/x/S/crew.json' in contract
    assert contract.index('"usage"') < contract.index('down {shift}'.replace('{shift}', 'S'))


@pytest.mark.usefixtures('_container_isolated')
def test_wt_five_members_fill_slots_1_to_5_of_the_2x4_container():
    argv = launcher.wt_command(CONT_SHIFT, Path('C:/x/state'), agents=True, mode_flag='--start', cwd='C:/w',
                               members=['Foreman', 'Advisor', 'Coder', 'Tester', 'Apprentice'], slots=8)
    slot_cells = [c for c in _cells(argv) if '--slot' in c]
    assert sorted((int(c[c.index('--slot') + 1]), c[c.index('--pane') + 1] if '--pane' in c else None)
                  for c in slot_cells) == [(1, 'Foreman'), (2, 'Advisor'), (3, 'Coder'), (4, 'Tester'),
                                           (5, 'Apprentice'), (6, None), (7, None), (8, None)]
    assert argv.count('cmd.exe') == 3


@pytest.mark.usefixtures('_container_isolated')
@pytest.mark.parametrize('crew, tool, model, account', [
    ('apprentice-codex', 'codex', 'gpt-5.6-luna', 2),
    ('apprentice-copilot', 'copilot', 'gemini-3.8-flash', None),
    ('apprentice-gemini', 'agy', 'gemini-3.8-flash-high', None)])
def test_spawn_crew_takes_the_role_from_the_named_manifest(tmp_path, injected, capsys, crew, tool, model, account):
    d = _container(tmp_path)   # a default shift whose crew.json has only Foreman and Coder
    launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--crew', crew])
    out = json.loads(capsys.readouterr().out)
    assert out['tool'] == tool and out['role'] == 'Apprentice' and out['slot'] == 3
    member = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert (member['brief'], member['colour'], member['tool'], member['model'], member.get('account')) == \
        ('apprentice.md', 'yellow', tool, model, account)
    assert 'reasoning' not in member and member['peer'] == f'{CONT_SHIFT}-foreman'
    assert json.loads((d / 'crew.json').read_text(encoding='utf-8'))['crew'] == 'default'   # the shift keeps its crew


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_crew_with_cli_overrides_the_tool_and_falls_back_on_the_model(tmp_path, injected, capsys):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--crew', 'apprentice-codex', '--cli', 'copilot'])
    assert json.loads(capsys.readouterr().out)['tool'] == 'copilot'
    member = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert (member['brief'], member['colour'], member['tool'], member['model']) == \
        ('apprentice.md', 'yellow', 'copilot', launcher.COPILOT_MODEL)
    assert 'account' not in member   # account 2 belongs to the manifest's codex Apprentice, not to copilot


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_cli_equals_form_overrides_crew_and_a_gone_members_tool(tmp_path, injected, monkeypatch, capsys):
    """--cli=copilot counts as an explicit --cli, like '--cli copilot' (asg-2 nit)."""
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--crew', 'apprentice-codex', '--cli=copilot'])
    assert json.loads(capsys.readouterr().out)['tool'] == 'copilot'
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: pid != 502)   # the agy-less Coder is gone
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    crew['members'][1].update(tool='agy', model='gemini-3.8-flash-high')
    (d / 'crew.json').write_text(json.dumps(crew), encoding='utf-8')
    launcher.spawn([CONT_SHIFT, '--role', 'Coder', '--cli=codex'])
    members = {m['label']: m for m in json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members']}
    assert members['Apprentice']['tool'] == 'copilot' and members['Coder']['tool'] == 'codex'


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_without_cli_still_defaults_to_claude(tmp_path, injected, capsys):
    _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Helper'])
    assert json.loads(capsys.readouterr().out)['tool'] == 'claude'


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_crew_reasoning_comes_from_the_named_manifest(tmp_path, injected, capsys):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--crew', 'codex'])
    member = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members'][-1]
    assert (member['tool'], member['model'], member['reasoning']) == ('codex', 'gpt-5.6-luna', 'high')


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_crew_overrides_a_gone_members_record(tmp_path, injected, monkeypatch, capsys):
    d = _container(tmp_path, members=('Foreman', 'Coder', 'Apprentice'), free=(4,))
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    crew['members'][-1].update(tool='agy', model='gemini-3.8-flash-high', brief='apprentice.md', colour='yellow')
    (d / 'crew.json').write_text(json.dumps(crew), encoding='utf-8')
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: pid != 503)   # the Apprentice is gone
    launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--crew', 'apprentice-codex'])
    members = json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members']
    assert [m['label'] for m in members] == ['Foreman', 'Coder', 'Apprentice']
    assert (members[-1]['tool'], members[-1]['model'], members[-1]['account'], members[-1]['identity']) == \
        ('codex', 'gpt-5.6-luna', 2, f'{CONT_SHIFT}-apprentice')


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_crew_refuses_a_role_the_manifest_lacks(tmp_path, injected):
    d = _container(tmp_path)
    before = (d / 'crew.json').read_text(encoding='utf-8')
    with pytest.raises(SystemExit, match='crew apprentice-codex has no role Helper'):
        launcher.spawn([CONT_SHIFT, '--role', 'Helper', '--crew', 'apprentice-codex'])
    with pytest.raises(SystemExit, match='no crew manifest'):
        launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--crew', 'nope'])
    assert (d / 'crew.json').read_text(encoding='utf-8') == before and injected == []


@pytest.mark.usefixtures('_container_isolated')
def test_pane_start_runs_a_spawned_crew_apprentice_on_its_tool_model_and_account(tmp_path, injected, monkeypatch,
                                                                                 capsys):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--crew', 'apprentice-codex'])
    seen = {}

    class Child:
        pid = 42

        def wait(self):
            return 0

    def popen(command, **k):
        seen.update(command=command, env=k.get('env'))
        return Child()
    monkeypatch.setattr(launcher.subprocess, 'Popen', popen)
    monkeypatch.setattr(launcher, 'register_name', lambda pid, name: None)
    monkeypatch.setattr(launcher, 'set_codex_role_name', lambda *a: None)
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: None)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: True)
    launcher.pane('Apprentice', str(d), start=True, cwd=str(tmp_path), slot=3)
    command = seen['command']
    assert command[0] == str(launcher.CODEX_EXE) and command[command.index('--model') + 1] == 'gpt-5.6-luna'
    assert seen['env']['CODEX_HOME'].endswith('.codex-second')
    assert launcher.read_slot(d, 3)['role'] == 'Apprentice'



# ── shift bus: bus.jsonl and nightshift log (NGH-026) ─────────────────────────

def _bus(d):
    path = Path(d) / 'bus.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def _strip(event):
    return {k: v for k, v in event.items() if k != 'ts'}


def test_bus_records_msg_send_sent_and_ack_in_order(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'A1', '--body', 'do X\nand Y')
    launcher.msg(['sent', 'mail0001', '1'])
    launcher.ack(['mail0001', 'Coder', '1'])
    launcher.ack(['mail0001', 'Coder', '1'])
    capsys.readouterr()
    events = _bus(d)
    assert [_strip(e) for e in events] == [
        {'seq': 1, 'event': 'msg', 'id': 1, 'kind': 'assignment', 'sender': 'Foreman', 'recipient': 'Coder',
         'asg': 'A1', 'body': 'do X\nand Y'},
        {'seq': 2, 'event': 'sent', 'id': 1},
        {'seq': 3, 'event': 'ack', 'id': 1, 'by': 'Coder', 'outcome': 'acknowledged'},
        {'seq': 4, 'event': 'ack', 'id': 1, 'by': 'Coder', 'outcome': 'already acknowledged'}]
    assert all(re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d', e['ts']) for e in events)


def test_bus_msg_sent_of_unknown_id_writes_nothing(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        launcher.msg(['sent', 'mail0001', '9'])
    assert _bus(d) == []


def test_bus_protocol_violation_note_is_a_msg_event_and_the_ack_says_so(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        launcher.ack(['mail0001', 'Coder', '42'])
    note, ack_event = _bus(d)
    assert (note['event'], note['kind'], note['sender'], note['recipient']) == ('msg', 'note', 'Coder', 'Foreman')
    assert note['body'].startswith('protocol violation: no message msg:42') and note['id'] == _rows(d)[-1]['id']
    assert _strip(ack_event) == {'seq': 2, 'event': 'ack', 'id': 42, 'by': 'Coder', 'outcome': 'protocol violation',
                                 'reason': 'no message msg:42'}


def test_bus_concurrent_appends_get_unique_increasing_seqs(tmp_path):
    import threading
    def writer(n):
        for i in range(25):
            launcher.bus_append(tmp_path, 'note', writer=n, i=i)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = _bus(tmp_path)
    assert [e['seq'] for e in events] == list(range(1, 201))
    assert sorted((e['writer'], e['i']) for e in events) == [(n, i) for n in range(8) for i in range(25)]
    assert not (tmp_path / 'bus.lock').exists()


def test_bus_failure_warns_but_msg_send_still_stores(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    (d / 'bus.jsonl').mkdir()   # unwritable as a file
    assert _send(capsys, 'Foreman', 'Coder', '--kind', 'note', '--body', 'still stored') == 'msg:1 kind:note asg:-\n'
    assert _rows(d)[0]['body'] == 'still stored'
    launcher.msg(['sent', 'mail0001', '1'])
    assert json.loads(capsys.readouterr().out)['state'] == 'sent'
    launcher.ack(['mail0001', 'Coder', '1'])
    captured = capsys.readouterr()
    assert json.loads(captured.out)['ack'] == 'acknowledged'
    assert 'warning: shift bus not written (ack)' in captured.err


def test_bus_held_lock_times_out_with_a_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher, 'BUS_LOCK_WAIT', 0.2)
    (tmp_path / 'bus.lock').write_text('', encoding='utf-8')   # fresh: another writer holds it
    launcher.bus_append(tmp_path, 'note')
    assert 'warning: shift bus not written (note)' in capsys.readouterr().err
    assert _bus(tmp_path) == [] and (tmp_path / 'bus.lock').exists()   # someone else's lock is left alone


def test_bus_stale_lock_is_taken_over(tmp_path):
    lock = tmp_path / 'bus.lock'
    lock.write_text('', encoding='utf-8')
    old = launcher.time.time() - 60
    os.utime(lock, (old, old))
    launcher.bus_append(tmp_path, 'note')
    assert [e['seq'] for e in _bus(tmp_path)] == [1] and not lock.exists()


def test_bus_torn_last_line_is_closed_off_and_counted(tmp_path, capsys):
    (tmp_path / 'bus.jsonl').write_bytes(b'{"seq": 1, "ts": "2026-10-16T08:00:00", "event": "note"}\n{"seq": 2, "tor')
    launcher.bus_append(tmp_path, 'note', n=3)
    lines = (tmp_path / 'bus.jsonl').read_text(encoding='utf-8').splitlines()
    assert lines[1] == '{"seq": 2, "tor' and json.loads(lines[2])['seq'] == 3


def test_start_puts_a_start_event_on_the_bus(monkeypatch, tmp_path, capsys):
    cwd = _no_launch(monkeypatch, tmp_path)
    state_dir = launcher.start(['--cwd', cwd, 'do it'])
    (event,) = _bus(state_dir)
    assert _strip(event) == {'seq': 1, 'event': 'start', 'shift': state_dir.name, 'ticket': None, 'cwd': cwd,
                             'crew': 'default', 'members': ['Foreman', 'Advisor', 'Coder', 'Tester', 'Apprentice']}


def test_start_dry_run_writes_no_bus(monkeypatch, tmp_path, capsys):
    cwd = _no_launch(monkeypatch, tmp_path)
    launcher.start(['--dry-run', '--cwd', cwd, 'do it'])
    assert not (tmp_path / 'shifts').exists() or not list((tmp_path / 'shifts').rglob('bus.jsonl'))


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_puts_a_spawn_event_on_the_bus(tmp_path, injected, capsys):
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Tester', '--slot', '3'])
    (event,) = _bus(d)
    assert _strip(event) == {'seq': 1, 'event': 'spawn', 'role': 'Tester', 'slot': 3, 'tool': 'claude',
                             'identity': f'{CONT_SHIFT}-tester'}


@pytest.mark.usefixtures('_container_isolated')
def test_failed_spawn_writes_no_bus_event(tmp_path, injected):
    d = _container(tmp_path)
    injected.rc, injected.err = 1, b'no console'
    with pytest.raises(SystemExit):
        launcher.spawn([CONT_SHIFT, '--role', 'Tester'])
    assert _bus(d) == []


def _record_bus(monkeypatch, events):
    real = launcher.bus_append
    monkeypatch.setattr(launcher, 'bus_append',
                        lambda state_dir, event, **f: events.append(('bus', event)) or real(state_dir, event, **f))


@pytest.mark.usefixtures('_container_isolated')
def test_down_puts_result_then_down_on_the_bus_before_its_own_kill(down_world, monkeypatch):
    d, events = down_world
    _record_bus(monkeypatch, events)
    result = {'status': 'done', 'stage': 'tested', 'summary': 'ok'}
    (d / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    launcher.down([CONT_SHIFT])
    result_event, down_event = _bus(d)
    assert _strip(result_event) == {'seq': 1, 'event': 'result', 'result': result}
    assert sorted(down_event['stopped']) == ['Coder', 'Foreman', 'Tester'] and down_event['lock_released'] == []
    assert (down_event['seq'], down_event['event']) == (2, 'down')
    assert events.index(('bus', 'result')) < events.index(('bus', 'down')) < \
        [e[0] for e in events].index('print') < events.index(('kill', 501))


@pytest.mark.usefixtures('_container_isolated')
def test_down_without_result_json_writes_only_down_and_unreadable_result_says_so(down_world):
    d, _ = down_world
    launcher.down([CONT_SHIFT])
    assert [e['event'] for e in _bus(d)] == ['down']
    (d / 'result.json').write_text('{not json', encoding='utf-8')
    launcher.down([CONT_SHIFT])
    result_event = _bus(d)[1]
    assert result_event['event'] == 'result' and result_event['result'] is None
    assert result_event['error'].startswith('result.json unreadable')


# ── nightshift log ──

def _log_shift(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'a1', '--body', 'build\nthe bus')
    launcher.msg(['sent', 'mail0001', '1'])
    launcher.ack(['mail0001', 'Coder', '1'])
    capsys.readouterr()
    return d


def _human(line):
    return re.sub(r'^(#\d+) \d\d:\d\d:\d\d ', r'\1 T ', line)


def test_log_prints_one_human_line_per_event(monkeypatch, tmp_path, capsys):
    d = _log_shift(monkeypatch, tmp_path, capsys)
    launcher.bus_append(d, 'start', shift='s', ticket='NGH-026', cwd='C:/w', crew='default', members=['Foreman', 'Coder'])
    launcher.bus_append(d, 'spawn', role='Tester', slot=3, tool='claude', identity='s-tester')
    launcher.bus_append(d, 'result', result={'status': 'done'})
    launcher.bus_append(d, 'down', stopped=['Coder'], lock_released=['C:/w'])
    launcher.log_command(['mail0001'])
    out = capsys.readouterr().out.splitlines()
    assert [_human(line) for line in out] == [
        '#1 T msg Foreman->Coder assignment asg:a1 msg:1  build the bus',
        '#2 T sent msg:1',
        '#3 T ack msg:1 by Coder: acknowledged',
        '#4 T start ticket:NGH-026 crew:default members:Foreman,Coder cwd:C:/w',
        '#5 T spawn Tester slot:3 tool:claude s-tester',
        '#6 T result {"result": {"status": "done"}}',
        '#7 T down stopped:Coder lock_released:C:/w']
    assert re.match(r'#1 \d\d:\d\d:\d\d msg ', out[0])


def test_log_since_and_json(monkeypatch, tmp_path, capsys):
    d = _log_shift(monkeypatch, tmp_path, capsys)
    launcher.log_command(['mail0001', '--since', '1'])
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ['#2', '#3']
    launcher.log_command(['mail0001', '--json', '--since', '2'])
    raw = (d / 'bus.jsonl').read_text(encoding='utf-8').splitlines()
    assert capsys.readouterr().out.splitlines() == raw[2:]


def test_log_without_a_bus_says_so_and_exits(monkeypatch, tmp_path, capsys):
    _mail_shift(monkeypatch, tmp_path)
    launcher.log_command(['mail0001'])
    captured = capsys.readouterr()
    assert captured.out == '' and 'no bus yet' in captured.err


def test_log_shows_a_torn_line_as_question_mark(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    (d / 'bus.jsonl').write_bytes(b'garbage\n')
    launcher.log_command(['mail0001'])
    assert capsys.readouterr().out == '? garbage\n'


def test_log_follow_picks_up_later_lines_holds_partial_ones_and_stops_on_down(monkeypatch, tmp_path, capsys):
    d = _log_shift(monkeypatch, tmp_path, capsys)
    bus = d / 'bus.jsonl'
    down = json.dumps({'seq': 5, 'ts': '2026-10-16T09:00:00', 'event': 'down', 'stopped': [], 'lock_released': []})
    half = len(down) // 2
    steps = [
        lambda: launcher.bus_append(d, 'note', text='later'),            # a whole new line
        lambda: bus.open('ab').write(down[:half].encode('utf-8')),       # half a line: not printed yet
        lambda: bus.open('ab').write((down[half:] + '\n').encode('utf-8')),
        lambda: pytest.fail('follow did not stop on down'),
    ]
    printed = []

    def sleep(seconds):
        printed.append(len(capsys.readouterr().out.splitlines()))
        steps.pop(0)()
    launcher.log_command(['mail0001', '--follow'], sleep=sleep)
    out = capsys.readouterr().out.splitlines()
    assert printed == [3, 1, 0]   # 3 at first, the later note, nothing for the partial line
    assert [_human(line) for line in out] == ['#5 T down stopped:- lock_released:-']


def test_log_follow_on_a_finished_shift_prints_and_exits(monkeypatch, tmp_path, capsys):
    d = _log_shift(monkeypatch, tmp_path, capsys)
    launcher.bus_append(d, 'down', stopped=[], lock_released=[])
    launcher.log_command(['mail0001', '--follow', '--since', '9'], sleep=lambda s: pytest.fail('should not poll'))
    assert capsys.readouterr().out == ''


def test_log_follow_waits_for_a_missing_bus_and_stops_on_ctrl_c(monkeypatch, tmp_path, capsys):
    _mail_shift(monkeypatch, tmp_path)
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 3:
            raise KeyboardInterrupt
    launcher.log_command(['mail0001', '-f'], sleep=sleep)
    captured = capsys.readouterr()
    assert calls == [launcher.LOG_POLL_SECONDS] * 3 and captured.err.count('no bus yet') == 1


def test_log_is_a_main_command(monkeypatch, capsys):
    assert launcher.COMMANDS['log'] is launcher.log_command
    monkeypatch.setattr(sys, 'argv', ['nightshift', '--help'])
    with pytest.raises(SystemExit) as err:
        launcher.main()
    assert err.value.code == 0
    assert 'log SHIFT [--follow] [--since N] [--json]' in capsys.readouterr().out
    assert 'bus.jsonl' in launcher.__doc__ and '`log SHIFT' in launcher.__doc__


# ── Copilot crew, reasoning effort and context tier (NGH-037) ───────────────

def test_copilot_crew_manifest_and_reasoning_and_context_constants():
    assert launcher.TOOL_REASONING_LEVELS['copilot'] == ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
    assert launcher.CONTEXT_TIERS == ('default', 'long_context')
    copilot = launcher.load_crew('copilot')
    assert copilot['layout'] == '2x4' and copilot['slots'] == 8 and copilot['model'] == 'gemini-3.8-flash'
    # Every role on Gemini 3.8 Flash (owner, 16 Oct: Grok burned half the monthly credits in 30 min).
    assert [(r['role'], r['tool'], r['model'], r.get('reasoning'), r.get('context')) for r in copilot['roles']] == [
        (role, 'copilot', 'gemini-3.8-flash', 'high', 'long_context')
        for role in ('Foreman', 'Advisor', 'Coder', 'Tester', 'Apprentice')
    ]


def test_copilot_command_supports_optional_reasoning_and_context():
    base = launcher.copilot_command('gemini-3.8-flash', 'BRIEF', 'FIRST')
    assert base == [str(launcher.COPILOT_EXE), '--allow-all', '--no-auto-update', '--model', 'gemini-3.8-flash',
                    '-i', 'BRIEF\n\nFIRST']
    with_reasoning = launcher.copilot_command('grok-4.7', 'BRIEF', 'FIRST', reasoning='max')
    assert with_reasoning == [str(launcher.COPILOT_EXE), '--allow-all', '--no-auto-update', '--model', 'grok-4.7',
                              '--reasoning-effort', 'max', '-i', 'BRIEF\n\nFIRST']
    with_context = launcher.copilot_command('gemini-3.8-flash', 'BRIEF', 'FIRST', context='long_context')
    assert with_context == [str(launcher.COPILOT_EXE), '--allow-all', '--no-auto-update', '--model', 'gemini-3.8-flash',
                            '--context', 'long_context', '-i', 'BRIEF\n\nFIRST']
    with_both = launcher.copilot_command('gemini-3.8-flash', 'BRIEF', 'FIRST', reasoning='high', context='long_context')
    assert with_both == [str(launcher.COPILOT_EXE), '--allow-all', '--no-auto-update', '--model', 'gemini-3.8-flash',
                         '--reasoning-effort', 'high', '--context', 'long_context', '-i', 'BRIEF\n\nFIRST']


def test_start_dry_run_copilot_crew_records_reasoning_and_context(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([]))
    monkeypatch.chdir(tmp_path)
    launcher.start(['--dry-run', '--crew', 'copilot', 'task'])
    plan = json.loads(capsys.readouterr().out)
    assert plan['crew'] == 'copilot'
    roles = plan['roles']
    for label in ('Foreman', 'Advisor', 'Coder', 'Tester', 'Apprentice'):
        r = roles[label]
        assert r['tool'] == 'copilot' and r['model'] == 'gemini-3.8-flash'
        assert r['reasoning'] == 'high'
        assert r['context'] == 'long_context'
        cmd = r['command']
        assert cmd[:4] == [str(launcher.COPILOT_EXE), '--allow-all', '--no-auto-update', '--model']
        assert '--model' in cmd and cmd[cmd.index('--model') + 1] == 'gemini-3.8-flash'
        assert '--reasoning-effort' in cmd and cmd[cmd.index('--reasoning-effort') + 1] == 'high'
        assert '--context' in cmd and cmd[cmd.index('--context') + 1] == 'long_context'


# ── Copilot identity, colour and terminal setup (NGH-039) ────────────────────


def test_copilot_command_session_name_is_optional_and_sits_immediately_before_interactive():
    head = [str(launcher.COPILOT_EXE), '--allow-all', '--no-auto-update', '--model', 'gemini-3.8-flash']
    omitted = launcher.copilot_command('gemini-3.8-flash', 'BRIEF', 'FIRST')
    explicit_none = launcher.copilot_command('gemini-3.8-flash', 'BRIEF', 'FIRST', session_name=None)
    assert omitted == explicit_none == head + ['-i', 'BRIEF\n\nFIRST']
    named = launcher.copilot_command(
        'gemini-3.8-flash', 'BRIEF', 'FIRST', reasoning='high', context='long_context',
        session_name='Coder (shift-coder)')
    assert named == head + ['--reasoning-effort', 'high', '--context', 'long_context',
                            '--name', 'Coder (shift-coder)', '-i', 'BRIEF\n\nFIRST']
    reasoning_only = launcher.copilot_command(
        'gemini-3.8-flash', 'BRIEF', 'FIRST', reasoning='high', session_name='Coder (shift-coder)')
    assert reasoning_only[reasoning_only.index('--reasoning-effort'):reasoning_only.index('-i')] == [
        '--reasoning-effort', 'high', '--name', 'Coder (shift-coder)']
    context_only = launcher.copilot_command(
        'gemini-3.8-flash', 'BRIEF', 'FIRST', context='long_context', session_name='Coder (shift-coder)')
    assert context_only[context_only.index('--context'):context_only.index('-i')] == [
        '--context', 'long_context', '--name', 'Coder (shift-coder)']
    # None means omit the flag; any other value, including empty, is inserted unchanged.
    blank = launcher.copilot_command('gemini-3.8-flash', 'BRIEF', 'FIRST', session_name='')
    assert blank[blank.index('-i') - 2:blank.index('-i')] == ['--name', '']
    defaulted = launcher.copilot_command(
        'gemini-3.8-flash', 'BRIEF', 'FIRST', permissions='default', session_name='Tester (shift-tester)')
    assert '--allow-all' not in defaulted
    assert defaulted[1:3] == ['--no-auto-update', '--model']
    assert defaulted[defaulted.index('-i') - 2:defaulted.index('-i')] == ['--name', 'Tester (shift-tester)']


def test_tool_command_forwards_session_name_only_to_copilot():
    name = 'Foreman (shift-foreman)'
    forwarded = launcher.tool_command(
        'copilot', 'grok-4.7', 'BRIEF', 'FIRST', permissions='skip', reasoning='max', session_name=name)
    assert forwarded == launcher.copilot_command(
        'grok-4.7', 'BRIEF', 'FIRST', permissions='skip', reasoning='max', session_name=name)
    assert forwarded[forwarded.index('-i') - 2:forwarded.index('-i')] == ['--name', name]
    assert launcher.tool_command('copilot', 'grok-4.7', 'BRIEF', 'FIRST') == \
        launcher.copilot_command('grok-4.7', 'BRIEF', 'FIRST')
    assert launcher.tool_command('copilot', 'grok-4.7', 'BRIEF', 'FIRST', session_name=None) == \
        launcher.copilot_command('grok-4.7', 'BRIEF', 'FIRST')
    for tool, model in (('codex', 'gpt-6.1-sol'), ('agy', 'gemini-3.8-flash-high')):
        without = launcher.tool_command(tool, model, 'BRIEF', 'FIRST', reasoning='high')
        with_name = launcher.tool_command(tool, model, 'BRIEF', 'FIRST', reasoning='high', session_name=name)
        assert with_name == without and '--name' not in with_name


def test_start_dry_run_copilot_crew_names_sessions_and_selects_role_schemes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('unexpected launch'))
    launcher.start(['--dry-run', '--no-worktree', '--cwd', str(tmp_path), '--crew', 'copilot', 'task'])
    plan = json.loads(capsys.readouterr().out)
    members = {m['label']: m for m in launcher.crew_members(launcher.load_crew('copilot'), plan['shift'])}
    assert [m['label'] for m in members.values()] == ['Foreman', 'Advisor', 'Coder', 'Tester', 'Apprentice']
    assert 'codex_colours' in launcher.wt_command.__code__.co_varnames
    for label, member in members.items():
        command = plan['roles'][label]['command']
        assert plan['roles'][label]['session_name'] == member['identity']
        assert command[command.index('-i') - 2:command.index('-i')] == [
            '--name', f"{label} ({member['identity']})"]
        assert command.index('--reasoning-effort') < command.index('--name')
        if 'context' in member:
            assert command.index('--context') < command.index('--name')
        else:
            assert '--context' not in command
    coloured = {}
    for cell in _cells(plan['wt_argv']):
        if '--pane' not in cell:
            assert '--colorScheme' not in cell
            continue
        label = cell[cell.index('--pane') + 1]
        coloured[label] = cell[cell.index('--colorScheme') + 1]
    assert coloured == {label: 'Nightshift ' + member['colour'] for label, member in members.items()}
    trial = launcher.wt_command(
        'S', Path('C:/state'), agents=True, mode_flag='--live', members=list(members),
        codex_colours={label: member['colour'] for label, member in members.items()})
    assert '--colorScheme' not in trial


def test_load_crew_rejects_unsupported_reasoning_and_context(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'bad-codex-max', """\
name: bad-codex-max
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true, tool: codex, reasoning: max}
  - {role: Coder, brief: coder.md}
""")
    with pytest.raises(SystemExit, match=r"reasoning 'max' for Foreman \(tool codex\) must be one of: low, medium, high, xhigh"):
        launcher.load_crew('bad-codex-max')

    _crew_file(monkeypatch, tmp_path, 'bad-copilot-reasoning', """\
name: bad-copilot-reasoning
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true, tool: copilot, reasoning: bogus}
  - {role: Coder, brief: coder.md}
""")
    with pytest.raises(SystemExit, match=r"reasoning 'bogus' for Foreman \(tool copilot\) must be one of: none, minimal, low, medium, high, xhigh, max"):
        launcher.load_crew('bad-copilot-reasoning')

    _crew_file(monkeypatch, tmp_path, 'bad-copilot-context', """\
name: bad-copilot-context
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true, tool: copilot, context: huge}
  - {role: Coder, brief: coder.md}
""")
    with pytest.raises(SystemExit, match=r"context 'huge' for Foreman must be one of: default, long_context"):
        launcher.load_crew('bad-copilot-context')

    _crew_file(monkeypatch, tmp_path, 'bad-codex-context', """\
name: bad-codex-context
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true, tool: codex, context: long_context}
  - {role: Coder, brief: coder.md}
""")
    with pytest.raises(SystemExit, match=r"context 'long_context' for Foreman: tool codex has no context tier flag"):
        launcher.load_crew('bad-codex-context')

    _crew_file(monkeypatch, tmp_path, 'bad-claude-context', """\
name: bad-claude-context
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true, tool: claude, context: long_context}
  - {role: Coder, brief: coder.md}
""")
    with pytest.raises(SystemExit, match=r"context 'long_context' for Foreman: tool claude has no context tier flag"):
        launcher.load_crew('bad-claude-context')


# ── Floor CLI (NGH-035): all state and process effects stay isolated ──────────

def test_floor_default_paths_are_derived_from_top_level_root(monkeypatch):
    import ast
    tree = ast.parse(Path(launcher.__file__).read_text(encoding='utf-8'))
    names = {'SOLID_DIR', 'SHIFTS_DIR', 'LOCKS_DIR', 'BUILDS_DIR', 'FLOOR_FILE'}
    env = {'Path': Path}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<constants>', 'exec'), env)
    assert env['SOLID_DIR'] == Path('C:/nightshift')
    for name, suffix in [('SHIFTS_DIR', 'shifts'), ('LOCKS_DIR', 'locks'),
                         ('BUILDS_DIR', 'builds'), ('FLOOR_FILE', 'floor.json')]:
        assert env[name] == env['SOLID_DIR'] / suffix


def test_handover_uses_recorded_build_evidence_without_running_suite(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, 'suite_result', lambda: pytest.fail('teardown ran suite'))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k: pytest.fail('unexpected process'))
    (tmp_path / 'builds').mkdir()
    (tmp_path / 'builds' / '1.json').write_text(json.dumps(
        {'n': 1, 'status': 'failed', 'exit': 9, 'head': 'abcdef123', 'dirty': True, 'cmd': 'unit suite'}), encoding='utf-8')
    report = '\n'.join(launcher.handover_report(tmp_path, {'roles': {}}))
    assert '#1 failed exit=9 head=abcdef12 dirty=True unit suite' in report
    assert 'teardown does not run tests' in report


@pytest.mark.usefixtures('_container_isolated')
def test_down_never_kills_recorded_pids_of_dead_roles(monkeypatch, tmp_path, capsys):
    d = _container(tmp_path, members=('Foreman', 'Coder'), free=(3, 4))
    monkeypatch.setattr(launcher, 'pid_alive', lambda *a, **k: False)
    monkeypatch.setattr(launcher, 'process_running', lambda *a, **k: False)
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {})
    monkeypatch.setattr(launcher, 'own_ancestors', lambda: set())
    monkeypatch.setattr(launcher, 'shift_commits', lambda *a: [])
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k: pytest.fail('dead PID was targeted'))
    launcher.down([d.name, '--hold'])
    assert json.loads(capsys.readouterr().out)['stopped'] == []


@pytest.fixture
def floor_world(monkeypatch, tmp_path):
    for name, suffix in [('SOLID_DIR', ''), ('SHIFTS_DIR', 'shifts'), ('LOCKS_DIR', 'locks'),
                         ('BUILDS_DIR', 'builds'), ('FLOOR_FILE', 'floor.json'), ('LEGACY_DIR', 'legacy')]:
        monkeypatch.setattr(launcher, name, tmp_path / suffix)
    launcher.SHIFTS_DIR.mkdir()
    monkeypatch.setattr(launcher, 'process_start_epoch', lambda pid: 123.0)
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {})
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('real launch'))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k: pytest.fail('unexpected subprocess'))
    return tmp_path


def _floor_shift(root, suffix, alive=True, result='none', roles=None, slots=None):
    d = root / 'shifts' / ('Nightshift-shift-' + suffix)
    d.mkdir(parents=True)
    record = {'shift': d.name, 'state_dir': str(d), 'task': 'NGH-035', 'alive': alive,
              'result': result, 'roles': roles or {}, 'messages': {'unsent': 0, 'unacked': 0}}
    if slots is not None:
        record['slots'] = slots
    return d, record


def _floor_snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('command', ['cleaner', 'warden', 'gaffer'])
def test_floor_empty_dry_run_does_not_write_any_state(floor_world, command, capsys):
    before = _floor_snapshot(floor_world)
    report = getattr(launcher, command)(['--dry-run'])
    assert report['dry_run'] is True
    assert json.loads(capsys.readouterr().out) == report
    assert _floor_snapshot(floor_world) == before


def test_cleaner_protects_live_active_and_reaps_dead_or_finished_only(floor_world, monkeypatch, capsys):
    records = {}
    eligible = []
    for suffix, alive, result in [('live', True, 'none'), ('invalid', True, 'invalid'),
                                  ('dead', False, 'none'), ('done', True, 'done'), ('halt', True, 'halted')]:
        d, record = _floor_shift(floor_world, suffix, alive, result)
        records[d] = record
        (d / 'claims.json').write_text(json.dumps({'x': {'role': 'Coder', 'time': 'old'}}), encoding='utf-8')
        if not alive or result in ('done', 'halted'):
            eligible.append(d)
    monkeypatch.setattr(launcher, 'shift_record', lambda d: records[Path(d)])
    calls = []
    def fake_down(argv, floor=None):
        calls.append(argv)
        records[launcher.SHIFTS_DIR / argv[0]]['alive'] = False
    monkeypatch.setattr(launcher, 'down', fake_down)
    before = _floor_snapshot(floor_world)
    launcher.cleaner(['--dry-run'])
    capsys.readouterr()
    assert calls == [] and _floor_snapshot(floor_world) == before
    launcher.cleaner([])
    capsys.readouterr()
    assert {args[0] for args in calls} == {d.name for d in eligible}
    assert all('--close' in args for args in calls)
    for d, record in records.items():
        assert bool(json.loads((d / 'claims.json').read_text())) == (d not in eligible)
    floor = json.loads(launcher.FLOOR_FILE.read_text())
    assert {row['shift'] for row in floor['reaped']} == {d.name for d in eligible}
    assert all({'shift', 'task', 'result', 'reaped_at'} <= row.keys() for row in floor['reaped'])
    snapshot = _floor_snapshot(floor_world)
    launcher.cleaner([])
    assert _floor_snapshot(floor_world) == snapshot
    assert len(calls) == len(eligible)


def test_warden_never_tears_down_or_patrols_dead_finished_shifts(floor_world, monkeypatch, capsys):
    records = {}
    for suffix, alive, result in [('active', True, 'none'), ('blocked', True, 'blocked'),
                                  ('dead', False, 'none'), ('done', True, 'done'), ('halt', True, 'halted')]:
        d, record = _floor_shift(floor_world, suffix, alive, result)
        records[d] = record
    monkeypatch.setattr(launcher, 'shift_record', lambda d: records[Path(d)])
    monkeypatch.setattr(launcher, 'down', lambda *a: pytest.fail('warden tore down a shift'))
    delivered, nudged = [], []
    monkeypatch.setattr(launcher, 'deliver_pending', lambda d, dry_run=False: delivered.append(Path(d)) or [])
    monkeypatch.setattr(launcher, 'nudge_idle_foremen', lambda d, dry_run=False: nudged.append(Path(d)) or [])
    launcher.warden([])
    expected = {d for d, r in records.items() if r['alive'] and r['result'] not in ('done', 'halted')}
    assert set(delivered) == set(nudged) == expected
    floor = json.loads(launcher.FLOOR_FILE.read_text())
    assert floor['last_warden_at']
    assert set(floor['health']) == {d.name for d in expected}
    capsys.readouterr()


def test_gaffer_default_capacity_and_tick_order_never_launch(floor_world, monkeypatch, capsys):
    records = {}
    for suffix in ('one', 'two'):
        d, record = _floor_shift(floor_world, suffix)
        records[d] = record
    monkeypatch.setattr(launcher, 'shift_record', lambda d: records[Path(d)])
    calls = []
    monkeypatch.setattr(launcher, 'cleaner', lambda argv: calls.append('cleaner') or {})
    monkeypatch.setattr(launcher, 'warden', lambda argv: calls.append('warden') or {})
    monkeypatch.setattr(launcher, 'start', lambda *a: pytest.fail('capacity exceeded'))
    report = launcher.gaffer(['--tick', '--ticket', 'NGH-035', '--repo', str(floor_world)])
    assert calls == ['cleaner', 'warden']
    assert report['reason'] == 'at_capacity'
    capsys.readouterr()


def test_legacy_shift_lookup_keeps_original_mailbox_and_claims(floor_world, monkeypatch):
    legacy = launcher.LEGACY_DIR / 'shifts' / 'Nightshift-shift-legacy01'
    legacy.mkdir(parents=True)
    (legacy / 'claims.json').write_text('{"x":{"role":"Coder"}}', encoding='utf-8')
    # Map only the default-root comparison to the isolated new root; neither real root is scanned.
    real_path = Path
    monkeypatch.setattr(launcher, 'Path', lambda p, *args: launcher.SHIFTS_DIR
                        if p == 'C:/nightshift/shifts' else real_path(p, *args))
    before = _floor_snapshot(floor_world)
    assert launcher.resolve_shift(legacy.name) == legacy
    assert launcher.resolve_shift('legacy01') == legacy
    assert _floor_snapshot(floor_world) == before
    assert not (launcher.SHIFTS_DIR / legacy.name).exists()


@pytest.mark.parametrize('command', ['cleaner', 'warden', 'gaffer'])
def test_floor_commands_route_through_main_and_help_exits_without_effects(floor_world, monkeypatch, capsys, command):
    assert command in launcher.COMMANDS
    before = _floor_snapshot(floor_world)
    monkeypatch.setattr(sys, 'argv', ['nightshift', command, '--help'])
    with pytest.raises(SystemExit) as exc:
        launcher.main()
    assert exc.value.code == 0
    assert '--dry-run' in capsys.readouterr().out
    assert _floor_snapshot(floor_world) == before


@pytest.mark.parametrize('worked,busy,expected', [(False, False, False), (True, True, False), (True, False, True)])
def test_codex_live_buffer_rename_requires_finished_idle_turn(worked, busy, expected):
    lines = ['older output'] * 6 + (['Worked for 10s'] if worked else [])
    if busy:
        lines.append('Working (10s • esc to interrupt)')
    lines += ['› Ask Codex to do anything', '', '  GPT-6.1-Sol high · 90% left']
    assert launcher.codex_prompt_is_empty(lines) is expected


@pytest.mark.parametrize('submitted', [False, True])
def test_delivery_fences_stale_incarnations_and_requires_submission(monkeypatch, tmp_path, capsys, submitted):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'old', '--body', 'old')
    _relaunch_coder(d)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'current', '--body', 'current')
    monkeypatch.setattr(launcher, 'pid_alive', lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(launcher.subprocess, 'run', lambda argv, **k:
                        sent.append(argv) or subprocess.CompletedProcess(argv, 0, json.dumps({'submitted': submitted}).encode()))
    before = _floor_snapshot(tmp_path)
    assert launcher.deliver_pending(d, dry_run=True) == [2]
    assert sent == [] and _floor_snapshot(tmp_path) == before
    assert launcher.deliver_pending(d) == ([2] if submitted else [])
    assert len(sent) == 1 and sent[0][0:3] == ['agents-cli', 'send', '32']
    assert 'msg:2 kind:assignment asg:current' in sent[0]
    assert [r['state'] for r in _rows(d)] == ['stored', 'sent' if submitted else 'stored']
    if submitted:
        assert launcher.deliver_pending(d) == [] and len(sent) == 1


def test_warden_dry_run_with_pending_mailbox_writes_no_sqlite_sidecars(floor_world, monkeypatch, capsys):
    d = _mail_shift(monkeypatch, floor_world)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'dry', '--body', 'do work')
    monkeypatch.setattr(launcher, 'pid_alive', lambda *a, **k: True)
    monkeypatch.setattr(launcher, 'session_idle_since', lambda *a: None)
    before = _floor_snapshot(floor_world)
    report = launcher.warden([d.name, '--dry-run'])
    assert report['shifts'][0]['delivered'] == [1]
    assert _floor_snapshot(floor_world) == before


@pytest.mark.parametrize('kind', ['fresh-empty', 'fresh-live', 'fresh-dead', 'old-live'])
def test_stale_floor_lock_checks_general_writer_pid_and_age(floor_world, monkeypatch, kind):
    path = floor_world / 'claims.lock'
    path.write_text('' if kind == 'fresh-empty' else json.dumps({'pid': 8765}), encoding='utf-8')
    seen = []
    monkeypatch.setattr(launcher, 'process_running', lambda pid: seen.append(pid) or kind != 'fresh-dead')
    monkeypatch.setattr(launcher, 'pid_alive', lambda *a: pytest.fail('writer need not be an agent'))
    if kind == 'old-live':
        old = launcher.time.time() - launcher.FILE_LOCK_STALE_S - 1
        os.utime(path, (old, old))
    assert launcher.stale_file_lock(path) is (kind in ('fresh-dead', 'old-live'))
    assert path.exists()


def test_warden_normalizes_only_live_claims_and_recovers_only_unfinished_slots(floor_world, monkeypatch, capsys):
    d, record = _floor_shift(floor_world, 'patrol', roles={'Coder': {'alive': True}, 'Tester': {'alive': False}},
                             slots=[{'slot': 1, 'role': 'Foreman', 'state': 'live'},
                                    {'slot': 2, 'role': 'Coder', 'state': 'dead'},
                                    {'slot': 3, 'role': None, 'state': 'empty'}])
    claims = {'C:\\space name\\a.py': {'role': 'Coder'}, 'b.py': {'role': 'Tester'},
              'c.py': {'role': 'foreign-coder'}}
    (d / 'claims.json').write_text(json.dumps(claims), encoding='utf-8')
    (d / 'claims.lock').write_text(json.dumps({'pid': 111}), encoding='utf-8')
    (d / 'bus.lock').write_text('', encoding='utf-8')
    old = launcher.time.time() - launcher.FILE_LOCK_STALE_S - 1
    os.utime(d / 'bus.lock', (old, old))
    monkeypatch.setattr(launcher, 'process_running', lambda *a: False)
    monkeypatch.setattr(launcher, 'shift_record', lambda _: record)
    monkeypatch.setattr(launcher, 'deliver_pending', lambda *a: [])
    monkeypatch.setattr(launcher, 'nudge_idle_foremen', lambda *a: [])
    recovered = []
    monkeypatch.setattr(launcher, 'recover_pane', lambda state, slot, dry_run=False:
                        recovered.append((slot, dry_run)) or {'slot': slot})
    before = _floor_snapshot(floor_world)
    launcher.warden([d.name, '--dry-run'])
    assert _floor_snapshot(floor_world) == before and recovered == [(2, True)]
    launcher.warden([d.name])
    assert recovered == [(2, True), (2, False)]
    assert not (d / 'claims.lock').exists() and not (d / 'bus.lock').exists()
    updated = json.loads((d / 'claims.json').read_text())
    assert updated['C:\\space name\\a.py']['role'] == d.name + '-coder'
    assert updated['b.py']['role'] == 'Tester' and updated['c.py']['role'] == 'foreign-coder'
    record['result'] = 'blocked'
    launcher.warden([d.name])
    assert len(recovered) == 2
    capsys.readouterr()


def test_nudge_respects_threshold_submission_and_repeat_cooldown(floor_world, monkeypatch):
    d = floor_world / 'shifts' / 'Nightshift-shift-nudge'
    d.mkdir()
    monkeypatch.setattr(launcher, 'member_info', lambda *a: {'pid': 12, 'started': 1})
    monkeypatch.setattr(launcher, 'pid_alive', lambda *a: True)
    idle = [launcher.NUDGE_IDLE_S - 1]
    monkeypatch.setattr(launcher, 'session_idle_since', lambda *a: idle[0])
    sent = []
    monkeypatch.setattr(launcher, 'send_header', lambda *a: sent.append(a) or True)
    assert launcher.nudge_idle_foremen(d) == [] and sent == []
    idle[0] += 1
    before = _floor_snapshot(floor_world)
    assert launcher.nudge_idle_foremen(d, dry_run=True) == ['Foreman']
    assert _floor_snapshot(floor_world) == before and sent == []
    assert launcher.nudge_idle_foremen(d) == ['Foreman']
    assert launcher.nudge_idle_foremen(d) == [] and len(sent) == 1


@pytest.mark.parametrize('queued', [False, True])
def test_gaffer_dispatch_preserves_options_queue_and_live_ticket_idempotency(floor_world, monkeypatch, capsys, queued):
    repo = str(floor_world / 'repo with spaces')
    entry = {'ticket': 'NGH-035', 'crew': 'codex', 'repo': repo}
    launcher.FLOOR_FILE.write_text(json.dumps({'queue': [entry, 'NGH-036'], 'keep': 'sentinel'}))
    records, calls = {}, []
    monkeypatch.setattr(launcher, 'shift_record', lambda d: records[Path(d)])

    def fake_start(argv):
        calls.append(argv)
        d, record = _floor_shift(floor_world, 'dispatched')
        (d / 'ticket.txt').write_text('NGH-035')
        records[d] = record
        return d
    monkeypatch.setattr(launcher, 'start', fake_start)
    flags = [] if queued else ['--ticket', 'NGH-035', '--crew', 'codex', '--repo', repo]
    before = _floor_snapshot(floor_world)
    report = launcher.gaffer([*flags, '--dry-run'])
    assert report['reason'] == 'would_dispatch' and calls == []
    assert _floor_snapshot(floor_world) == before
    report = launcher.gaffer(flags)
    assert report['dispatched'] is True
    assert calls == [['NGH-035', '--crew', 'codex', '--cwd', repo]]
    floor = json.loads(launcher.FLOOR_FILE.read_text())
    assert floor['keep'] == 'sentinel'
    assert floor['queue'] == (['NGH-036'] if queued else [entry, 'NGH-036'])
    dispatch = floor['dispatches'][0]
    assert (dispatch['ticket'], dispatch['repo'], dispatch['crew']) == ('NGH-035', repo, 'codex')
    assert dispatch['dispatched_at']
    assert launcher.gaffer(['--ticket', 'NGH-035'])['reason'] == 'already_running'
    assert len(calls) == 1
    capsys.readouterr()


def test_gaffer_failed_start_retains_queued_ticket_and_releases_lock(floor_world, monkeypatch):
    launcher.FLOOR_FILE.write_text(json.dumps({'queue': ['NGH-035', 'NGH-036']}))
    before = _floor_snapshot(floor_world)
    monkeypatch.setattr(launcher, 'start', lambda *a: (_ for _ in ()).throw(SystemExit('launch failed')))
    with pytest.raises(SystemExit, match='launch failed'):
        launcher.gaffer([])
    assert _floor_snapshot(floor_world) == before
    assert not launcher.FLOOR_FILE.with_suffix('.lock').exists()


def test_gaffer_concurrent_calls_serialize_capacity_check_and_dispatch(floor_world, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    records, calls = {}, []
    monkeypatch.setattr(launcher, 'process_running', lambda *a: True)
    monkeypatch.setattr(launcher, 'shift_record', lambda d: records[Path(d)])

    def fake_start(argv):
        calls.append(argv)
        d, record = _floor_shift(floor_world, 'dispatch' + str(len(calls)))
        records[d] = record
        return d
    monkeypatch.setattr(launcher, 'start', fake_start)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda ticket: launcher.gaffer(['--ticket', ticket, '--max-shifts', '1']),
                                ['NGH-035', 'NGH-036']))
    assert len(calls) == 1
    assert sorted(r['reason'] for r in reports) == ['at_capacity', 'dispatched']
    assert len(json.loads(launcher.FLOOR_FILE.read_text())['dispatches']) == 1


def test_top_level_help_lists_floor_commands_and_close_default(floor_world, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'argv', ['nightshift', '--help'])
    with pytest.raises(SystemExit) as exc:
        launcher.main()
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert all(word in text for word in ('cleaner', 'warden', 'gaffer'))
    assert '--hold (default)' not in text


@pytest.mark.parametrize('receipt,tail,expected', [
    ({'submitted': True}, {}, True),
    ({'submitted': False, 'enter': True, 'sent': 'msg:7 kind:note asg:-'},
     {'state': 'idle', 'lines': ['› msg:7 kind:note asg:-', 'answer', '›']}, False),
    ({'enter': True, 'sent': 'msg:7 kind:note asg:-', 'via': 'inject'},
     {'state': 'idle', 'lines': ['› msg:7 kind:note asg:-', 'answer', '›']}, True),
    ({'enter': True, 'sent': 'msg:7 kind:note asg:-', 'via': 'inject'},
     {'state': 'busy', 'lines': ['› msg:7 kind:note asg:-', 'Working', '› Ask Codex to do anything']}, True),
    ({'enter': True, 'sent': 'msg:7 kind:note asg:-', 'via': 'inject'},
     {'state': 'idle', 'lines': ['› msg:7 kind:note asg:-']}, False),
    ({'enter': True, 'sent': 'msg:7 kind:note asg:-', 'via': 'inject'},
     {'state': 'idle', 'lines': ['› msg:7 kind:note asg:-', 'answer', '› user draft']}, False),
    ({'enter': True, 'sent': 'msg:7 kind:note asg:-', 'via': 'inject'},
     {'state': 'unknown', 'lines': ['› msg:7 kind:note asg:-', 'answer', '›']}, False),
    ({'enter': False, 'sent': 'msg:7 kind:note asg:-'}, {}, False),
])
def test_send_header_confirms_legacy_receipts_only_after_cleared_composer(monkeypatch, receipt, tail, expected):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        result = receipt if argv[1] == 'send' else tail
        return subprocess.CompletedProcess(argv, 0, json.dumps(result).encode(), b'')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    assert launcher.send_header(32, 'msg:7 kind:note asg:-') is expected
    assert sum(argv[1] == 'send' for argv in calls) == 1
    if 'submitted' in receipt:
        assert len(calls) == 1


def test_uncertain_delivery_is_tail_checked_without_blind_resend(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'once', '--body', 'do work')
    monkeypatch.setattr(launcher, 'pid_alive', lambda *a, **k: True)
    calls = []
    tail = {'state': 'idle', 'lines': ['› msg:1 kind:assignment asg:once']}

    def run(argv, **kwargs):
        calls.append(argv)
        result = {'enter': True, 'sent': argv[-1], 'via': 'inject'} if argv[1] == 'send' else tail
        return subprocess.CompletedProcess(argv, 0, json.dumps(result).encode(), b'')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    assert launcher.deliver_pending(d) == []
    assert launcher.deliver_pending(d) == []
    assert sum(argv[1] == 'send' for argv in calls) == 1
    assert _rows(d)[0]['state'] == 'stored'
    tail['lines'] += ['processing complete', '›']
    assert launcher.deliver_pending(d) == [1]
    assert sum(argv[1] == 'send' for argv in calls) == 1
    assert _rows(d)[0]['state'] == 'sent'


@pytest.mark.parametrize('tool', ['codex', 'copilot'])
@pytest.mark.parametrize('state,recent_activity,expected', [('idle', False, True), ('busy', False, False),
                                                          ('unknown', False, False), ('idle', True, False)])
def test_non_claude_foreman_nudge_requires_idle_and_old_bus_activity(floor_world, monkeypatch, tool,
                                                                  state, recent_activity, expected):
    d = floor_world / 'shifts' / 'Nightshift-shift-idle'
    d.mkdir()
    now = 1_800_000_000.0
    monkeypatch.setattr(launcher.time, 'time', lambda: now)
    monkeypatch.setattr(launcher, 'member_info', lambda *a: {'pid': 12, 'tool': tool,
                                                          'started': now - launcher.NUDGE_IDLE_S - 10})
    monkeypatch.setattr(launcher, 'pid_alive', lambda *a: True)
    monkeypatch.setattr(launcher, 'session_idle_since', lambda *a: pytest.fail('Claude-only idle query'))
    monkeypatch.setattr(launcher, 'agent_tail', lambda pid: {'state': state})
    if recent_activity:
        (d / 'bus.jsonl').write_text(json.dumps({'seq': 1, 'event': 'ack', 'by': 'Foreman', 'id': 123,
            'ts': launcher.time.strftime('%Y-%m-%dT%H:%M:%S', launcher.time.localtime(now - 1))}) + '\n')
    sent = []
    monkeypatch.setattr(launcher, 'send_header', lambda *a: sent.append(a) or True)
    assert launcher.nudge_idle_foremen(d) == (['Foreman'] if expected else [])
    assert bool(sent) is expected


def test_warden_core_filter_uses_manifest_base_role_and_skips_helpers(floor_world, monkeypatch, capsys):
    d, record = _floor_shift(floor_world, 'core', roles={'Foreman': {'alive': True}}, slots=[
        {'slot': 1, 'role': 'Foreman', 'state': 'live'},
        {'slot': 2, 'role': 'Builder', 'state': 'dead'},
        {'slot': 3, 'role': 'Apprentice', 'state': 'dead'},
        {'slot': 4, 'role': 'Helper', 'state': 'dead'},
    ])
    members = [{'label': label, 'role': role, 'identity': d.name + '-' + label.lower()}
               for label, role in [('Foreman', 'Foreman'), ('Builder', 'Coder'),
                                   ('Apprentice', 'Apprentice'), ('Helper', 'Helper')]]
    (d / 'crew.json').write_text(json.dumps({'members': members}), encoding='utf-8')
    monkeypatch.setattr(launcher, 'shift_record', lambda *a: record)
    monkeypatch.setattr(launcher, 'deliver_pending', lambda *a: [])
    monkeypatch.setattr(launcher, 'nudge_idle_foremen', lambda *a: [])
    recovered = []
    monkeypatch.setattr(launcher, 'recover_pane', lambda state, slot, dry_run=False:
                        recovered.append(slot) or {'slot': slot})
    launcher.warden([d.name])
    assert recovered == [2]
    capsys.readouterr()


def test_mailbox_read_sees_committed_uncheckpointed_wal_without_changing_source(tmp_path, monkeypatch, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    # Keep the writer open with auto-checkpoint disabled so the new message exists in WAL, not only main DB.
    with launcher.mailbox(d) as writer:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        launcher.insert_message(writer, d, 'assignment', 'Foreman', 'Coder', 'committed in WAL', 'wal')
        assert (d / 'mailbox.sqlite3-wal').stat().st_size > 0
        before = _floor_snapshot(tmp_path)
        rows = launcher.unacked_messages(d)
        assert len(rows) == 1 and rows[0]['body'] == 'committed in WAL'
        assert _floor_snapshot(tmp_path) == before


def test_missing_mailbox_read_does_not_create_directory_or_database(tmp_path):
    d = tmp_path / 'absent'
    with launcher.mailbox(d, create=False) as conn:
        assert conn is None
    assert not d.exists()


def test_closed_checkpointed_mailbox_repeat_reads_leave_source_bytes_and_files_unchanged(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'assignment', '--asg', 'closed', '--body', 'checkpointed')
    assert not (d / 'mailbox.sqlite3-wal').exists() and not (d / 'mailbox.sqlite3-shm').exists()
    before = _floor_snapshot(tmp_path)
    for _ in range(3):
        rows = launcher.unacked_messages(d)
        assert len(rows) == 1 and rows[0]['body'] == 'checkpointed'
        assert _floor_snapshot(tmp_path) == before


def test_snapshot_read_exception_closes_connection_and_removes_scratch(monkeypatch, tmp_path, capsys):
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'note', '--body', 'before error')
    before = _floor_snapshot(tmp_path)
    with pytest.raises(RuntimeError, match='reader exception'):
        with launcher.mailbox(d, create=False) as conn:
            scratch = Path(conn.execute('PRAGMA database_list').fetchone()[2])
            assert not scratch.is_relative_to(d)
            raise RuntimeError('reader exception')
    assert not scratch.exists()
    with pytest.raises(launcher.sqlite3.ProgrammingError):
        conn.execute('SELECT 1')
    assert _floor_snapshot(tmp_path) == before
    assert len(launcher.unacked_messages(d)) == 1


def test_snapshot_copy_serializes_helper_writer_until_capture_completes(monkeypatch, tmp_path, capsys):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'note', '--body', 'before snapshot')
    copied, release_copy, attempting, entered = [threading.Event() for _ in range(4)]
    original_copy = launcher.shutil.copy2

    def paused_copy(src, dst, *args, **kwargs):
        result = original_copy(src, dst, *args, **kwargs)
        if Path(src).name == 'mailbox.sqlite3':
            copied.set()
            if not release_copy.wait(5):
                raise TimeoutError('test did not release snapshot copy')
        return result

    def write_message():
        attempting.set()
        with launcher.mailbox(d) as writer:
            entered.set()
            launcher.insert_message(writer, d, 'note', 'Foreman', 'Coder', 'after snapshot')

    monkeypatch.setattr(launcher.shutil, 'copy2', paused_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(launcher.unacked_messages, d)
        try:
            assert copied.wait(5)
            writer = pool.submit(write_message)
            assert attempting.wait(5)
            assert not entered.wait(0.2), 'helper writer entered during DB/WAL capture'
        finally:
            release_copy.set()
        assert [row['body'] for row in reader.result(timeout=5)] == ['before snapshot']
        writer.result(timeout=5)
    assert [row['body'] for row in launcher.unacked_messages(d)] == ['before snapshot', 'after snapshot']


def test_snapshot_copy_error_releases_guard_and_cleans_temporary_files(monkeypatch, tmp_path, capsys):
    from concurrent.futures import ThreadPoolExecutor
    d = _mail_shift(monkeypatch, tmp_path)
    _send(capsys, 'Foreman', 'Coder', '--kind', 'note', '--body', 'safe')
    before = _floor_snapshot(tmp_path)
    temps = []
    original_temp = launcher.tempfile.TemporaryDirectory

    def track_temp(*args, **kwargs):
        temp = original_temp(*args, **kwargs)
        temps.append(Path(temp.name))
        return temp

    def fail_copy(*args, **kwargs):
        raise OSError('simulated copy failure')

    with monkeypatch.context() as patch:
        patch.setattr(launcher.tempfile, 'TemporaryDirectory', track_temp)
        patch.setattr(launcher.shutil, 'copy2', fail_copy)
        with pytest.raises(OSError, match='simulated copy failure'):
            launcher.unacked_messages(d)
    assert temps and all(not p.exists() for p in temps)
    assert _floor_snapshot(tmp_path) == before
    # Different thread proves the mutex was released, rather than recursively reentered by this thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert len(pool.submit(launcher.unacked_messages, d).result(timeout=5)) == 1


def test_mailbox_guard_timeout_does_not_steal_ownership_or_leave_files(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'mailbox.sqlite3'

    def enter_guard():
        with launcher.mailbox_guard(path, timeout_ms=20):
            return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        with launcher.mailbox_guard(path):
            with pytest.raises(TimeoutError, match='still held'):
                pool.submit(enter_guard).result(timeout=5)
        assert pool.submit(enter_guard).result(timeout=5) is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('abandoned', [False, True])
def test_mailbox_guard_releases_acquired_or_abandoned_handle_on_exception(monkeypatch, tmp_path, abandoned):
    calls = []

    class Api:
        def __init__(self, name, result):
            self.name, self.result = name, result

        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result

    kernel = types.SimpleNamespace(CreateMutexW=Api('create', 123), WaitForSingleObject=Api('wait', 0x80 if abandoned else 0),
                                   ReleaseMutex=Api('release', True), CloseHandle=Api('close', True))
    monkeypatch.setattr(launcher.ctypes, 'WinDLL', lambda *a, **k: kernel)
    with pytest.raises(RuntimeError, match='inside guard'):
        with launcher.mailbox_guard(tmp_path / 'mailbox.sqlite3'):
            raise RuntimeError('inside guard')
    assert [name for name, _ in calls] == ['create', 'wait', 'release', 'close']
    assert calls[-2][1] == calls[-1][1] == (123,)


def test_cleaner_wires_down_claims_locks_window_bus_and_repeat_noop(floor_world, monkeypatch, capsys):
    d = _container(floor_world, members=('Foreman', 'Coder'), free=(3, 4))
    monkeypatch.setattr(launcher, 'pid_alive', lambda *a, **k: False)
    monkeypatch.setattr(launcher, 'process_running', lambda *a, **k: False)
    monkeypatch.setattr(launcher, 'own_ancestors', lambda: set())
    monkeypatch.setattr(launcher, 'shift_commits', lambda *a: [])
    windows = {777: CONT_SHIFT + ' shift', 888: 'other shift'}
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: windows.copy())
    closed = []

    def close(hwnd, *args):
        closed.append(hwnd.value)
        windows.pop(hwnd.value)
    monkeypatch.setattr(launcher.ctypes.windll.user32, 'PostMessageW', close)
    (d / 'claims.json').write_text(json.dumps({'a.py': {'role': 'Coder', 'time': 'old'}}))
    for name in ('claims.lock', 'bus.lock'):
        path = d / name
        path.write_text('')
        old = launcher.time.time() - launcher.FILE_LOCK_STALE_S - 1
        os.utime(path, (old, old))
    launcher.LOCKS_DIR.mkdir()
    repo_lock = launcher.lock_path(str(floor_world))
    repo_lock.write_text(json.dumps({'shift': d.name, 'cwd': str(floor_world), 'state_dir': str(d)}))
    first = launcher.cleaner([])
    assert first['windows_closed'] == [d.name] and closed == [777] and 888 in windows
    assert not repo_lock.exists() and json.loads((d / 'claims.json').read_text()) == {}
    assert not (d / 'claims.lock').exists() and not (d / 'bus.lock').exists()
    assert (d / 'handover.md').exists()
    assert _bus(d)[-1]['event'] == 'down'
    before = _floor_snapshot(floor_world)
    second = launcher.cleaner([])
    assert second['reaped'] == [] and _floor_snapshot(floor_world) == before
    capsys.readouterr()


@pytest.mark.parametrize('dry_run,failed', [(True, False), (False, False), (False, True)])
def test_recovery_reuses_dead_slot_and_reports_launch_failure_without_real_spawn(floor_world, monkeypatch,
                                                                               dry_run, failed):
    d, _ = _floor_shift(floor_world, 'recover')
    monkeypatch.setattr(launcher, 'read_slot', lambda *a: {'role': 'Coder'})
    monkeypatch.setattr(launcher, 'slot_state', lambda *a: 'dead')
    calls = []

    def fake_spawn(argv):
        calls.append(argv)
        if failed:
            raise SystemExit('no shell remains')
    monkeypatch.setattr(launcher, 'spawn', fake_spawn)
    report = launcher.recover_pane(d, 3, dry_run=dry_run)
    assert calls == ([] if dry_run else [[d.name, '--role', 'Coder', '--slot', '3']])
    assert report['recovered'] is (not dry_run and not failed)
    if failed:
        assert report['error'] == 'no shell remains'


@pytest.mark.usefixtures('_container_isolated')
def test_codex_real_popen_type_schedules_rename_on_daemon_thread(monkeypatch, tmp_path):
    d = _container(tmp_path, members=('Foreman', 'Coder'), free=(3, 4))
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    crew['members'][1]['tool'] = 'codex'
    (d / 'crew.json').write_text(json.dumps(crew), encoding='utf-8')
    events = []

    class FakePopen:
        pid = 42

        def __init__(self, *a, **k):
            pass

        def poll(self):
            return None

        def wait(self):
            events.append('wait')
            return 0

    class FakeThread:
        def __init__(self, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            events.append('thread')
            self.target()

    monkeypatch.setattr(launcher.subprocess, 'Popen', FakePopen)
    monkeypatch.setattr(launcher.threading, 'Thread', FakeThread)
    monkeypatch.setattr(launcher, 'set_codex_role_name', lambda *a: events.append('rename') or True)
    monkeypatch.setattr(launcher, 'register_name', lambda *a: None)
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: None)
    monkeypatch.setattr(launcher, 'process_running', lambda *a, **k: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane('Coder', str(d), start=True, cwd=str(tmp_path), slot=2)
    assert events == ['thread', 'rename', 'wait']


@pytest.mark.usefixtures('_container_isolated')
def test_spawned_copilot_apprentice_pane_runs_with_reasoning_effort_and_context(tmp_path, injected, monkeypatch,
                                                                               capsys):
    # spawn carries reasoning/context into crew.json, and pane() passes them to the copilot argv it launches
    d = _container(tmp_path)
    launcher.spawn([CONT_SHIFT, '--role', 'Apprentice', '--crew', 'copilot'])
    member = next(m for m in json.loads((d / 'crew.json').read_text(encoding='utf-8'))['members']
                  if m['label'] == 'Apprentice')
    assert (member['tool'], member['model'], member['reasoning'], member['context']) == \
        ('copilot', 'gemini-3.8-flash', 'high', 'long_context')
    seen = {}
    renames = []

    class Child:
        pid = 42

        def wait(self):
            return 0

        def poll(self):
            return None

    def popen(command, **kw):
        seen.update(command=command, env=kw.get('env'))
        return Child()

    monkeypatch.setattr(launcher.subprocess, 'Popen', popen)
    monkeypatch.setattr(launcher, 'register_name', lambda pid, name: None)
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: None)
    monkeypatch.setattr(launcher, 'set_codex_role_name', lambda *a, **k: renames.append(a) or True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    monkeypatch.setattr(launcher, 'process_running', lambda pid, started=None: True)
    launcher.pane('Apprentice', str(d), start=True, cwd=str(tmp_path), slot=3)
    command = seen['command']
    assert command[0] == str(launcher.COPILOT_EXE)
    assert member['label'] == 'Apprentice' and member['identity'] == f'{CONT_SHIFT}-apprentice'
    assert command[command.index('--model'):command.index('-i')] == [
        '--model', 'gemini-3.8-flash', '--reasoning-effort', 'high', '--context', 'long_context',
        '--name', f"Apprentice ({member['identity']})"]
    assert not any('/rename' in str(arg) for arg in command) and renames == []
    assert seen['env']['COPILOT_DISABLE_TERMINAL_TITLE'] == '1'
    assert seen['env']['COPILOT_SETUP_TERMINAL'] == 'false'


@pytest.mark.usefixtures('_container_isolated')
def test_pane_names_copilot_by_pane_label_and_does_not_inject_rename(monkeypatch, tmp_path):
    """Coder-2 must be named from the pane label, not the role stem, and must not get Codex /rename."""
    d = _container(tmp_path)
    saved = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    member = _member(CONT_SHIFT, 'Coder', label='Coder-2', colour='purple', tool='copilot',
                     model='gemini-3.8-flash')
    member.update(reasoning='high', context='long_context')
    saved['members'].append(member)
    (d / 'crew.json').write_text(json.dumps(saved), encoding='utf-8')
    seen = {'backgrounds': []}
    renames = []

    class Child:
        pid = 42

        def wait(self):
            return 0

        def poll(self):
            return None

    def popen(command, **kw):
        seen.update(command=command, env=kw.get('env'))
        return Child()

    monkeypatch.setattr(launcher.subprocess, 'Popen', popen)
    monkeypatch.setattr(launcher, 'register_name', lambda *a: None)
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: None)
    monkeypatch.setattr(launcher, 'set_codex_role_name', lambda *a, **k: renames.append(a) or True)
    monkeypatch.setattr(launcher, 'set_pane_background', lambda colour: seen['backgrounds'].append(colour))
    monkeypatch.setattr(launcher, 'process_running', lambda *a, **k: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane('Coder-2', str(d), start=True, cwd=str(tmp_path), slot=3)
    command = seen['command']
    assert command[command.index('--model'):command.index('-i')] == [
        '--model', 'gemini-3.8-flash', '--reasoning-effort', 'high', '--context', 'long_context',
        '--name', f'Coder-2 ({CONT_SHIFT}-coder-2)']
    assert not any('/rename' in str(arg) for arg in command) and renames == []
    assert seen['backgrounds'] == [launcher.ROLE_BACKGROUNDS['purple'], '#0C0C0C']
    assert seen['env']['COPILOT_DISABLE_TERMINAL_TITLE'] == '1'
    assert seen['env']['COPILOT_SETUP_TERMINAL'] == 'false'
    assert 'CODEX_HOME' not in seen['env']


# NGH-030: context is disposable for members; Foreman owns safe recovery.
@pytest.mark.parametrize('role,brief_file', [
    ('Coder', 'coder.md'), ('Coder', 'coder-cli.md'), ('Coder', 'coder-agy.md'),
    ('Tester', 'tester.md'), ('Tester', 'tester-cli.md'),
    ('Advisor', 'advisor.md'), ('Advisor', 'advisor-cli.md'),
    ('Apprentice', 'apprentice.md'),
])
def test_member_context_policy_bans_compaction_and_requires_persisted_recovery(role, brief_file):
    brief = launcher.role_brief(role, f'S-{role.lower()}', 'S-foreman',
                                'C:/state with spaces/S', brief_file).lower()
    assert re.search(r'never (?:invoke|run|use)\s+`?/compact', brief)
    assert 'automatic compaction' in brief
    assert 'context is exhausted' in brief or 'context exhaustion' in brief
    for recovery in ('foreman', 'same slot', 'handover.md', 'mailbox/inbox', 'unfinished assignment'):
        assert recovery in brief, (brief_file, recovery)


@pytest.mark.parametrize('brief_file', ['foreman.md', 'foreman-cli.md', 'foreman-agy-coder.md'])
def test_foreman_compaction_is_permitted_only_with_ordered_handover_and_recovery(brief_file):
    brief = launcher.role_brief('Foreman', 'S-foreman', 'S-coder, S-tester',
                                'C:/state with spaces/S', brief_file).lower()
    assert not re.search(r'never (?:invoke|run|use)\s+`?/compact', brief)
    before, after = brief.split('before any compaction', 1)[1].split('after compaction', 1)
    assert before.index('handover.md') < before.index('mailbox/bus note')
    for saved in ('current assignments', 'acked', 'unfinished work', 'waits'):
        assert saved in before, (brief_file, saved)
    positions = [after.index(item) for item in
                 ('ticket.txt', 'prompt.txt', 'requires.txt', 'result ladder', 'claims', 'inbox', 'handover.md')]
    assert positions == sorted(positions)
    assert after.index('handover.md') < after.index('only then continue')


def _capture_context_launch(monkeypatch, state, label, *, start=True, live=False, cwd=None, slot=None):
    """Capture the actual Popen boundary, never start a member or a terminal."""
    seen = {}

    class Child:
        pid = 42

        def wait(self):
            return 0

        def poll(self):
            return None

    monkeypatch.setattr(launcher.subprocess, 'Popen',
                        lambda command, **kw: seen.update(command=command, **kw) or Child())
    monkeypatch.setattr(launcher.subprocess, 'run',
                        lambda *a, **kw: pytest.fail('unexpected subprocess.run'))
    for name in ('accept_trust_prompt', 'register_name', 'set_codex_role_name',
                 'set_pane_background'):
        monkeypatch.setattr(launcher, name, lambda *a, **kw: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda *a: True)
    monkeypatch.setattr(launcher, 'inject_command', lambda pid, message, **kw: seen.update(opening=message) or True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane(label, str(state), start=start, live=live, cwd=str(cwd or state.parent), slot=slot)
    return seen


@pytest.mark.parametrize('label,base_role', [
    ('Coder', 'Coder'), ('Tester', 'Tester'), ('Advisor', 'Advisor'), ('Apprentice', 'Apprentice'),
    ('Coder-2', 'Coder'), ('Foreman', 'Coder'), ('Captain', 'Foreman'), ('Foreman', 'Foreman'),
])
@pytest.mark.parametrize('inherited', [None, '0', '1'])
def test_claude_context_control_uses_base_role_and_is_child_only(monkeypatch, tmp_path,
                                                              label, base_role, inherited):
    state = tmp_path / 'state with spaces' / 'S'
    state.mkdir(parents=True)
    member = _member('S', base_role, label=label, human=base_role == 'Foreman')
    (state / 'crew.json').write_text(json.dumps({'crew': 'default', 'members': [member]}), encoding='utf-8')
    (state / 'prompt.txt').write_text('THE TASK', encoding='utf-8')
    if inherited is None:
        monkeypatch.delenv('DISABLE_AUTO_COMPACT', raising=False)
    else:
        monkeypatch.setenv('DISABLE_AUTO_COMPACT', inherited)
    monkeypatch.setenv('DISABLE_COMPACT', '1')
    before = dict(os.environ)
    seen = _capture_context_launch(monkeypatch, state, label)
    if base_role == 'Foreman':
        assert seen['env'].get('DISABLE_AUTO_COMPACT') in (None, '0')
        assert seen['env'].get('DISABLE_COMPACT') in (None, '0')
    else:
        assert seen['env']['DISABLE_AUTO_COMPACT'] == '1'
    assert dict(os.environ) == before
    assert seen['env'] is not os.environ
    assert seen['cwd'] == str(state.parent)


@pytest.mark.parametrize('role', ['Foreman', 'Coder'])
def test_live_communication_route_also_scopes_claude_compaction(monkeypatch, tmp_path, role):
    state = tmp_path / 'S'
    state.mkdir()
    monkeypatch.delenv('DISABLE_AUTO_COMPACT', raising=False)
    seen = _capture_context_launch(monkeypatch, state, role, start=False, live=True)
    assert (seen['env'].get('DISABLE_AUTO_COMPACT') == '1') is (role != 'Foreman')
    assert 'DISABLE_AUTO_COMPACT' not in os.environ


@pytest.mark.parametrize('tool', ['codex', 'copilot', 'agy'])
@pytest.mark.parametrize('role', ['Foreman', 'Coder', 'Tester', 'Advisor', 'Apprentice'])
def test_unverified_tools_report_limitations_without_inventing_disable_flags(monkeypatch, tmp_path, tool, role):
    state = tmp_path / 'S'
    state.mkdir()
    member = _member('S', role, tool=tool, human=role == 'Foreman')
    (state / 'crew.json').write_text(json.dumps({'crew': 'default', 'members': [member]}), encoding='utf-8')
    (state / 'prompt.txt').write_text('THE TASK', encoding='utf-8')
    for key in ('DISABLE_AUTO_COMPACT', 'DISABLE_COMPACT'):
        monkeypatch.delenv(key, raising=False)
    before = dict(os.environ)
    control = launcher.compaction_control(tool, role)
    assert control['automatic'] == ('permitted' if role == 'Foreman' else 'unsupported')
    assert control['environment'] == {}
    if role != 'Foreman':
        assert control['reason']
    seen = _capture_context_launch(monkeypatch, state, role)
    assert not any('compact' in arg.lower() for arg in seen['command'][:-1])
    assert 'DISABLE_AUTO_COMPACT' not in seen['env'] and 'DISABLE_COMPACT' not in seen['env']
    assert dict(os.environ) == before


@pytest.mark.parametrize('crew_name', sorted(p.stem for p in launcher.CREWS_DIR.glob('*.yaml')))
def test_dry_run_reports_actual_role_controls_for_every_installed_crew(monkeypatch, tmp_path, capsys, crew_name):
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'unwritten shifts')
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **k: pytest.fail('dry-run launched a process'))
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k: pytest.fail('dry-run ran a command'))
    before = dict(os.environ)
    launcher.start(['--dry-run', '--no-worktree', '--cwd', str(tmp_path), '--crew', crew_name, 'context policy probe'])
    plan = json.loads(capsys.readouterr().out)
    members = launcher.crew_members(launcher.load_crew(crew_name), plan['shift'])
    for member in members:
        control = plan['roles'][member['label']]['compaction']
        if member['role'] == 'Foreman':
            assert control['automatic'] == 'permitted'
            expected = {'DISABLE_AUTO_COMPACT': '0', 'DISABLE_COMPACT': '0'} if member['tool'] == 'claude' else {}
        elif member['tool'] == 'claude':
            assert control['automatic'] == 'disabled'
            expected = {'DISABLE_AUTO_COMPACT': '1'}
        else:
            assert control['automatic'] == 'unsupported' and control['reason']
            expected = {}
        assert control['environment'] == expected
        actual_env = launcher.crew_env({}, tool=member['tool'], role=member['role'])
        assert all(actual_env[k] == v for k, v in expected.items())
    assert not (tmp_path / 'unwritten shifts').exists()
    assert dict(os.environ) == before


@pytest.mark.usefixtures('_container_isolated')
@pytest.mark.parametrize('tool', ['claude', 'codex', 'copilot', 'agy'])
def test_context_replacement_reuses_slot_and_recovers_acknowledged_unfinished_assignment(
        monkeypatch, tmp_path, injected, capsys, tool):
    d = _container(tmp_path, members=('Foreman', 'Advisor', 'Coder', 'Tester', 'Apprentice'),
                   slots=8, free=(6, 7, 8))
    saved = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    member = saved['members'][-1]
    member.update(tool=tool, model='original-model', brief='apprentice.md', persona='apprentice.yaml', colour='yellow',
                  reasoning='high', context='long_context', account=2, permissions='default')
    (d / 'crew.json').write_text(json.dumps(saved), encoding='utf-8')
    (d / 'handover.md').write_text('unfinished recovery-task; token RECOVER-42', encoding='utf-8')
    launcher.msg(['send', d.name, 'Foreman', 'Apprentice', '--kind', 'assignment', '--asg', 'recovery-task',
                  '--body', 'Read token from handover, then wait.'])
    launcher.ack([d.name, 'Apprentice', '1'])
    capsys.readouterr()
    monkeypatch.setattr(launcher, 'pid_alive', lambda pid, started=None: pid != 505)
    original_slots = {n: launcher.read_slot(d, n) for n in range(1, 9)}
    launcher.spawn([d.name, '--role', 'Apprentice'])
    spawn = json.loads(capsys.readouterr().out)
    assert spawn['slot'] == 5 and spawn['tool'] == tool
    assert injected[0][3:5] == ['2505', '1']
    assert '--slot 5' in injected.inputs[0].decode('utf-8')
    after = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    replacement = next(m for m in after['members'] if m['label'] == 'Apprentice')
    assert len(after['members']) == 5 and after['slots'] == 8
    assert replacement == {**member, 'replacement': True}
    for n in (1, 2, 3, 4, 6, 7, 8):
        assert launcher.read_slot(d, n) == original_slots[n]
    seen = _capture_context_launch(monkeypatch, d, 'Apprentice', slot=5)
    prompt = seen['opening'] if tool == 'claude' else seen['command'][-1]
    if tool == 'claude':
        assert prompt not in seen['command']  # recovery is sent only after colour confirmation.
    assert f'"{d.as_posix()}/handover.md"' in prompt
    assert f'inbox {d.name} Apprentice' in prompt
    for recovery in ('replacement incarnation', 'active_assignments', 'stale', 're-dispatched',
                     'unfinished assignment', 'READY'):
        assert recovery in prompt
    info = launcher.member_info(d, 'Apprentice')
    assert info['pid'] == 42 and info['started'] != 1.0
    assert info['history'][-1]['pid'] == 505 and info['history'][-1]['started'] == 1.0
    assert launcher.read_slot(d, 5)['cmd_pid'] == 2505
    assert launcher.read_slot(d, 5)['role'] == 'Apprentice'
    if tool == 'claude':
        assert seen['env']['DISABLE_AUTO_COMPACT'] == '1'
    view = launcher.inbox_view(d, 'Apprentice')
    assert view['inbound_unacked'] == []
    assert [(a['asg'], a['state'], a['stale']) for a in view['active_assignments']] == [
        ('recovery-task', 'acknowledged', True)]
    with pytest.raises(launcher.ProtocolViolation) as err:
        launcher.ack([d.name, 'Apprentice', '1'])
    assert err.value.code == 5
    launcher.msg(['send', d.name, 'Foreman', 'Apprentice', '--kind', 'assignment', '--asg', 'recovery-task',
                  '--body', 'Resume from handover: RECOVER-42'])
    current = launcher.inbox_view(d, 'Apprentice')['inbound_unacked']
    assert len(current) == 1 and current[0]['recipient_incarnation'] == info['started']
    launcher.ack([d.name, 'Apprentice', str(current[0]['id'])])
    launcher.msg(['send', d.name, 'Apprentice', 'Foreman', '--kind', 'result', '--asg', 'recovery-task',
                  '--body', 'Completed RECOVER-42'])
    assert launcher.inbox_view(d, 'Foreman')['active_assignments'] == []
    assert (d / 'handover.md').read_text(encoding='utf-8') == 'unfinished recovery-task; token RECOVER-42'


def test_first_incarnation_keeps_ready_opening_without_recovery_instructions():
    member = _member('S', 'Apprentice')
    assert launcher.opening_message(member, 'C:/state/S') == launcher.START_MESSAGES['Apprentice']


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_transports_quoted_unicode_windows_paths_only_on_utf8_stdin(monkeypatch, tmp_path, injected, capsys):
    d = _container(tmp_path / 'state with spaces & café')
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', d.parent)
    saved = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    saved['cwd'] = r'C:\repo with spaces & café\worktree'
    (d / 'crew.json').write_text(json.dumps(saved), encoding='utf-8')
    launcher.spawn([d.name, '--role', 'Tester'])
    assert injected[0] == ['agents-cli', '_attach', 'inject', '2503', '1']
    command = injected.inputs[0].decode('utf-8')
    assert f'--state-dir "{d}"' in command
    assert '--cwd "C:\\repo with spaces & café\\worktree"' in command
    assert command.startswith(f'"{sys.executable}" "{Path(launcher.__file__).resolve()}" ')
    assert json.loads(capsys.readouterr().out)['slot'] == 3


@pytest.mark.usefixtures('_container_isolated')
@pytest.mark.parametrize('returncode,stdout,stderr,reason', [
    (1, b'{"error":"inject needs text on stdin"}', b'', 'inject needs text on stdin'),
    (0, b'{"error":{"message":"WriteConsoleInput failed"}}', b'', 'WriteConsoleInput failed'),
    (1, b'stdout diagnosis', b'stderr diagnosis', 'stdout diagnosis\nstderr diagnosis'),
    (1, b'', b'', 'exit 1'),
])
def test_spawn_helper_error_preserves_diagnostics_and_rolls_back_all_state(
        tmp_path, injected, returncode, stdout, stderr, reason):
    d = _container(tmp_path)
    before = _floor_snapshot(tmp_path)
    injected.rc, injected.out, injected.err = returncode, stdout, stderr
    with pytest.raises(SystemExit) as err:
        launcher.spawn([d.name, '--role', 'Tester'])
    assert reason in str(err.value)
    assert _floor_snapshot(tmp_path) == before


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_dry_run_copilot_budget_share_command_and_leaves_crew_unchanged(tmp_path, injected, capsys):
    # 1) budget 60 with Copilot Apprentice and Tester at 30 each, Tester gone:
    # spawn --dry-run --role Tester (copilot) command contains --max-ai-credits 30, and crew.json is unchanged
    d = _container(tmp_path, members=('Foreman', 'Apprentice'), free=(3,))
    foreman = _member(CONT_SHIFT, 'Foreman', human=True, colour='red')
    apprentice = _member(CONT_SHIFT, 'Apprentice', human=False, colour='yellow')
    apprentice.update(tool='copilot', model='gemini-3.8-flash', max_credits=30)
    tester = _member(CONT_SHIFT, 'Tester', human=False, colour='green')
    tester.update(tool='copilot', model='gemini-3.8-flash', max_credits=30)
    crew_data = {'crew': 'default', 'members': [foreman, apprentice, tester], 'slots': 4,
                 'permission_mode': 'auto', 'model': 'claude-opus-5-5', 'cwd': 'C:/git/proj',
                 'max_credits': 60}
    crew_file = d / 'crew.json'
    crew_file.write_text(json.dumps(crew_data, indent=2), encoding='utf-8')
    before_crew = crew_file.read_text(encoding='utf-8')

    launcher.spawn([CONT_SHIFT, '--role', 'Tester', '--dry-run'])
    out = json.loads(capsys.readouterr().out)
    assert out['dry_run'] is True
    assert out['role'] == 'Tester'
    assert out['tool'] == 'copilot'
    assert '--max-ai-credits' in out['command']
    idx = out['command'].index('--max-ai-credits')
    assert out['command'][idx + 1] == '30'
    assert crew_file.read_text(encoding='utf-8') == before_crew
    assert len(injected) == 0


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_dry_run_refuses_when_no_budget_share_left(tmp_path, injected):
    # 2) same shift, spawn --dry-run --role Coder2 --cli copilot raises SystemExit "no budget share left"
    d = _container(tmp_path, members=('Foreman', 'Apprentice'), free=(3,))
    foreman = _member(CONT_SHIFT, 'Foreman', human=True, colour='red')
    apprentice = _member(CONT_SHIFT, 'Apprentice', human=False, colour='yellow')
    apprentice.update(tool='copilot', model='gemini-3.8-flash', max_credits=30)
    tester = _member(CONT_SHIFT, 'Tester', human=False, colour='green')
    tester.update(tool='copilot', model='gemini-3.8-flash', max_credits=30)
    crew_data = {'crew': 'default', 'members': [foreman, apprentice, tester], 'slots': 4,
                 'permission_mode': 'auto', 'model': 'claude-opus-5-5', 'cwd': 'C:/git/proj',
                 'max_credits': 60}
    (d / 'crew.json').write_text(json.dumps(crew_data, indent=2), encoding='utf-8')

    with pytest.raises(SystemExit) as err:
        launcher.spawn([CONT_SHIFT, '--role', 'Coder2', '--cli', 'copilot', '--dry-run'])
    assert 'no budget share left' in str(err.value)


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_dry_run_replaces_claude_tester_with_copilot_getting_leftover_share(tmp_path, injected, capsys):
    # 3) budget 60, one copilot pane at 30 and the Tester replaced by claude:
    # spawn --dry-run --role Tester --cli copilot gets 30
    d = _container(tmp_path, members=('Foreman', 'Apprentice'), free=(3,))
    foreman = _member(CONT_SHIFT, 'Foreman', human=True, colour='red')
    apprentice = _member(CONT_SHIFT, 'Apprentice', human=False, colour='yellow')
    apprentice.update(tool='copilot', model='gemini-3.8-flash', max_credits=30)
    tester = _member(CONT_SHIFT, 'Tester', human=False, colour='green')
    tester.update(tool='claude', model='claude-opus-5-5')
    crew_data = {'crew': 'default', 'members': [foreman, apprentice, tester], 'slots': 4,
                 'permission_mode': 'auto', 'model': 'claude-opus-5-5', 'cwd': 'C:/git/proj',
                 'max_credits': 60}
    (d / 'crew.json').write_text(json.dumps(crew_data, indent=2), encoding='utf-8')

    launcher.spawn([CONT_SHIFT, '--role', 'Tester', '--cli', 'copilot', '--dry-run'])
    out = json.loads(capsys.readouterr().out)
    assert out['dry_run'] is True
    assert out['tool'] == 'copilot'
    assert out['member']['max_credits'] == 30
    idx = out['command'].index('--max-ai-credits')
    assert out['command'][idx + 1] == '30'


@pytest.mark.usefixtures('_container_isolated')
def test_spawn_claude_on_budgeted_shift_carries_no_max_credits(tmp_path, injected, capsys):
    # 4) a claude spawn on a budgeted shift has no max_credits
    d = _container(tmp_path, members=('Foreman', 'Apprentice'), free=(3,))
    foreman = _member(CONT_SHIFT, 'Foreman', human=True, colour='red')
    apprentice = _member(CONT_SHIFT, 'Apprentice', human=False, colour='yellow')
    apprentice.update(tool='copilot', model='gemini-3.8-flash', max_credits=30)
    crew_data = {'crew': 'default', 'members': [foreman, apprentice], 'slots': 4,
                 'permission_mode': 'auto', 'model': 'claude-opus-5-5', 'cwd': 'C:/git/proj',
                 'max_credits': 60}
    (d / 'crew.json').write_text(json.dumps(crew_data, indent=2), encoding='utf-8')

    launcher.spawn([CONT_SHIFT, '--role', 'Tester', '--cli', 'claude', '--dry-run'])
    out = json.loads(capsys.readouterr().out)
    assert out['dry_run'] is True
    assert out['tool'] == 'claude'
    assert 'max_credits' not in out['member']


# NGH-040: real Git integration; only the crew/window launch boundary is mocked.

def _wt_git(repo, *args, check=True):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                            encoding='utf-8', errors='replace', timeout=30)
    if check:
        assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip() if check else result


def _wt_commit(repo, path, text, message='test change'):
    target = Path(repo) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8')
    _wt_git(repo, 'add', '--', path)
    _wt_git(repo, 'commit', '-m', message)
    return _wt_git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def worktree_world(monkeypatch, tmp_path):
    root = tmp_path / 'floor with spaces'
    for name, suffix in [('SOLID_DIR', ''), ('SHIFTS_DIR', 'shifts'),
                         ('WORKTREES_DIR', 'worktrees'), ('LOCKS_DIR', 'locks'),
                         ('BUILDS_DIR', 'builds'), ('FLOOR_FILE', 'floor.json'),
                         ('LEGACY_DIR', 'legacy')]:
        monkeypatch.setattr(launcher, name, root / suffix, raising=False)
    launches = []
    monkeypatch.setattr(launcher, 'install_profile', lambda *a: None)
    monkeypatch.setattr(launcher, 'launch', lambda *a: launches.append(a))
    monkeypatch.setattr(launcher, 'terminal_windows', lambda: {})
    monkeypatch.setattr(launcher, 'close_target', lambda *a: None)
    monkeypatch.setattr(launcher, 'own_ancestors', lambda: set())
    monkeypatch.setattr(launcher, 'pid_alive', lambda *a: False)
    monkeypatch.setattr(launcher, 'process_start_epoch', lambda pid: 123.0)
    repo = tmp_path / 'repo with spaces and café'
    repo.mkdir()
    _wt_git(repo, 'init', '-b', 'main')
    for key, value in [('user.name', 'Worktree Tester'), ('user.email', 'tester@example.invalid'),
                       ('commit.gpgsign', 'false'), ('core.autocrlf', 'false'),
                       ('core.hooksPath', str(tmp_path / 'no-hooks'))]:
        _wt_git(repo, 'config', key, value)
    _wt_commit(repo, 'shared.txt', 'original\n', 'initial')
    _wt_commit(repo, 'notes.txt', 'notes\n', 'notes')
    return types.SimpleNamespace(root=root, repo=repo, launches=launches)


def _wt_start(world, *args):
    state = launcher.start(['--cwd', str(world.repo), 'Ticket NGH-040: verify isolation', *args])
    meta = json.loads((state / 'worktree.json').read_text(encoding='utf-8'))
    return state, Path(meta['worktree_dir']), meta


def _wt_test_commit(state, work, capsys):
    # Exercise the real build wrapper/evidence schema, without invoking another suite.
    launcher.build([state.name, '--cwd', str(work), '--cmd',
                    subprocess.list2cmdline([sys.executable, '-c', 'print(42)'])])
    record = json.loads(capsys.readouterr().out)
    assert record['exit'] == 0 and record['dirty'] is False
    assert record['head'] == _wt_git(work, 'rev-parse', 'HEAD')
    return record


def _wt_land(state, capsys, expected=0, *flags):
    capsys.readouterr()
    if expected:
        with pytest.raises(SystemExit) as err:
            launcher.land([state.name, *flags])
        assert err.value.code == expected
    else:
        launcher.land([state.name, *flags])
    return json.loads(capsys.readouterr().out)


def test_worktree_start_records_metadata_and_all_slot_cwds(worktree_world, capsys):
    w = worktree_world
    base = _wt_git(w.repo, 'rev-parse', 'main')
    nested = w.repo / 'nested'
    nested.mkdir()
    state = launcher.start(['--cwd', str(nested), 'Ticket NGH-040: nested cwd'])
    meta = json.loads((state / 'worktree.json').read_text(encoding='utf-8'))
    work = Path(meta['worktree_dir'])
    assert Path(meta['repo_root']) == w.repo.resolve()
    assert work == launcher.WORKTREES_DIR / state.name
    assert meta['target_branch'] == 'main' and meta['base_sha'] == base and meta['created_at']
    assert re.fullmatch(r'shift/NGH-040-[a-f0-9]+', meta['branch'])
    assert _wt_git(work, 'branch', '--show-current') == meta['branch']
    assert _wt_git(work, 'rev-parse', 'HEAD') == base
    assert json.loads((state / 'crew.json').read_text())['cwd'] == str(work)
    argv = w.launches[-1][0]
    assert argv.count('--cwd') == 5   # crew wrappers take --cwd; all eight WT slots take -d
    assert argv.count('-d') == 8
    assert all(argv[i + 1] == str(work) for i, token in enumerate(argv) if token == '-d')
    assert all(argv[i + 1] == str(work) for i, token in enumerate(argv) if token == '--cwd')
    assert not launcher.lock_path(str(w.repo)).exists()
    # Parent remains untouched even though the nested source folder is untracked.
    assert _wt_git(w.repo, 'rev-parse', 'HEAD') == base


def test_worktree_dry_run_has_no_git_or_state_mutations(worktree_world, capsys):
    w = worktree_world
    before = _wt_git(w.repo, 'worktree', 'list', '--porcelain')
    branches = _wt_git(w.repo, 'branch', '--list')
    launcher.start(['--cwd', str(w.repo), '--dry-run', 'Ticket NGH-040: dry run'])
    plan = json.loads(capsys.readouterr().out)
    assert Path(plan['cwd']).parent == launcher.WORKTREES_DIR
    assert str(w.repo) != plan['cwd']
    assert _wt_git(w.repo, 'worktree', 'list', '--porcelain') == before
    assert _wt_git(w.repo, 'branch', '--list') == branches
    assert not launcher.SHIFTS_DIR.exists() and not launcher.WORKTREES_DIR.exists()
    assert not w.launches


def test_worktree_opt_out_and_non_git_keep_in_place_locks(worktree_world, tmp_path):
    w = worktree_world
    state = launcher.start(['--cwd', str(w.repo), '--no-worktree', 'in place'])
    assert not (state / 'worktree.json').exists()
    assert json.loads((state / 'crew.json').read_text())['cwd'] == str(w.repo)
    assert launcher.lock_path(str(w.repo)).exists()
    plain = tmp_path / 'plain folder'
    plain.mkdir()
    state = launcher.start(['--cwd', str(plain), 'non git'])
    assert not (state / 'worktree.json').exists()
    assert launcher.lock_path(str(plain)).exists()


def test_worktree_two_shifts_are_isolated_with_local_claim_conflicts(worktree_world, capsys):
    w = worktree_world
    one, work1, _ = _wt_start(w)
    two, work2, _ = _wt_start(w)
    assert work1 != work2
    (work1 / 'shared.txt').write_text('only one\n')
    assert (work2 / 'shared.txt').read_text() == 'original\n'
    assert (w.repo / 'shared.txt').read_text() == 'original\n'
    # Even an identical absolute claim is shift-local for isolated worktrees.
    path = str(work1 / 'shared.txt')
    launcher.claim([one.name, 'Coder', path])
    launcher.claim([two.name, 'Coder', path])
    before = (one / 'claims.json').read_bytes()
    with pytest.raises(launcher.FileClaimed) as err:
        launcher.claim([one.name, 'Tester', path])
    assert err.value.code == 4 and (one / 'claims.json').read_bytes() == before


def test_worktree_land_fast_forward_preserves_unrelated_staged_and_untracked_notes(worktree_world, capsys):
    w = worktree_world
    state, work, meta = _wt_start(w)
    sha = _wt_commit(work, 'shared.txt', 'landed\n')
    _wt_test_commit(state, work, capsys)
    (w.repo / 'notes.txt').write_text('staged notes\n')
    _wt_git(w.repo, 'add', 'notes.txt')
    (w.repo / 'private notes.txt').write_text('private\n')
    before = _wt_git(w.repo, 'status', '--porcelain')
    out = _wt_land(state, capsys)
    assert out == {'status': 'landed', 'sha': sha}
    assert _wt_git(w.repo, 'rev-parse', 'main') == sha
    assert _wt_git(w.repo, 'status', '--porcelain') == before
    assert (w.repo / 'notes.txt').read_text() == 'staged notes\n'
    assert (w.repo / 'private notes.txt').read_text() == 'private\n'
    assert (w.repo / 'shared.txt').read_text() == 'landed\n'
    landed = json.loads((state / 'landed.json').read_text())
    assert landed['landed_sha'] == sha and landed['base_sha'] == meta['base_sha']
    events = [json.loads(line) for line in (state / 'bus.jsonl').read_text().splitlines()]
    assert any(event['event'] == 'land' for event in events)
    assert not launcher.lock_path(str(w.repo)).exists()


@pytest.mark.parametrize('staged', [False, True])
def test_worktree_land_refuses_overlapping_parent_dirty_file(worktree_world, capsys, staged):
    w = worktree_world
    state, work, _ = _wt_start(w)
    _wt_commit(work, 'shared.txt', 'crew edit\n')
    _wt_test_commit(state, work, capsys)
    base = _wt_git(w.repo, 'rev-parse', 'HEAD')
    (w.repo / 'shared.txt').write_text('owner edit\n')
    if staged:
        _wt_git(w.repo, 'add', 'shared.txt')
    before = _wt_git(w.repo, 'status', '--porcelain')
    with pytest.raises(SystemExit):
        launcher.land([state.name])
    assert _wt_git(w.repo, 'rev-parse', 'HEAD') == base
    assert _wt_git(w.repo, 'status', '--porcelain') == before
    assert (w.repo / 'shared.txt').read_text() == 'owner edit\n'
    assert not (state / 'landed.json').exists()
    assert not launcher.lock_path(str(w.repo)).exists()


def test_worktree_land_rebase_requires_fresh_clean_build_before_second_land(worktree_world, capsys):
    w = worktree_world
    state, work, _ = _wt_start(w)
    old = _wt_commit(work, 'crew.txt', 'crew\n')
    _wt_test_commit(state, work, capsys)
    target = _wt_commit(w.repo, 'owner.txt', 'owner\n')
    out = _wt_land(state, capsys, 2)
    assert out['status'] == 'rebased_needs_test' and out['new_base'] == target
    rebased = _wt_git(work, 'rev-parse', 'HEAD')
    assert out['head'] == rebased and rebased != old
    assert _wt_git(w.repo, 'rev-parse', 'HEAD') == target
    assert not (state / 'landed.json').exists()
    assert not launcher.lock_path(str(w.repo)).exists()
    with pytest.raises(SystemExit):
        launcher.land([state.name])
    assert _wt_git(w.repo, 'rev-parse', 'HEAD') == target
    _wt_test_commit(state, work, capsys)
    assert _wt_land(state, capsys)['sha'] == rebased
    assert (w.repo / 'owner.txt').read_text() == 'owner\n'
    assert (w.repo / 'crew.txt').read_text() == 'crew\n'


@pytest.mark.parametrize('keep', [False, True])
def test_worktree_land_conflict_reports_files_releases_lock_and_aborts_unless_kept(worktree_world, capsys, keep):
    w = worktree_world
    state, work, _ = _wt_start(w)
    old = _wt_commit(work, 'shared.txt', 'crew conflict\n')
    _wt_test_commit(state, work, capsys)
    target = _wt_commit(w.repo, 'shared.txt', 'owner conflict\n')
    flags = ['--keep-conflicts'] if keep else []
    out = _wt_land(state, capsys, 4, *flags)
    assert out['status'] == 'conflict' and out['target_head'] == target
    assert 'shared.txt' in out['files']
    assert _wt_git(w.repo, 'rev-parse', 'HEAD') == target
    assert not (state / 'landed.json').exists()
    assert not launcher.lock_path(str(w.repo)).exists()
    if keep:
        assert '<<<<<<<' in (work / 'shared.txt').read_text()
        assert 'shared.txt' in _wt_git(work, 'diff', '--name-only', '--diff-filter=U')
        _wt_git(work, 'rebase', '--abort')
    else:
        assert _wt_git(work, 'rev-parse', 'HEAD') == old
        assert (work / 'shared.txt').read_text() == 'crew conflict\n'
        assert not _wt_git(work, 'status', '--porcelain')


@pytest.mark.parametrize('state_kind', ['unbuilt', 'dirty', 'new_commit'])
def test_worktree_land_rejects_missing_or_stale_clean_evidence(worktree_world, capsys, state_kind):
    w = worktree_world
    state, work, _ = _wt_start(w)
    _wt_commit(work, 'crew.txt', 'crew\n')
    if state_kind != 'unbuilt':
        _wt_test_commit(state, work, capsys)
    if state_kind == 'dirty':
        (work / 'crew.txt').write_text('uncommitted\n')
    if state_kind == 'new_commit':
        _wt_commit(work, 'crew.txt', 'not tested\n')
    base = _wt_git(w.repo, 'rev-parse', 'HEAD')
    with pytest.raises(SystemExit):
        launcher.land([state.name])
    assert _wt_git(w.repo, 'rev-parse', 'HEAD') == base
    assert not (state / 'landed.json').exists()


@pytest.mark.parametrize('hold', [False, True])
def test_worktree_down_preserves_dirty_unlanded_work_in_recovery_branch_or_hold(worktree_world, capsys, hold):
    w = worktree_world
    state, work, meta = _wt_start(w)
    _wt_commit(work, 'crew.txt', 'committed work\n')
    (work / 'shared.txt').write_text('dirty recovery\n')
    (work / 'new recovery.txt').write_text('untracked recovery\n')
    launcher.down([state.name, *(['--hold'] if hold else [])])
    assert _wt_git(w.repo, 'rev-parse', '--verify', meta['branch'])
    if hold:
        assert work.is_dir() and (work / 'shared.txt').read_text() == 'dirty recovery\n'
        assert (work / 'new recovery.txt').read_text() == 'untracked recovery\n'
        recovery = json.loads((state / 'recovery.json').read_text())
        assert recovery['disposition'] == 'held' and recovery['cleanup_pending'] is False
        assert _wt_git(work, 'status', '--porcelain')   # hold did not commit dirty edits
    else:
        assert not work.exists()
        assert _wt_git(w.repo, 'show', f"{meta['branch']}:shared.txt") == 'dirty recovery'
        assert _wt_git(w.repo, 'show', f"{meta['branch']}:new recovery.txt") == 'untracked recovery'
    assert 'preserved_branch' in (state / 'handover.md').read_text()
    assert meta['branch'] in (state / 'handover.md').read_text()
    floor = json.loads(launcher.FLOOR_FILE.read_text())
    assert meta['branch'] in json.dumps(floor) and 'preserved_branch' in json.dumps(floor)


def test_worktree_down_removes_landed_worktree_and_branch(worktree_world, capsys):
    w = worktree_world
    state, work, meta = _wt_start(w)
    sha = _wt_commit(work, 'crew.txt', 'land me\n')
    _wt_test_commit(state, work, capsys)
    _wt_land(state, capsys)
    launcher.down([state.name])
    assert not work.exists()
    assert not _wt_git(w.repo, 'branch', '--list', meta['branch'])
    assert str(work) not in _wt_git(w.repo, 'worktree', 'list', '--porcelain')
    assert _wt_git(w.repo, 'rev-parse', 'main') == sha


def test_worktree_start_from_linked_checkout_resolves_canonical_repository(worktree_world):
    w = worktree_world
    _, linked, _ = _wt_start(w)
    state = launcher.start(['--cwd', str(linked), 'Ticket NGH-040: another shift'])
    meta = json.loads((state / 'worktree.json').read_text())
    assert Path(meta['repo_root']) == w.repo.resolve()
    assert Path(meta['worktree_dir']) != linked


def test_worktree_explicit_target_branch_uses_its_head(worktree_world):
    w = worktree_world
    _wt_git(w.repo, 'checkout', '-b', 'release')
    target = _wt_commit(w.repo, 'release.txt', 'release\n')
    _, work, meta = _wt_start(w, '--target-branch', 'release')
    assert meta['target_branch'] == 'release' and meta['base_sha'] == target
    assert _wt_git(work, 'rev-parse', 'HEAD') == target


def test_worktree_land_refuses_untracked_parent_collision(worktree_world, capsys):
    w = worktree_world
    state, work, _ = _wt_start(w)
    _wt_commit(work, 'new file.txt', 'crew file\n')
    _wt_test_commit(state, work, capsys)
    base = _wt_git(w.repo, 'rev-parse', 'HEAD')
    (w.repo / 'new file.txt').write_text('owner file\n')
    with pytest.raises(SystemExit):
        launcher.land([state.name])
    assert (w.repo / 'new file.txt').read_text() == 'owner file\n'
    assert _wt_git(w.repo, 'rev-parse', 'HEAD') == base
    assert not (state / 'landed.json').exists()


def test_worktree_latest_failed_build_invalidates_earlier_pass(worktree_world, capsys):
    w = worktree_world
    state, work, _ = _wt_start(w)
    _wt_commit(work, 'crew.txt', 'crew\n')
    _wt_test_commit(state, work, capsys)
    with pytest.raises(SystemExit) as err:
        launcher.build([state.name, '--cwd', str(work), '--cmd',
                        subprocess.list2cmdline([sys.executable, '-c', 'raise SystemExit(7)'])])
    assert err.value.code == 7
    with pytest.raises(SystemExit):
        launcher.land([state.name])
    assert not (state / 'landed.json').exists()


def test_worktree_concurrent_landings_use_exclusive_repository_lock(worktree_world, capsys, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    w = worktree_world
    one, work1, _ = _wt_start(w)
    two, work2, _ = _wt_start(w)
    _wt_commit(work1, 'one.txt', 'one\n')
    _wt_commit(work2, 'two.txt', 'two\n')
    _wt_test_commit(one, work1, capsys)
    _wt_test_commit(two, work2, capsys)
    monkeypatch.setattr(launcher, 'process_running', lambda *a: True)
    gate = threading.Barrier(2)
    original_state = launcher.git_state
    mutex = threading.Lock()
    active, maximum = 0, 0

    def checked_state(cwd):
        nonlocal active, maximum
        with mutex:
            active += 1
            maximum = max(maximum, active)
        try:
            # A short delay creates a real overlap opportunity after lock acquisition.
            launcher.time.sleep(0.15)
            return original_state(cwd)
        finally:
            with mutex:
                active -= 1

    monkeypatch.setattr(launcher, 'git_state', checked_state)

    def run(state):
        gate.wait(timeout=10)
        try:
            launcher.land([state.name])
            return 0
        except SystemExit as err:
            return err.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run, one), pool.submit(run, two)
        assert sorted([a.result(timeout=30), b.result(timeout=30)]) == [0, 2]
    assert maximum == 1
    assert not launcher.lock_path(str(w.repo)).exists()
    assert sum((state / 'landed.json').exists() for state in (one, two)) == 1


def test_worktree_cleaner_preserves_dead_shift_recovery_in_floor(worktree_world, capsys):
    w = worktree_world
    state, work, meta = _wt_start(w)
    (work / 'shared.txt').write_text('cleaner recovery\n')
    launcher.cleaner([])
    assert not work.exists()
    assert _wt_git(w.repo, 'show', f"{meta['branch']}:shared.txt") == 'cleaner recovery'
    assert meta['branch'] in (state / 'handover.md').read_text()
    floor = json.loads(launcher.FLOOR_FILE.read_text())
    assert floor['worktrees'][state.name]['preserved_branch'] == meta['branch']


@pytest.mark.skipif(os.name != 'nt', reason='Windows current-directory handle semantics')
def test_worktree_down_with_live_current_directory_preserves_teardown_progress(worktree_world, capsys):
    w = worktree_world
    state, work, meta = _wt_start(w)
    _wt_commit(work, 'crew.txt', 'land me\n')
    _wt_test_commit(state, work, capsys)
    _wt_land(state, capsys)
    child = subprocess.Popen([sys.executable, '-u', '-c',
                              'import sys; print("ready", flush=True); sys.stdin.read()'],
                             cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        # A held cwd must not abort down before it records recovery/down state.
        launcher.down([state.name])
        events = [json.loads(line) for line in (state / 'bus.jsonl').read_text().splitlines()]
        assert events[-1]['event'] == 'down'
        recovery = json.loads((state / 'recovery.json').read_text())
        assert recovery['cleanup_pending'] is True and recovery['error']
        assert recovery['disposition'] == 'landed'
        floor = json.loads(launcher.FLOOR_FILE.read_text())
        assert floor['worktrees'][state.name] == recovery
        assert 'cleanup_pending' in (state / 'handover.md').read_text()
        assert _wt_git(w.repo, 'branch', '--list', meta['branch'])
        for _ in range(2):
            report = launcher.cleaner([])
            assert any(row['shift'] == state.name for row in report['reaped'])
            recovery = json.loads((state / 'recovery.json').read_text())
            assert recovery['cleanup_pending'] is True and recovery['error']
            floor = json.loads(launcher.FLOOR_FILE.read_text())
            assert floor['worktrees'][state.name] == recovery
            assert any(row['shift'] == state.name for row in floor['reaped'])
    finally:
        child.communicate(timeout=10)
    launcher.cleaner([])
    assert not work.exists()
    assert not _wt_git(w.repo, 'branch', '--list', meta['branch'])
    assert str(work).replace('\\', '/') not in _wt_git(w.repo, 'worktree', 'list', '--porcelain')
    recovery = json.loads((state / 'recovery.json').read_text())
    assert recovery['cleanup_pending'] is False and not recovery['error']
    assert json.loads(launcher.FLOOR_FILE.read_text())['worktrees'][state.name] == recovery
    before = _floor_snapshot(w.root)
    assert launcher.cleaner([])['reaped'] == []
    assert _floor_snapshot(w.root) == before


@pytest.mark.parametrize('failure_phase', ['add', 'commit'])
def test_worktree_failed_recovery_save_keeps_edits_branch_and_completes_down(
        worktree_world, capsys, monkeypatch, failure_phase):
    w = worktree_world
    state, work, meta = _wt_start(w)
    base = _wt_git(work, 'rev-parse', 'HEAD')
    (work / 'shared.txt').write_text('unsaved tracked edit\n')
    (work / 'new file.txt').write_text('unsaved untracked edit\n')
    lock = _lock(str(w.repo), state.name)
    original = launcher.git_run

    def fail_save(cwd, *args, **kwargs):
        if Path(cwd) == work and args[0] == failure_phase:
            raise SystemExit(f'injected recovery {failure_phase} failure')
        return original(cwd, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(launcher, 'git_run', fail_save)
        launcher.down([state.name])
    out = json.loads(capsys.readouterr().out)
    assert out['lock_released'] == [str(w.repo)] and not lock.exists()
    assert work.is_dir()
    assert (work / 'shared.txt').read_text() == 'unsaved tracked edit\n'
    assert (work / 'new file.txt').read_text() == 'unsaved untracked edit\n'
    assert _wt_git(w.repo, 'rev-parse', meta['branch']) == base
    events = [json.loads(line) for line in (state / 'bus.jsonl').read_text().splitlines()]
    assert events[-1]['event'] == 'down'
    recovery = json.loads((state / 'recovery.json').read_text())
    assert recovery['preserved_branch'] == meta['branch']
    assert recovery['cleanup_pending'] is True
    assert f'injected recovery {failure_phase} failure' in recovery['error']
    assert json.loads(launcher.FLOOR_FILE.read_text())['worktrees'][state.name] == recovery
    # Removing the fault lets the existing cleaner save the original files and reclaim.
    launcher.cleaner([])
    assert not work.exists()
    assert _wt_git(w.repo, 'show', f"{meta['branch']}:shared.txt") == 'unsaved tracked edit'
    assert _wt_git(w.repo, 'show', f"{meta['branch']}:new file.txt") == 'unsaved untracked edit'
    assert json.loads((state / 'recovery.json').read_text())['cleanup_pending'] is False


# ── Apprentice-first uplift (NGH-038): independent acceptance regressions ────

def _uplift_shift(monkeypatch, tmp_path):
    d = _mail_shift(monkeypatch, tmp_path)
    roles = ['Foreman', 'Advisor', 'Coder', 'Tester', 'Apprentice']
    (d / 'crew.json').write_text(json.dumps({'crew': 'acceptance-crew', 'members': [
        {'label': r, 'role': r, 'identity': f'{d.name}-{r.lower()}', 'human_contact': r == 'Foreman'}
        for r in roles]}), encoding='utf-8')
    for i, role in enumerate(roles):
        (d / f'{role}.json').write_text(json.dumps({
            'role': role, 'name': f'{d.name}-{role.lower()}', 'started': 100.0 + i}), encoding='utf-8')
    return d


def _uplift_send(capsys, sender, recipient, kind, asg, body, *options):
    _send(capsys, sender, recipient, '--kind', kind, '--asg', asg, '--body', body, *options)


def _uplift_start(capsys, asg='code-1'):
    _uplift_send(capsys, 'Foreman', 'Apprentice', 'assignment', asg, 'Implement bounded change')


def _uplift_need(capsys, asg='code-1'):
    _uplift_send(capsys, 'Apprentice', 'Foreman', 'note', asg,
                 'APPRENTICE-NEEDS-SENIOR\nCross-cutting design requires senior judgment')


def _uplift_transfer(capsys, asg='code-1', reason='needs-senior'):
    _uplift_send(capsys, 'Foreman', 'Coder', 'uplift', asg, 'Review evidence and take over', '--reason', reason)


def _uplift_fail(capsys, verification, coding='code-1', body='TESTED-FAIL\nExact reproduction'):
    _uplift_send(capsys, 'Foreman', 'Tester', 'assignment', verification, 'Verify change', '--coding-asg', coding)
    _uplift_send(capsys, 'Tester', 'Foreman', 'result', verification, body)


def _uplift_view(d, role='Foreman'):
    return launcher.inbox_view(d, role)


def test_uplift_needs_senior_transfers_same_asg_with_durable_evidence(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_need(capsys)
    original = _rows(d)
    _uplift_transfer(capsys)
    rows = _rows(d)
    assert rows[:2] == original
    transfer = rows[-1]
    assert (transfer['kind'], transfer['asg'], transfer['sender'], transfer['recipient']) == \
        ('uplift', 'code-1', 'Foreman', 'Coder')
    assert [r['id'] for r in _uplift_view(d, 'Coder')['active_assignments']] == [transfer['id']]
    assert not _uplift_view(d, 'Apprentice')['active_assignments']
    assert any(r['id'] == transfer['id'] for r in _uplift_view(d, 'Coder')['inbound_unacked'])
    # Evidence must survive the sender and the CLI process, not just appear in stdout.
    persisted = json.dumps(transfer)
    assert 'APPRENTICE-NEEDS-SENIOR' in persisted
    events = [json.loads(line) for line in (d / 'bus.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(e.get('kind') == 'uplift' and e.get('id') == transfer['id'] for e in events)


@pytest.mark.parametrize('owner', ['Apprentice', 'apprentice', 'Nightshift-shift-mail0001-apprentice'])
def test_uplift_refuses_apprentice_claim_aliases_without_partial_writes(monkeypatch, tmp_path, capsys, owner):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_need(capsys)
    launcher.claim(['mail0001', owner, str(tmp_path / 'a file.py')])
    capsys.readouterr()
    before, bus = _rows(d), (d / 'bus.jsonl').read_bytes()
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys)
    assert _rows(d) == before and (d / 'bus.jsonl').read_bytes() == bus
    assert launcher.load_claims(d)
    launcher.release(['mail0001', owner])
    capsys.readouterr()
    _uplift_transfer(capsys)
    assert _rows(d)[-1]['kind'] == 'uplift'


def test_uplift_requires_stored_evidence_and_deduplicates_source_dispatch(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys)
    assert len(_rows(d)) == 1
    _uplift_need(capsys)
    _uplift_transfer(capsys)
    before = _rows(d)
    with launcher.contextlib.suppress(SystemExit):
        _uplift_transfer(capsys)
    assert _rows(d) == before
    assert _uplift_view(d)['uplift_counts']['total'] == 1


@pytest.mark.parametrize('sender,recipient', [('Apprentice', 'Coder'), ('Coder', 'Foreman'), ('Foreman', 'Tester')])
def test_uplift_refuses_invalid_routing_without_writes(monkeypatch, tmp_path, capsys, sender, recipient):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_need(capsys)
    before = _rows(d)
    with pytest.raises(SystemExit):
        _uplift_send(capsys, sender, recipient, 'uplift', 'code-1', 'route', '--reason', 'needs-senior')
    assert _rows(d) == before


def test_uplift_two_distinct_linked_failures_trigger_even_after_done(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_send(capsys, 'Apprentice', 'Foreman', 'result', 'code-1', 'APPRENTICE-DONE\nImplemented')
    _uplift_fail(capsys, 'verify-1')
    _uplift_send(capsys, 'Tester', 'Foreman', 'result', 'verify-1', 'TESTED-FAIL\nDuplicate verdict')
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys, reason='tester-failed-twice')
    _uplift_fail(capsys, 'verify-2')
    _uplift_transfer(capsys, reason='tester-failed-twice')
    transfer = _rows(d)[-1]
    assert [r['id'] for r in _uplift_view(d, 'Coder')['active_assignments']] == [transfer['id']]
    assert 'Exact reproduction' in json.dumps(transfer)
    assert _uplift_view(d)['uplift_counts']['by_reason']['tester-failed-twice'] == 1


def test_uplift_unlinked_failures_and_relay_notes_do_not_count(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_send(capsys, 'Tester', 'Foreman', 'result', 'unlinked-1', 'TESTED-FAIL\nNo linkage')
    _uplift_send(capsys, 'Tester', 'Foreman', 'result', 'unlinked-2', 'TESTED-FAIL\nNo linkage')
    _uplift_send(capsys, 'Foreman', 'Coder', 'note', 'code-1', 'TESTED-FAIL\nRelayed failure')
    before = _rows(d)
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys, reason='tester-failed-twice')
    assert _rows(d) == before


def test_uplift_guidance_handback_requires_released_coder_claims(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_need(capsys)
    _uplift_transfer(capsys)
    _uplift_send(capsys, 'Coder', 'Foreman', 'note', 'code-1', 'GUIDANCE\nUse the existing parser')
    launcher.claim(['mail0001', f'{d.name}-coder', str(tmp_path / 'source.py')])
    capsys.readouterr()
    before = _rows(d)
    with pytest.raises(SystemExit):
        _uplift_start(capsys)
    assert _rows(d) == before
    launcher.release(['mail0001', f'{d.name}-coder'])
    capsys.readouterr()
    _uplift_start(capsys)
    latest = _rows(d)[-1]
    assert [r['id'] for r in _uplift_view(d, 'Apprentice')['active_assignments']] == [latest['id']]
    assert not _uplift_view(d, 'Coder')['active_assignments']
    assert _uplift_view(d)['uplift_counts']['total'] == 1


def test_uplift_old_verification_results_do_not_contaminate_handback(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    for verification in ('old-1', 'old-2'):
        _uplift_send(capsys, 'Foreman', 'Tester', 'assignment', verification, 'Verify old version',
                     '--coding-asg', 'code-1')
    _uplift_need(capsys)
    _uplift_transfer(capsys)
    _uplift_send(capsys, 'Coder', 'Foreman', 'note', 'code-1', 'GUIDANCE\nReturn to Apprentice')
    _uplift_start(capsys)
    for verification in ('old-1', 'old-2'):
        _uplift_send(capsys, 'Tester', 'Foreman', 'result', verification, 'TESTED-FAIL\nLate old verdict')
    before = _rows(d)
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys, reason='tester-failed-twice')
    assert _rows(d) == before


def test_uplift_completion_requires_latest_coding_assignee_after_dispatch(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_send(capsys, 'Coder', 'Foreman', 'result', 'code-1', 'CODER-DONE\nHistorical result')
    _uplift_need(capsys)
    _uplift_transfer(capsys)
    transfer_id = _rows(d)[-1]['id']
    _uplift_send(capsys, 'Apprentice', 'Foreman', 'result', 'code-1', 'APPRENTICE-DONE\nLate old owner')
    assert [r['id'] for r in _uplift_view(d, 'Coder')['active_assignments']] == [transfer_id]
    _uplift_send(capsys, 'Coder', 'Foreman', 'result', 'code-1', 'CODER-DONE\nCurrent completion')
    assert not _uplift_view(d, 'Coder')['active_assignments']


def test_uplift_counts_do_not_depend_on_bus_duplicates_or_other_crew(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_need(capsys)
    _uplift_transfer(capsys)
    first = _uplift_view(d)['uplift_counts']
    assert first['crew'] == 'acceptance-crew' and first['total'] == 1
    assert first['by_reason']['needs-senior'] == 1
    with (d / 'bus.jsonl').open('ab') as f:
        f.write((d / 'bus.jsonl').read_bytes())
    assert _uplift_view(d)['uplift_counts'] == first
    # A different shift with the same assignment ID starts its own accounting.
    other = tmp_path / 'shifts' / 'Nightshift-shift-other'
    other.mkdir()
    (other / 'crew.json').write_bytes((d / 'crew.json').read_bytes())
    assert _uplift_view(other)['uplift_counts']['total'] == 0


@pytest.mark.parametrize('delivery', ['sent', 'ack'])
def test_uplift_timeout_uses_first_delivery_epoch_and_exact_900_boundary(monkeypatch, tmp_path, capsys, delivery):
    d = _uplift_shift(monkeypatch, tmp_path)
    clock = [10000.0]
    monkeypatch.setattr(launcher.time, 'time', lambda: clock[0])
    _uplift_start(capsys)
    clock[0] += 1000
    assert not _uplift_view(d)['uplift_due']  # storing is not delivery
    if delivery == 'sent':
        launcher.msg(['sent', 'mail0001', '1'])
    else:
        launcher.ack(['mail0001', 'Apprentice', '1'])
    capsys.readouterr()
    epoch = _rows(d)[0]['delivered_epoch']
    assert epoch == 11000
    clock[0] = epoch + 899.999
    launcher.ack(['mail0001', 'Apprentice', '1'])
    launcher.msg(['sent', 'mail0001', '1'])
    capsys.readouterr()
    assert _rows(d)[0]['delivered_epoch'] == epoch
    assert not _uplift_view(d)['uplift_due']
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys, reason='timeout')
    clock[0] = epoch + 900
    due = _uplift_view(d)['uplift_due']
    assert due == [{'asg': 'code-1', 'source_dispatch_id': 1, 'reasons': ['timeout']}]
    _uplift_transfer(capsys, reason='timeout')
    assert _uplift_view(d)['uplift_counts']['by_reason']['timeout'] == 1


def test_uplift_timeout_suppressed_by_done_but_needs_senior_is_not(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    now = [10000.0]
    monkeypatch.setattr(launcher.time, 'time', lambda: now[0])
    _uplift_start(capsys)
    launcher.ack(['mail0001', 'Apprentice', '1'])
    capsys.readouterr()
    _uplift_send(capsys, 'Apprentice', 'Foreman', 'result', 'code-1', 'APPRENTICE-DONE\nComplete')
    now[0] += 900
    assert not _uplift_view(d)['uplift_due']
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys, reason='timeout')
    _uplift_need(capsys)
    _uplift_transfer(capsys)


def test_uplift_stale_apprentice_incarnation_cannot_transfer(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_need(capsys)
    info = json.loads((d / 'Apprentice.json').read_text())
    (d / 'Apprentice.json').write_text(json.dumps({**info, 'started': 999.0}))
    before = _rows(d)
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys)
    assert _rows(d) == before and not _uplift_view(d)['uplift_due']


def test_uplift_concurrent_retries_create_one_durable_transfer(monkeypatch, tmp_path, capsys):
    from concurrent.futures import ThreadPoolExecutor

    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_need(capsys)

    def transfer(_):
        launcher.msg(['send', 'mail0001', 'Foreman', 'Coder', '--kind', 'uplift', '--asg', 'code-1',
                      '--reason', 'needs-senior', '--body', 'Concurrent retry'])

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(transfer, range(2)))
    capsys.readouterr()
    rows = [r for r in _rows(d) if r['kind'] == 'uplift']
    assert len(rows) == 1
    assert _uplift_view(d)['uplift_counts']['total'] == 1
    events = [json.loads(line) for line in (d / 'bus.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len([e for e in events if e.get('kind') == 'uplift']) == 1


def test_uplift_numbered_labels_use_manifest_roles_and_identity_claims(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    crew = json.loads((d / 'crew.json').read_text())
    for member in crew['members']:
        if member['role'] in ('Apprentice', 'Coder', 'Tester'):
            role = member['role']
            member['label'] = role + '2'
            member['identity'] = f'{d.name}-{role.lower()}2'
            info = json.loads((d / f'{role}.json').read_text())
            (d / f'{role}2.json').write_text(json.dumps({**info, 'name': member['identity']}))
    (d / 'crew.json').write_text(json.dumps(crew))
    _uplift_send(capsys, 'Foreman', 'Apprentice2', 'assignment', 'numbered', 'Implement')
    _uplift_send(capsys, 'Apprentice2', 'Foreman', 'note', 'numbered', 'APPRENTICE-NEEDS-SENIOR\nReason')
    launcher.claim(['mail0001', f'{d.name}-apprentice2', str(tmp_path / 'numbered.py')])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        _uplift_send(capsys, 'Foreman', 'Coder2', 'uplift', 'numbered', 'Takeover', '--reason', 'needs-senior')
    launcher.release(['mail0001', f'{d.name}-apprentice2'])
    capsys.readouterr()
    _uplift_send(capsys, 'Foreman', 'Coder2', 'uplift', 'numbered', 'Takeover', '--reason', 'needs-senior')
    assert _uplift_view(d, 'Coder2')['active_assignments'][0]['asg'] == 'numbered'


def test_uplift_legacy_untimed_dispatch_is_not_assumed_to_have_timed_out(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    with launcher.contextlib.closing(launcher.sqlite3.connect(d / 'mailbox.sqlite3')) as conn:
        conn.execute(launcher.MAILBOX_SCHEMA)
        conn.execute("INSERT INTO messages (kind,sender,sender_incarnation,recipient,recipient_incarnation,"
                     "asg,body,created,state) VALUES (?,?,?,?,?,?,?,?,?)",
                     ('assignment', 'Foreman', 100.0, 'Apprentice', 104.0, 'code-1', 'Legacy assignment',
                      '2020-01-01 00:00:00', 'acknowledged'))
        conn.commit()
    snapshot = (d / 'mailbox.sqlite3').read_bytes()
    assert not _uplift_view(d)['uplift_due']
    assert (d / 'mailbox.sqlite3').read_bytes() == snapshot
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys, reason='timeout')
    assert _rows(d)[0]['delivered_epoch'] is None
    assert _rows(d)[0]['body'] == 'Legacy assignment'


def test_uplift_failures_span_same_apprentice_retry_dispatches(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_send(capsys, 'Apprentice', 'Foreman', 'result', 'code-1', 'APPRENTICE-DONE\nFirst attempt')
    _uplift_fail(capsys, 'verify-first')
    _uplift_start(capsys)
    latest_id = _rows(d)[-1]['id']
    _uplift_send(capsys, 'Apprentice', 'Foreman', 'result', 'code-1', 'APPRENTICE-DONE\nSecond attempt')
    _uplift_fail(capsys, 'verify-second')
    _uplift_transfer(capsys, reason='tester-failed-twice')
    transfer = _rows(d)[-1]
    assert transfer['source_dispatch_id'] == latest_id
    evidence = json.loads(transfer['evidence'])
    assert {r['asg'] for r in evidence} == {'verify-first', 'verify-second'}
    assert all(r['body'].startswith('TESTED-FAIL') for r in evidence)


def test_uplift_new_apprentice_incarnation_resets_failure_continuity(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_fail(capsys, 'verify-old')
    info = json.loads((d / 'Apprentice.json').read_text())
    (d / 'Apprentice.json').write_text(json.dumps({**info, 'started': 999.0}))
    _uplift_start(capsys)
    _uplift_fail(capsys, 'verify-new')
    before = _rows(d)
    with pytest.raises(SystemExit):
        _uplift_transfer(capsys, reason='tester-failed-twice')
    assert _rows(d) == before


def test_uplift_same_owner_retry_timeout_belongs_to_latest_dispatch(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    now = [10000.0]
    monkeypatch.setattr(launcher.time, 'time', lambda: now[0])
    _uplift_start(capsys)
    launcher.msg(['sent', 'mail0001', '1'])
    capsys.readouterr()
    now[0] += 901
    assert _uplift_view(d)['uplift_due'][0]['reasons'] == ['timeout']
    _uplift_start(capsys)
    latest_id = _rows(d)[-1]['id']
    assert not _uplift_view(d)['uplift_due']
    launcher.msg(['sent', 'mail0001', str(latest_id)])
    capsys.readouterr()
    now[0] += 899
    assert not _uplift_view(d)['uplift_due']
    now[0] += 1
    assert _uplift_view(d)['uplift_due'] == [
        {'asg': 'code-1', 'source_dispatch_id': latest_id, 'reasons': ['timeout']}]


def test_uplift_handback_then_new_escalation_counts_each_source_once(monkeypatch, tmp_path, capsys):
    d = _uplift_shift(monkeypatch, tmp_path)
    _uplift_start(capsys)
    _uplift_need(capsys)
    _uplift_transfer(capsys)
    first = _rows(d)[-1]
    _uplift_send(capsys, 'Coder', 'Foreman', 'note', 'code-1', 'GUIDANCE\nEdit using recommendation')
    _uplift_start(capsys)
    _uplift_need(capsys)
    _uplift_transfer(capsys)
    second = _rows(d)[-1]
    assert first['source_dispatch_id'] != second['source_dispatch_id']
    _uplift_transfer(capsys)  # retry same latest transfer
    assert len([r for r in _rows(d) if r['kind'] == 'uplift']) == 2
    assert _uplift_view(d)['uplift_counts'] == {
        'crew': 'acceptance-crew', 'total': 2,
        'by_reason': {'needs-senior': 2, 'tester-failed-twice': 0, 'timeout': 0}}


# ── Claude + Copilot crew, Copilot AI credit cap, Copilot send retries (NGH-041) ──

def test_claude_copilot_manifest_caps_only_apprentice_and_uses_claude_tester():
    crew = launcher.load_crew('claude-copilot')
    assert crew['layout'] == '2x4' and crew['slots'] == 8 and crew['contract'] == 'result.json'
    assert crew['max_credits'] == 30   # one Copilot pane consumes the shift budget
    assert [(r['role'], r['tool'], r['model'], r.get('max_credits')) for r in crew['roles']] == [
        ('Foreman', 'claude', 'claude-opus-5-5', None),
        ('Advisor', 'claude', 'claude-opus-5-5', None),
        ('Apprentice', 'copilot', 'gemini-3.8-flash', 30),
        ('Coder', 'claude', 'claude-opus-5-5', None),
        ('Tester', 'claude', 'claude-opus-5-5', None)]
    assert [r['role'] for r in crew['roles'] if r['tool'] == 'copilot'] == ['Apprentice']
    tester = crew['roles'][-1]
    assert tester['colour'] == 'blue' and tester['brief'] == 'tester.md'
    for r in crew['roles']:
        if r['tool'] == 'copilot':
            assert (r['reasoning'], r['context'], r['permissions']) == ('high', 'long_context', 'skip')
    assert [r['brief'] for r in crew['roles']] == ['foreman.md', 'advisor.md', 'apprentice.md', 'coder.md',
                                                   'tester.md']


def _colour047_world(monkeypatch, tmp_path):
    """A fresh Claude registry and transcript, with no CLI or console process."""
    home = tmp_path / 'user home'
    cwd = tmp_path / 'repo with spaces [colour]'
    cwd.mkdir()
    sessions = home / '.claude' / 'sessions'
    sessions.mkdir(parents=True)
    project = home / '.claude' / 'projects' / re.sub(r'[:\\/]', '-', str(cwd))
    project.mkdir(parents=True)
    transcript = project / 'session-047.jsonl'
    transcript.write_text('{"type":"system","content":"startup"}\n', encoding='utf-8')
    (sessions / '42.json').write_text(json.dumps({'sessionId': 'session-047'}), encoding='utf-8')
    theme = home / '.claude.json'
    theme.write_text('{"theme":"light","sentinel":"preserve"}\n', encoding='utf-8')
    settings = home / '.claude' / 'settings.json'
    settings.write_text('{"permissions":{"defaultMode":"auto"}}\n', encoding='utf-8')
    originals = {p: p.read_bytes() for p in (theme, settings)}
    monkeypatch.setattr(launcher.Path, 'home', lambda: home)
    monkeypatch.setattr(launcher.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(launcher, 'peek_lines', lambda *a, **kw: ['❯'])
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **kw: pytest.fail('real console access'))
    child = types.SimpleNamespace(pid=42, poll=lambda: None)
    return child, cwd, transcript, originals


@pytest.mark.parametrize('colour', ['red', 'purple', 'green', 'blue'])
def test_colour047_waits_for_applied_colour_even_when_composer_stays_empty(monkeypatch, tmp_path, colour):
    child, cwd, transcript, originals = _colour047_world(monkeypatch, tmp_path)
    sent, sleeps = [], []

    def inject(pid, command, **kw):
        sent.append((pid, command))
        return True

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            with transcript.open('a', encoding='utf-8') as out:
                out.write(json.dumps({'type': 'agent-color', 'agentColor': colour,
                                      'sessionId': 'session-047'}) + '\n')

    monkeypatch.setattr(launcher, 'inject_command', inject)
    monkeypatch.setattr(launcher.time, 'sleep', sleep)
    assert launcher.set_role_colour(child, colour, str(cwd)) is True
    assert sent == [(42, '/color ' + colour)]
    assert len(sleeps) >= 2, 'An old empty composer is not colour application confirmation'
    assert {p: p.read_bytes() for p in originals} == originals


@pytest.mark.parametrize('failure', ['inject-refused', 'child-exited', 'no-confirmation', 'wrong-colour',
                                   'missing-registry', 'malformed-registry', 'malformed-transcript'])
def test_colour047_unconfirmed_colour_cannot_unlock_first_turn(monkeypatch, tmp_path, failure):
    child, cwd, transcript, originals = _colour047_world(monkeypatch, tmp_path)
    sent = []
    registry = launcher.Path.home() / '.claude' / 'sessions' / '42.json'
    if failure == 'child-exited':
        child.poll = lambda: 1
    elif failure == 'missing-registry':
        registry.unlink()
    elif failure == 'malformed-registry':
        registry.write_text('{broken', encoding='utf-8')
    elif failure == 'malformed-transcript':
        transcript.write_text('{partial json', encoding='utf-8')
    elif failure == 'wrong-colour':
        transcript.write_text(json.dumps({'type': 'agent-color', 'agentColor': 'red',
                                          'sessionId': 'session-047'}) + '\n', encoding='utf-8')

    def inject(pid, command, **kw):
        sent.append(command)
        return failure != 'inject-refused'

    monkeypatch.setattr(launcher, 'inject_command', inject)
    assert launcher.set_role_colour(child, 'blue', str(cwd)) is False
    assert all(command == '/color blue' for command in sent)
    if failure == 'child-exited':
        assert sent == []
    assert {p: p.read_bytes() for p in originals} == originals


@pytest.mark.usefixtures('_container_isolated')
@pytest.mark.parametrize('role,colour', [('Foreman', 'red'), ('Advisor', 'purple'), ('Coder', 'green'),
                                      ('Tester', 'blue')])
@pytest.mark.parametrize('spawned', [False, True], ids=['initial-manifest', 'spawned-member'])
def test_colour047_each_claude_role_applies_colour_before_immediate_first_turn(
        monkeypatch, tmp_path, injected, role, colour, spawned):
    if spawned:
        state = _container(tmp_path, members=('Foreman',), free=(2, 3, 4))
        if role == 'Foreman':
            monkeypatch.setattr(launcher, 'pid_alive', lambda *a: False)
        launcher.spawn([CONT_SHIFT, '--role', role, '--crew', 'claude-copilot'])
        saved = json.loads((state / 'crew.json').read_text(encoding='utf-8'))
        member = next(m for m in saved['members'] if m['label'] == role)
        assert member['colour'] == colour and member['tool'] == 'claude'
    else:
        state = tmp_path / CONT_SHIFT
        state.mkdir()
    (state / 'prompt.txt').write_text('FIRST TURN TASK', encoding='utf-8')
    child, cwd, transcript, originals = _colour047_world(monkeypatch, tmp_path)
    events, commands = [], []
    expected_message = launcher.opening_message(member, state) if spawned else launcher.START_MESSAGES[role]

    def popen(command, **kw):
        commands.append(command)
        assert expected_message not in command, 'Task argv can start the model before colour is applied'
        assert command[command.index('--name') + 1] == f'{CONT_SHIFT}-{role.lower()}'
        assert command[command.index('--model') + 1] == 'claude-opus-5-5'
        assert kw['cwd'] == str(cwd)
        assert not any('/theme' in a or '--agent-color' in a for a in command)
        events.append('launch')
        return child

    def run(argv, **kw):
        assert argv == ['agents-cli', '_attach', 'inject', '42', '1']
        message = kw['input'].decode('utf-8')
        if message == '/color ' + colour:
            events.append('colour-command')
            with transcript.open('a', encoding='utf-8') as out:
                out.write(json.dumps({'type': 'agent-color', 'agentColor': colour,
                                      'sessionId': 'session-047'}) + '\n')
        else:
            assert message == expected_message
            assert events[-1] == 'colour-command'
            assert json.loads(transcript.read_text(encoding='utf-8').splitlines()[-1])['agentColor'] == colour
            events.append('first-turn')
        return subprocess.CompletedProcess(argv, 0, b'{}', b'')

    child.wait = lambda: events.append('wait') or 0
    monkeypatch.setattr(launcher.subprocess, 'Popen', popen)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: events.append('trust'))
    monkeypatch.setattr(launcher, 'set_pane_background', lambda bg: events.append(('background', bg)))
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane(role, str(state), start=True, cwd=str(cwd), crew='claude-copilot')
    assert events == [('background', launcher.CAMPBELL_BACKGROUND), 'launch', 'trust', 'colour-command',
                      'first-turn', 'wait']
    assert len(commands) == 1
    record = json.loads((state / f'{role}.json').read_text(encoding='utf-8'))
    assert record['tool'] == 'claude' and record['name'] == f'{CONT_SHIFT}-{role.lower()}'
    assert {p: p.read_bytes() for p in originals} == originals


@pytest.mark.parametrize('confirmed', [False, None])
def test_colour047_pane_never_sends_task_without_positive_confirmation(monkeypatch, tmp_path, capsys, confirmed):
    state = tmp_path / 'S'
    state.mkdir()
    member = _member('S', 'Tester', colour='blue')
    (state / 'crew.json').write_text(json.dumps({'members': [member]}), encoding='utf-8')
    child = types.SimpleNamespace(pid=42, wait=lambda: 0)
    sent = []
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **kw: child)
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda *a: confirmed)
    monkeypatch.setattr(launcher, 'inject_command', lambda pid, message, **kw: sent.append(message) or True)
    monkeypatch.setattr(launcher, 'set_pane_background', lambda *a: None)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane('Tester', str(state), start=True, cwd=str(tmp_path))
    assert sent == []
    assert 'not confirmed' in capsys.readouterr().out.lower()


def test_colour047_pane_reports_first_message_injection_failure(monkeypatch, tmp_path, capsys):
    state = tmp_path / 'S'
    state.mkdir()
    (state / 'crew.json').write_text(json.dumps({'members': [_member('S', 'Tester', colour='blue')]}),
                                  encoding='utf-8')
    child = types.SimpleNamespace(pid=42, wait=lambda: 0)
    sent = []
    monkeypatch.setattr(launcher.subprocess, 'Popen', lambda *a, **kw: child)
    monkeypatch.setattr(launcher, 'accept_trust_prompt', lambda *a: None)
    monkeypatch.setattr(launcher, 'set_role_colour', lambda *a: True)
    monkeypatch.setattr(launcher, 'inject_command', lambda pid, message, **kw: sent.append(message) or False)
    monkeypatch.setattr(launcher, 'set_pane_background', lambda *a: None)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    launcher.pane('Tester', str(state), start=True, cwd=str(tmp_path))
    assert sent == [launcher.START_MESSAGES['Tester']]
    assert re.search(r'opening message.*(?:not sent|failed)', capsys.readouterr().out, re.I)


@pytest.mark.parametrize('failure', ['nonzero', 'timeout'])
def test_colour047_console_injection_failure_is_not_reported_as_success(monkeypatch, failure):
    monkeypatch.setattr(launcher, 'peek_lines', lambda *a, **kw: ['❯'])

    def run(argv, **kw):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 10)
        return subprocess.CompletedProcess(argv, 1, b'', b'console unavailable')

    monkeypatch.setattr(launcher.subprocess, 'run', run)
    assert launcher.inject_command(42, '/color blue') is False


def test_start_dry_run_claude_copilot_caps_each_copilot_pane(monkeypatch, tmp_path, capsys):
    _fixed_uuid(monkeypatch)
    monkeypatch.setattr(launcher.subprocess, 'run', _fake_run([]))
    launcher.start(['--dry-run', '--no-worktree', '--cwd', str(tmp_path), '--crew', 'claude-copilot', 'task'])
    plan = json.loads(capsys.readouterr().out)
    assert next(iter(plan)) == 'max_credits' and plan['max_credits'] == 30   # the shift budget comes first
    roles = plan['roles']
    assert _titles(plan['wt_argv']) == [roles['Tester']['pane_title']] * 8
    assert plan['wt_argv'].count('--suppressApplicationTitle') == 8
    panes = [cell for cell in _cells(plan['wt_argv']) if '--title' in cell]
    assert len(panes) == 8
    for cell in panes:
        if '--pane' in cell and cell[cell.index('--pane') + 1] == 'Apprentice':
            assert cell[cell.index('--colorScheme') + 1] == 'Nightshift yellow'
        else:
            assert '--colorScheme' not in cell  # Claude and free slots retain the profile's Campbell scheme.
    for label in ('Foreman', 'Advisor', 'Coder', 'Tester'):
        assert 'command' not in roles[label] and 'max_credits' not in roles[label]
        assert roles[label].get('model', plan['model']) == 'claude-opus-5-5'
    for label in ('Apprentice',):
        cmd = roles[label]['command']
        assert roles[label]['max_credits'] == 30
        assert cmd[cmd.index('--max-ai-credits') + 1] == '30'
        # the bundled Copilot 1.0.74 (--no-auto-update) rejects --usage-output-file (NGH-042)
        assert '--usage-output-file' not in cmd
        assert not any('usage.json' in str(a) for a in cmd)
        assert cmd[-4] == '--name' and cmd[-2] == '-i'


def test_copilot_command_credit_cap_without_usage_file():
    head = [str(launcher.COPILOT_EXE), '--allow-all', '--no-auto-update', '--model', 'gemini-3.8-flash']
    assert launcher.copilot_command('gemini-3.8-flash', 'B', 'F', max_credits=30) \
        == head + ['--max-ai-credits', '30', '-i', 'B\n\nF']
    assert launcher.copilot_command('gemini-3.8-flash', 'B', 'F') == head + ['-i', 'B\n\nF']
    with pytest.raises(TypeError):
        launcher.copilot_command('gemini-3.8-flash', 'B', 'F', usage_file='C:/s/x.json')
    assert not hasattr(launcher, 'usage_file')


def _budget_crew(monkeypatch, tmp_path, budget, roles):
    _crew_file(monkeypatch, tmp_path, 'budget', 'name: budget\n' + (f'max_credits: {budget}\n' if budget else '')
               + 'roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n' + roles)
    return lambda: launcher.load_crew('budget')


COPILOT_PAIR = ('  - {role: Coder, brief: coder.md, tool: copilot, model: m, count: 2}\n'
                '  - {role: Tester, brief: tester.md, tool: copilot, model: m}\n'
                '  - {role: Advisor, brief: advisor.md}\n')


@pytest.mark.parametrize('budget,share', [(90, 30), (100, 33), (120, 40)])
def test_max_credits_shift_budget_splits_evenly_with_floor_over_copilot_panes(monkeypatch, tmp_path, budget, share):
    crew = _budget_crew(monkeypatch, tmp_path, budget, COPILOT_PAIR)()   # 3 Copilot panes: Coder x2 + Tester
    assert crew['max_credits'] == budget
    assert {r['role']: r.get('max_credits') for r in crew['roles']} == {
        'Foreman': None, 'Coder': share, 'Tester': share, 'Advisor': None}
    members = launcher.crew_members(crew, 'S')
    assert {m['label']: m.get('max_credits') for m in members} == {
        'Foreman': None, 'Coder-1': share, 'Coder-2': share, 'Tester': share, 'Advisor': None}


def test_max_credits_share_below_copilots_minimum_is_refused(monkeypatch, tmp_path):
    load = _budget_crew(monkeypatch, tmp_path, 89, COPILOT_PAIR)
    with pytest.raises(SystemExit, match=r"max_credits 89 over 3 Copilot panes is 29 each; Copilot's minimum "
                                         r'--max-ai-credits is 30 per pane'):
        load()


def test_max_credits_on_a_crew_without_copilot_panes_is_refused(monkeypatch, tmp_path):
    load = _budget_crew(monkeypatch, tmp_path, 60, '  - {role: Coder, brief: coder.md, tool: codex, model: m}\n')
    with pytest.raises(SystemExit, match='the crew has no copilot panes'):
        load()


@pytest.mark.parametrize('value', ["'60'", '60.0', 'true'])
def test_max_credits_shift_budget_must_be_a_whole_number(monkeypatch, tmp_path, value):
    load = _budget_crew(monkeypatch, tmp_path, value, COPILOT_PAIR)
    with pytest.raises(SystemExit, match=r'max_credits \(the shift budget\) must be a whole number'):
        load()


@pytest.mark.parametrize('tool', ['copilot', 'claude'])
def test_per_role_max_credits_is_refused_in_favour_of_the_shift_budget(monkeypatch, tmp_path, tool):
    model = ', model: m' if tool == 'copilot' else ''
    load = _budget_crew(monkeypatch, tmp_path, None,
                        f'  - {{role: Coder, brief: coder.md, tool: {tool}{model}, max_credits: 30}}\n')
    with pytest.raises(SystemExit, match='max_credits for Coder: the Copilot credit cap is the shift budget, set once '
                                         'as a top-level max_credits: key'):
        load()


def test_crew_without_a_budget_has_no_credit_caps():
    crew = launcher.load_crew('copilot')
    assert 'max_credits' not in crew and not any('max_credits' in r for r in crew['roles'])


def test_default_copilot_model_is_in_the_copilot_catalog():
    assert launcher.COPILOT_MODEL == 'gemini-3.8-flash' and launcher.DEFAULT_TOOL_MODELS['copilot'] == 'gemini-3.8-flash'
    assert launcher.AGY_MODEL == 'gemini-3.8-flash-high'


def _copilot_send_world(monkeypatch, tails):
    """agents-cli faked: send reports submitted false; each tail call pops the next screen (the last one repeats)."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == 'send':
            out = {'submitted': False, 'enter': True}
        elif argv[1] == 'tail':
            out = tails.pop(0) if len(tails) > 1 else tails[0]
        else:
            out = {}
        return subprocess.CompletedProcess(argv, 0, json.dumps(out).encode(), b'')
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher.time, 'sleep', lambda s: None)
    return calls


HDR = 'msg:7 kind:note asg:-'
STUCK = {'state': 'prompt', 'lines': ['─────', f'❯ {HDR}', '─────', 'ctrl+c exit']}
DONE = {'state': 'busy', 'lines': [f'❯ {HDR}', 'Working', '─────', '❯', '─────', 'esc interrupt']}
MENU = {'state': 'prompt', 'lines': ['─────', f'❯ {HDR}', '─────', '1. Allow  2. Deny  enter to confirm']}


def test_copilot_send_presses_enter_until_the_header_is_submitted(monkeypatch):
    calls = _copilot_send_world(monkeypatch, [STUCK, DONE])
    assert launcher.send_header(32, HDR, 'copilot') is True
    assert [a for a in calls if a[1] == 'key'] == [['agents-cli', 'key', '32', 'enter']]


def test_copilot_send_enter_retries_are_bounded(monkeypatch):
    calls = _copilot_send_world(monkeypatch, [STUCK])
    assert launcher.send_header(32, HDR, 'copilot') is False
    assert sum(a[1] == 'key' for a in calls) == launcher.COPILOT_ENTER_TRIES
    assert sum(a[1] == 'send' for a in calls) == 1


@pytest.mark.parametrize('tool,tail', [('copilot', MENU), ('codex', STUCK), (None, STUCK)])
def test_no_extra_enter_into_a_menu_or_on_other_tools(monkeypatch, tool, tail):
    calls = _copilot_send_world(monkeypatch, [tail])
    assert launcher.send_header(32, HDR, tool) is False
    assert not any(a[1] == 'key' for a in calls)



# ── per-tool trust dialogs and stdin injection (NGH-041 c2) ──────────────────

TRUST_SCREENS = {
    'claude': (['   No, exit', ' ❯ Yes, I trust this folder'], [' ❯ No, exit', '   Yes, I trust this folder']),
    'agy': (['Do you trust the contents of this project?', '> Yes, I trust this folder', 'enter Confirm'],
            ['Do you trust the contents of this project?', '  Yes, I trust this folder', '> No', 'enter Confirm']),
    'codex': (['Trust this folder? Codex can read, edit, and run files here, subject to your',
               'permission settings.', '› 1. Trust and continue', '  2. Open restricted', '  3. Quit'],
              ['Trust this folder? Codex can read, edit, and run', 'files here.', '  1. Trust and continue',
               '› 2. Open restricted', '  3. Quit']),
    'copilot': (['│ Confirm folder trust                       │', '│ Do you trust the files in this │',
                 '│ folder?                                    │', '│ ❯ 1. Yes                    │',
                 '│   2. Yes, and remember this folder for future sessions │', '│   3. No (Esc) │'],
                ['│ Do you trust the files in this folder? │', '│   1. Yes │',
                 '│ ❯ 2. Yes, and remember this folder for future sessions │', '│   3. No (Esc) │']),
}


@pytest.mark.parametrize('tool', sorted(TRUST_SCREENS))
def test_trust_prompt_matcher_per_tool(tool):
    accept, other = TRUST_SCREENS[tool]
    assert launcher.trust_prompt_state(tool, '\n'.join(accept)) == 'accept'
    assert launcher.trust_prompt_state(tool, '\n'.join(other)) == 'dialog'
    assert launcher.trust_prompt_state(tool, 'ordinary output\n❯ ') is None
    for elsewhere in (t for t in TRUST_SCREENS if t != tool and t not in ('claude', 'agy')):
        # another CLI's dialog text never triggers this tool's answer
        assert launcher.trust_prompt_state(tool, '\n'.join(TRUST_SCREENS[elsewhere][0])) is None


def test_codex_trust_for_the_repository_root_is_left_to_the_human():
    screen = TRUST_SCREENS['codex'][0] + ["Note: You're in a subdirectory of a Git project. Trusting will apply to the",
                                          'repository root:']
    assert launcher.trust_prompt_state('codex', '\n'.join(screen)) == 'past'


@pytest.mark.parametrize('tool', sorted(TRUST_SCREENS))
def test_accept_trust_prompt_moves_to_the_accepting_option_then_presses_enter(monkeypatch, tool):
    accept, other = TRUST_SCREENS[tool]
    screens = iter([{'lines': []}, {'lines': other}, {'lines': accept}])
    keys = []

    def run(argv, **kwargs):
        if argv[:3] == ['agents-cli', '_attach', 'peek']:
            return subprocess.CompletedProcess(argv, 0, json.dumps(next(screens)).encode(), b'')
        if argv[:2] == ['agents-cli', 'key']:
            keys.append(argv[3])
            return subprocess.CompletedProcess(argv, 0, b'{}', b'')
        raise AssertionError(argv)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher.time, 'sleep', lambda s: None)
    launcher.accept_trust_prompt(types.SimpleNamespace(pid=1, poll=lambda: None), tool)
    assert keys == ['down', 'enter']


def test_accept_trust_prompt_ignores_tools_without_a_known_dialog(monkeypatch):
    monkeypatch.setattr(launcher.subprocess, 'run', lambda *a, **k: pytest.fail('no peek for an unknown tool'))
    launcher.accept_trust_prompt(types.SimpleNamespace(pid=1, poll=lambda: None), 'opencode')


# ── per-repository configuration and instructions (NGH-043) ──────────────────

def _repo043_file(repo, name, text):
    path = Path(repo) / '.nightshift' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


@pytest.mark.parametrize('text', [
    '', 'null\n', '[]\n', 'false\n', 'config\n', 'target_branch: [broken\n',
    'unknown: true\n', 'target_branch: 2\n', 'target_branch: true\n',
    'target_branch: null\n', 'default_crew: []\n', 'default_crew: false\n',
    'build: []\n', 'build: null\n', 'build: command\n', 'build: {unknown: 1}\n',
    'build: {cmd: 2}\n', 'build: {cmd: false}\n', 'build: {cmd: null}\n',
    'build: {timeout: true}\n', 'build: {timeout: "90"}\n',
    'build: {timeout: []}\n', 'build: {timeout: null}\n',
    'build: {timeout: -1}\n', 'build: {timeout: .nan}\n',
    'build: {timeout: .inf}\n', 'build: {timeout: -.inf}\n',
])
def test_repo043_invalid_config_names_path_and_fails_before_start(monkeypatch, tmp_path, text):
    path = _repo043_file(tmp_path, 'config.yaml', text)
    shifts = tmp_path / 'state-that-must-not-exist'
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', shifts)
    monkeypatch.setattr(launcher, 'launch', lambda *a: pytest.fail('invalid config launched a window'))
    with pytest.raises(SystemExit) as err:
        launcher.start(['--dry-run', '--no-worktree', '--cwd', str(tmp_path), 'plain task'])
    assert path.as_posix() in str(err.value).replace('\\', '/')
    assert not shifts.exists()


@pytest.mark.parametrize('config', [{}, {'build': {}}, {'build': {'timeout': 90}},
                                     {'build': {'timeout': 0.5, 'cmd': 'check --all'}}])
def test_repo043_valid_config_is_reported_verbatim_in_dry_run(monkeypatch, tmp_path, capsys, config):
    import yaml
    _repo043_file(tmp_path, 'config.yaml', yaml.safe_dump(config))
    launcher.start(['--dry-run', '--no-worktree', '--cwd', str(tmp_path), 'plain task'])
    plan = json.loads(capsys.readouterr().out)
    assert plan['repo_config'] == config


def test_repo043_no_config_preserves_builtin_defaults(monkeypatch, tmp_path, capsys):
    launcher.start(['--dry-run', '--no-worktree', '--cwd', str(tmp_path), 'plain task'])
    plan = json.loads(capsys.readouterr().out)
    assert plan.get('repo_config') is None
    assert set(plan['roles']) == {'Foreman', 'Advisor', 'Coder', 'Tester', 'Apprentice'}
    text = launcher.role_brief('Coder', 'S-coder', 'S-foreman', cwd=tmp_path)
    assert '# Protocol' in text and '# Repository' not in text


@pytest.mark.parametrize('explicit', [False, True])
def test_repo043_start_config_defaults_and_explicit_cli_win(worktree_world, capsys, explicit):
    w = worktree_world
    _wt_git(w.repo, 'branch', 'configured')
    _repo043_file(w.repo, 'config.yaml', 'target_branch: configured\ndefault_crew: trio\n')
    _repo043_file(w.repo, 'crews/trio.yaml', TRIO)
    flags = ['--target-branch', 'main', '--crew', 'default'] if explicit else []
    launcher.start(['--dry-run', '--cwd', str(w.repo), *flags, 'plain task'])
    plan = json.loads(capsys.readouterr().out)
    assert plan['worktree']['target_branch'] == ('main' if explicit else 'configured')
    assert set(plan['roles']) == ({'Foreman', 'Advisor', 'Coder', 'Tester', 'Apprentice'} if explicit else
                                  {'Foreman', 'Coder-1', 'Coder-2', 'Coder-3'})
    assert plan['repo_config'] == {'target_branch': 'configured', 'default_crew': 'trio'}
    assert not w.launches


def test_repo043_repo_crew_overrides_builtin_and_other_names_fall_back(monkeypatch, tmp_path):
    _crew_file(monkeypatch, tmp_path, 'same', TRIO)
    _crew_file(monkeypatch, tmp_path, 'fallback', TRIO)
    local = TRIO.replace('claude-sonnet-5-5', 'local-model')
    _repo043_file(tmp_path, 'crews/same.yaml', local)
    assert launcher.load_crew('same', repo_root=tmp_path)['model'] == 'local-model'
    assert launcher.load_crew('fallback', repo_root=tmp_path)['model'] == 'claude-sonnet-5-5'
    _repo043_file(tmp_path, 'crews/same.yaml', 'roles: []\n')
    with pytest.raises(SystemExit, match='non-empty list'):
        launcher.load_crew('same', repo_root=tmp_path)


def test_repo043_canonical_root_prefers_worktree_metadata_and_resolves_fallback(tmp_path):
    canonical, work, state = (tmp_path / name for name in ('canonical repo', 'linked worktree', 'state'))
    for path in (canonical, work, state):
        path.mkdir()
    (state / 'worktree.json').write_text(json.dumps({'repo_root': str(canonical), 'worktree_dir': str(work)}))
    assert Path(launcher.canonical_repo_root(cwd=work, state_dir=state)) == canonical.resolve()
    assert Path(launcher.canonical_repo_root(cwd=work / '..' / work.name)) == work.resolve()


@pytest.mark.parametrize('role,brief_file', [('Foreman', 'foreman.md'), ('Coder', 'coder.md'),
                                          ('ReviewBot', 'coder.md')])
def test_repo043_prompt_layers_expand_all_placeholders_preserve_json_and_custom_role(tmp_path, role, brief_file):
    state = tmp_path / 'state' / 'Nightshift-shift-repo043'
    state.mkdir(parents=True)
    cwd = tmp_path / 'working folder with café'
    cwd.mkdir()
    _repo043_file(cwd, 'config.yaml', 'build:\n  cmd: "check --json"\n')
    _repo043_file(cwd, 'instructions.md', 'COMMON-REPO {shift} {identity} {state_dir} {nightshift} {cwd}\n'
                                         'Literal JSON: {"pass": true, "nested": {"n": 1}}\n')
    _repo043_file(cwd, f'{role.lower()}.md', 'ROLE-REPO {role} {peer_name}\n')
    text = launcher.role_brief(role, 'identity043', 'peer043', state, brief_file, cwd=cwd)
    assert text.count('# Protocol\n') == 1 and text.count('# Repository\n') == 1
    assert text.index('# Protocol\n') < text.index('# Repository\n') < text.index('COMMON-REPO') < text.index('ROLE-REPO')
    assert 'check --json' in text and 'build Nightshift-shift-repo043' in text
    assert 'identity043' in text and 'peer043' in text
    assert state.as_posix() in text and cwd.as_posix() in text
    assert 'Literal JSON: {"pass": true, "nested": {"n": 1}}' in text
    for key in ('shift', 'identity', 'state_dir', 'nightshift', 'cwd', 'role', 'peer_name'):
        assert '{' + key + '}' not in text
    if role == 'Foreman':
        marker = 'The human task for this shift follows, verbatim:'
        assert text.count('# Task\n') == 1 and text.count(marker) == 1
        assert text.index('ROLE-REPO') < text.index('# Task\n') < text.index(marker)
    if role == 'ReviewBot':
        assert '# Protocol\n' in text and '# ReviewBot' in text


@pytest.mark.parametrize('filename,text', [('instructions.md', 'repo-wide only'), ('coder.md', 'role only'),
                                         ('config.yaml', '{}\n')])
def test_repo043_each_optional_source_independently_enables_repository_layer(tmp_path, filename, text):
    _repo043_file(tmp_path, filename, text)
    brief = launcher.role_brief('Coder', 'S-coder', 'S-foreman', cwd=tmp_path)
    assert brief.count('# Repository\n') == 1
    if filename.endswith('.md'):
        assert text in brief


def test_repo043_prompt_reads_canonical_config_not_stale_worktree_copy(tmp_path):
    canonical, work, state = (tmp_path / name for name in ('canonical', 'work', 'state'))
    for path in (canonical, work, state):
        path.mkdir()
    (state / 'worktree.json').write_text(json.dumps({'repo_root': str(canonical), 'worktree_dir': str(work)}))
    _repo043_file(canonical, 'instructions.md', 'CANONICAL-REPO')
    _repo043_file(work, 'instructions.md', 'STALE-WORKTREE-REPO')
    brief = launcher.role_brief('Coder', 'S-coder', 'S-foreman', state, cwd=work)
    assert 'CANONICAL-REPO' in brief and 'STALE-WORKTREE-REPO' not in brief


@pytest.mark.parametrize('brief_file', ['foreman.md', 'foreman-cli.md', 'foreman-agy-coder.md'])
def test_repo043_foreman_self_host_rules_are_repository_scoped(monkeypatch, tmp_path, brief_file):
    # This checkout is itself a live shift. Read the repository assets under
    # review without discovering the outer shift's canonical base checkout.
    monkeypatch.setattr(launcher, 'SHIFTS_DIR', tmp_path / 'isolated-shifts')
    generic = (launcher.BRIEFS_DIR / brief_file).read_text(encoding='utf-8')
    for rule in ('test_nightshift_shifts.py', 'docs/ARCHITECTURE.md', 'C:/git/nightshift-shifts',
                 'no task IDs in commit messages'):
        assert rule not in generic
    other = launcher.role_brief('Foreman', 'S-foreman', 'S-coder', brief=brief_file, cwd=tmp_path)
    assert 'test_nightshift_shifts.py' not in other and 'docs/ARCHITECTURE.md' not in other
    self_host = launcher.role_brief('Foreman', 'S-foreman', 'S-coder', brief=brief_file,
                                    cwd=Path(__file__).resolve().parent)
    assert self_host.index('# Repository') < self_host.index('test_nightshift_shifts.py') < self_host.index('# Task')
    assert 'docs/ARCHITECTURE.md' in self_host


@pytest.mark.parametrize('explicit_timeout,expected_minutes', [(None, 1.5), ('2.5', 2.5)])
def test_repo043_build_infers_shift_cwd_and_converts_config_seconds_only(monkeypatch, tmp_path, capsys,
                                                                      explicit_timeout, expected_minutes):
    state, work, seen = _build_shift(monkeypatch, tmp_path)
    (state / 'crew.json').write_text(json.dumps({'cwd': str(work), 'members': []}))
    _repo043_file(work, 'config.yaml', 'build: {cmd: configured-command, timeout: 90}\n')
    original = launcher.take_build_lock
    waits = []

    def lock(cwd, shift, n, timeout):
        waits.append(timeout)
        return original(cwd, shift, n, timeout)
    monkeypatch.setattr(launcher, 'take_build_lock', lock)
    elsewhere = tmp_path / 'unrelated-caller'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    flags = ['--timeout', explicit_timeout, '--cmd', 'explicit-command'] if explicit_timeout else []
    launcher.build(['bld00001', *flags])
    record = json.loads(capsys.readouterr().out)
    assert record['cwd'] == str(work.resolve())
    assert record['cmd'] == ('explicit-command' if explicit_timeout else 'configured-command')
    assert seen['popen'][0][0] == record['cmd'] and record['exit'] == 0
    assert waits == [expected_minutes]


def test_repo043_build_uses_worktree_metadata_for_canonical_config_and_execution(monkeypatch, tmp_path, capsys):
    state, work, seen = _build_shift(monkeypatch, tmp_path)
    canonical = tmp_path / 'canonical'
    canonical.mkdir()
    (state / 'worktree.json').write_text(json.dumps({'repo_root': str(canonical), 'worktree_dir': str(work)}))
    _repo043_file(canonical, 'config.yaml', 'build: {cmd: canonical-command}\n')
    _repo043_file(work, 'config.yaml', 'build: {cmd: stale-command}\n')
    launcher.build(['bld00001'])
    record = json.loads(capsys.readouterr().out)
    assert record['cmd'] == 'canonical-command' and record['cwd'] == str(work.resolve())
    assert seen['popen'][0][1]['cwd'] == str(work.resolve())


def test_repo043_build_without_config_still_requires_command(monkeypatch, tmp_path):
    state, work, seen = _build_shift(monkeypatch, tmp_path)
    (state / 'crew.json').write_text(json.dumps({'cwd': str(work), 'members': []}))
    with pytest.raises(SystemExit) as err:
        launcher.build(['bld00001'])
    assert err.value.code == 2 and not seen['popen']


def test_repo043_build_explicit_cwd_selects_its_own_config(monkeypatch, tmp_path, capsys):
    state, work, seen = _build_shift(monkeypatch, tmp_path)
    other = tmp_path / 'explicit cwd'
    other.mkdir()
    (state / 'crew.json').write_text(json.dumps({'cwd': str(work), 'members': []}))
    _repo043_file(work, 'config.yaml', 'build: {cmd: shift-command}\n')
    _repo043_file(other, 'config.yaml', 'build: {cmd: explicit-cwd-command}\n')
    launcher.build(['bld00001', '--cwd', str(other)])
    record = json.loads(capsys.readouterr().out)
    assert record['cmd'] == 'explicit-cwd-command' and record['cwd'] == str(other.resolve())


def test_repo043_mirror_exact_modified_untracked_excluded_deleted_and_binary(worktree_world):
    w = worktree_world
    _wt_commit(w.repo, '.nightshift/config.yaml', 'build: {cmd: old-command}\n')
    _wt_commit(w.repo, '.nightshift/deleted.md', 'remove this in canonical repo\n')
    _repo043_file(w.repo, 'config.yaml', 'build: {cmd: local-command}\n')
    (w.repo / '.nightshift' / 'deleted.md').unlink()
    _repo043_file(w.repo, 'instructions.md', 'local café instructions\n')
    _repo043_file(w.repo, 'crews/local.yaml', TRIO)
    binary = w.repo / '.nightshift' / 'ignored.bin'
    binary.write_bytes(b'\x00\xff\x00\x80')
    exclude = w.repo / '.git' / 'info' / 'exclude'
    exclude.write_text('/.nightshift/ignored.bin\n', encoding='utf-8')
    before = _wt_git(w.repo, 'status', '--porcelain', '--untracked-files=all')
    _, work, _ = _wt_start(w)
    source = {p.relative_to(w.repo / '.nightshift').as_posix(): p.read_bytes()
              for p in (w.repo / '.nightshift').rglob('*') if p.is_file()}
    mirrored = {p.relative_to(work / '.nightshift').as_posix(): p.read_bytes()
                for p in (work / '.nightshift').rglob('*') if p.is_file()}
    assert mirrored == source
    assert _wt_git(w.repo, 'status', '--porcelain', '--untracked-files=all') == before
    assert _wt_git(work, 'diff', '--cached', '--name-only') == ''


def test_repo043_excluded_local_config_visible_in_worktree_without_staging(worktree_world):
    w = worktree_world
    _repo043_file(w.repo, 'config.yaml', 'build: {cmd: local-check}\n')
    (w.repo / '.git' / 'info' / 'exclude').write_text('/.nightshift/\n', encoding='utf-8')
    assert _wt_git(w.repo, 'status', '--porcelain') == ''
    _, work, _ = _wt_start(w)
    assert (work / '.nightshift' / 'config.yaml').read_text() == 'build: {cmd: local-check}\n'
    assert _wt_git(work, 'ls-files', '.nightshift') == ''


def test_repo043_absent_canonical_layer_does_not_delete_target_branch_policy(worktree_world):
    w = worktree_world
    _wt_git(w.repo, 'checkout', '-b', 'configured-target')
    _wt_commit(w.repo, '.nightshift/instructions.md', 'target branch policy\n')
    _wt_git(w.repo, 'checkout', 'main')
    assert not (w.repo / '.nightshift').exists()
    _, work, _ = _wt_start(w, '--target-branch', 'configured-target')
    assert (work / '.nightshift' / 'instructions.md').read_text() == 'target branch policy\n'
    assert launcher.git_state(str(work))[1] is False


def test_repo043_explicit_start_target_stays_pinned_when_config_changes_before_land(worktree_world, capsys):
    w = worktree_world
    main_head = _wt_git(w.repo, 'rev-parse', 'main')
    _wt_git(w.repo, 'checkout', '-b', 'explicit-target')
    _repo043_file(w.repo, 'config.yaml', 'target_branch: main\n')
    state, work, meta = _wt_start(w, '--target-branch', 'explicit-target')
    assert meta['target_branch'] == 'explicit-target'
    sha = _wt_commit(work, 'feature.txt', 'pinned target feature\n')
    _repo043_file(w.repo, 'config.yaml', 'target_branch: different-config-target\n')
    _wt_test_commit(state, work, capsys)
    _wt_land(state, capsys)
    assert _wt_git(w.repo, 'rev-parse', 'explicit-target') == sha
    assert _wt_git(w.repo, 'rev-parse', 'main') == main_head


def test_repo043_only_exact_untracked_layer_is_ignored_not_similar_names(worktree_world):
    w = worktree_world
    _repo043_file(w.repo, 'instructions.md', 'local policy\n')
    _, work, _ = _wt_start(w)
    assert launcher.git_state(str(work))[1] is False
    (work / '.nightshift-not-config').write_text('real untracked work\n')
    _, dirty, porcelain = launcher.git_state(str(work))
    assert dirty is True and any('.nightshift-not-config' in line for line in porcelain)


def test_repo043_local_config_does_not_block_clean_build_landing_or_teardown(worktree_world, capsys):
    w = worktree_world
    _repo043_file(w.repo, 'config.yaml', 'build: {cmd: local-check}\n')
    state, work, meta = _wt_start(w)
    assert (work / '.nightshift' / 'config.yaml').exists()
    sha = _wt_commit(work, 'feature.txt', 'tested feature\n')
    _wt_test_commit(state, work, capsys)
    _wt_land(state, capsys)
    assert _wt_git(w.repo, 'rev-parse', 'main') == sha
    assert _wt_git(w.repo, 'ls-tree', '-r', '--name-only', 'main', '.nightshift') == ''
    launcher.down([state.name])
    assert not work.exists() and not _wt_git(w.repo, 'branch', '--list', meta['branch'])
    assert (w.repo / '.nightshift' / 'config.yaml').read_text() == 'build: {cmd: local-check}\n'


@pytest.mark.parametrize('change', ['modified', 'deleted', 'staged'])
def test_repo043_tracked_config_changes_are_dirty_and_prevent_land(worktree_world, capsys, change):
    w = worktree_world
    _wt_commit(w.repo, '.nightshift/instructions.md', 'tracked repository policy\n')
    state, work, _ = _wt_start(w)
    _wt_test_commit(state, work, capsys)
    policy = work / '.nightshift' / 'instructions.md'
    if change == 'deleted':
        policy.unlink()
    else:
        policy.write_text('changed tracked policy\n')
        if change == 'staged':
            _wt_git(work, 'add', '.nightshift/instructions.md')
    assert launcher.git_state(str(work))[1] is True
    with pytest.raises(SystemExit):
        launcher.land([state.name])
    assert not (state / 'landed.json').exists()


@pytest.mark.parametrize('tracked', [False, True])
def test_repo043_recovery_never_commits_local_config_but_preserves_tracked_edits(worktree_world, capsys, tracked):
    w = worktree_world
    if tracked:
        _wt_commit(w.repo, '.nightshift/instructions.md', 'old tracked policy\n')
    _repo043_file(w.repo, 'config.yaml', 'build: {cmd: local-check}\n')
    state, work, meta = _wt_start(w)
    if tracked:
        (work / '.nightshift' / 'instructions.md').write_text('recovered tracked policy\n')
    (work / 'shared.txt').write_text('recovered feature\n')
    launcher.down([state.name])
    assert not work.exists()
    assert _wt_git(w.repo, 'show', meta['branch'] + ':shared.txt') == 'recovered feature'
    paths = _wt_git(w.repo, 'ls-tree', '-r', '--name-only', meta['branch'], '.nightshift').splitlines()
    assert paths == (['.nightshift/instructions.md'] if tracked else [])
    if tracked:
        assert _wt_git(w.repo, 'show', meta['branch'] + ':.nightshift/instructions.md') == 'recovered tracked policy'


def test_repo043_untracked_config_only_down_does_not_make_recovery_commit(worktree_world):
    w = worktree_world
    _repo043_file(w.repo, 'instructions.md', 'local only\n')
    state, work, meta = _wt_start(w)
    before = _wt_git(work, 'rev-parse', 'HEAD')
    launcher.down([state.name])
    assert not work.exists()
    assert _wt_git(w.repo, 'rev-parse', meta['branch']) == before


@pytest.mark.usefixtures('_container_isolated')
def test_repo043_spawn_crew_lookup_uses_canonical_root_not_process_cwd(tmp_path, injected, capsys):
    state = _container(tmp_path)
    canonical, work = tmp_path / 'canonical', tmp_path / 'work'
    canonical.mkdir()
    work.mkdir()
    (state / 'worktree.json').write_text(json.dumps({'repo_root': str(canonical), 'worktree_dir': str(work)}))
    _repo043_file(canonical, 'crews/local043.yaml', 'roles:\n'
                  '  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                  '  - {role: ReviewBot, brief: coder.md, model: local-model}\n')
    launcher.spawn([CONT_SHIFT, '--role', 'ReviewBot', '--crew', 'local043'])
    result = json.loads(capsys.readouterr().out)
    member = json.loads((state / 'crew.json').read_text())['members'][-1]
    assert result['role'] == 'ReviewBot' and member['model'] == 'local-model'
    assert len(injected.inputs) == 1


def test_repo043_pane_fallback_manifest_and_prompt_use_repo_root(monkeypatch, tmp_path):
    _repo043_file(tmp_path, 'crews/default.yaml', 'model: repo-model\nroles:\n'
                  '  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                  '  - {role: Coder, brief: coder.md}\n')
    _repo043_file(tmp_path, 'instructions.md', 'PANE-REPO-POLICY')
    seen, _ = _capture_role_launch(monkeypatch, tmp_path, 'Coder', str(tmp_path))
    command = seen['cmd']
    assert command[command.index('--model') + 1] == 'repo-model'
    assert 'PANE-REPO-POLICY' in command[command.index('--append-system-prompt') + 1]

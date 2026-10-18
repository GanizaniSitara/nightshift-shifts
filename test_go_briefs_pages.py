"""NGH-063 one-page briefs: crews render within budget, the files say what the contract asks, and `briefs: legacy`
reproduces NGH-062 (native only).

Contract: C:/nightshift/shifts/<shift>/contract-ngh063.md. The legacy goldens were recorded from a binary built at
26817ec (NGH-062 landed) with NIGHTSHIFT_REBASE_GOLDENS=1 and NIGHTSHIFT_GO_EXE pointing at it; that binary ignores the
`briefs` key and renders the then-current files, which NGH-063 moves to briefs/legacy and personas/legacy unchanged.
Fixtures are the launcher's start and pane worlds: --dry-run only, stub tools only, nothing launched.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from test_go_launcher import (REBASE, REPO, exe_path, fake_bin, frozen, normalise, pane_observation, pane_world,
                              python_launcher, run as launcher_run, sleeper, start_world, unported, write_json)
from test_go_launcher import assert_equal_bytes  # noqa: F401  (re-exported by the launcher tests)


BRIEFS, PERSONAS, CREWS = REPO / 'briefs', REPO / 'personas', REPO / 'crews'
WARNING = re.compile(r'^brief (\S+): (role|repository|site) layer (\d+) words, budget (\d+)$')
CREW_NAMES = sorted(p.stem for p in CREWS.glob('*.yaml'))
BUDGETED = ('default', 'work', 'codex', 'copilot')


def crew_roles(name):
    return yaml.safe_load((CREWS / f'{name}.yaml').read_text(encoding='utf-8'))['roles']


def page(name):
    return (BRIEFS / name).read_text(encoding='utf-8')


def current_briefs():
    return sorted(p for p in BRIEFS.glob('*.md'))


def current_personas():
    return sorted(PERSONAS.glob('*.yaml'))


# ---------------------------------------------------------------- files

def test_legacy_sets_exist_and_new_set_has_no_tool_variants():
    legacy = {p.name for p in (BRIEFS / 'legacy').glob('*.md')}
    assert {'common.md', 'foreman.md', 'coder.md', 'tester.md', 'coder-cli.md', 'foreman-agy-coder.md'} <= legacy, legacy
    assert {p.name for p in (PERSONAS / 'legacy').glob('*.yaml')} >= {'foreman.yaml', 'coder.yaml', 'tester.yaml'}
    names = {p.name for p in current_briefs()}
    assert 'common.md' not in names, 'common.md is retired from the new set'
    assert not [n for n in names if re.search(r'-cli\.md$|-agy\.md$|^foreman-agy-coder\.md$', n)], names
    assert {'foreman.md', 'coder.md', 'apprentice.md', 'tester.md', 'advisor.md', 'decompiler.md', 'researcher.md',
            'uitester.md'} <= names, names


@pytest.mark.parametrize('name', CREW_NAMES)
def test_crew_brief_references_are_one_file_per_role(name):
    for role in crew_roles(name):
        brief = role.get('brief', '')
        assert not re.search(r'-cli\.md$|-agy\.md$|foreman-agy-coder', brief), (name, role)
        assert (BRIEFS / brief).is_file(), (name, brief)


def test_specialists_researcher_has_its_own_page():
    researcher = next(r for r in crew_roles('specialists') if r['role'] == 'Researcher')
    assert researcher['brief'] == 'researcher.md'


@pytest.mark.parametrize('path', current_personas(), ids=lambda p: p.name)
def test_personas_are_identity_only(path):
    text = path.read_text(encoding='utf-8')
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    assert len(lines) <= 4, (path.name, lines)
    assert not re.search(r'nightshift|\{nightshift\}|`|msg send|--\w|\bclaim\b|\bspawn\b|\back\b', text, re.I), \
        (path.name, text)
    data = yaml.safe_load(text)
    assert isinstance(data, dict) and data.get('identity'), path.name


@pytest.mark.parametrize('path', current_briefs() + current_personas(), ids=lambda p: p.name)
def test_no_does_not_apply_and_no_claude_only_prose(path):
    text = path.read_text(encoding='utf-8')
    assert 'does not apply' not in text.lower(), path.name
    for claude_only in ('ListAgents', 'SendMessage', 'agents-cli'):
        assert claude_only not in text, (path.name, claude_only)   # the renderer emits per-tool lines


def test_foreman_page_has_event_rules_and_commands():
    text = page('foreman.md')
    for event in ('member-died', 'loop-detected', 'asg-timeout'):
        assert event in text, event
    assert 'handover' in text and re.search(r'context', text, re.I), 'context exhaustion -> handover'
    assert re.search(r'respawn', text) and re.search(r'BLOCKED', text) and re.search(r're-?dispatch', text, re.I)
    for command in ('assign', 'wait', 'note', 'commit', 'land', 'result'):
        assert re.search(r'\{nightshift\} ' + command + r'\b', text), command
    lowered = text.lower()
    assert 'diff' in lowered and re.search(r'never (touch |edit )?(the )?code|never edit', lowered), \
        'read the diff; between events scope, never touch the code'


def test_decompiler_keeps_the_missing_tool_rule():
    text = page('decompiler.md')
    assert 'BLOCKED' in text and re.search(r'tool', text, re.I) and re.search(r'missing|not found|absent', text, re.I)


def test_tester_page_has_the_verdict_template():
    text = page('tester.md')
    for needle in ('TESTED-PASS', 'TESTED-FAIL', 'builds/<n>.json'):
        assert needle in text, needle
    assert re.search(r'test files', text, re.I) and re.search(r'\{nightshift\} build', text)


@pytest.mark.parametrize('name', ['coder.md', 'apprentice.md', 'tester.md', 'foreman.md', 'advisor.md',
                                  'decompiler.md', 'researcher.md', 'uitester.md'])
def test_new_pages_use_placeholders_not_literal_paths(name):
    text = page(name)
    assert 'C:/Tools/nightshift.exe' not in text and 'C:\\Tools' not in text, name
    assert not re.search(r'Nightshift-shift-[0-9a-f]{8}', text), name


# ---------------------------------------------------------------- every crew renders, within budget

def start_crew(w, name):
    return launcher_run(['start', '--dry-run', '--cwd', str(w['plain']), '--crew', name, 'Pages task'], w['env'], True)


@pytest.mark.parametrize('name', CREW_NAMES)
def test_every_crew_renders_with_start_dry_run(start_world, name):
    proc = start_crew(start_world, name)
    assert proc.returncode == 0, (name, proc.stderr)
    roles = json.loads(proc.stdout)['roles']
    for label, r in roles.items():
        assert r['brief_mode'] in ('standard', 'compact'), (label, r)
        assert r['brief_layers']['role'] > 0, (name, label)


@pytest.mark.parametrize('name', BUDGETED)
def test_budgeted_crews_have_no_role_layer_warnings(start_world, name):
    proc = start_crew(start_world, name)
    assert proc.returncode == 0, proc.stderr
    roles = json.loads(proc.stdout)['roles']
    over = {label: [x for x in r['brief_warnings'] if WARNING.match(x) and ' role layer ' in x]
            for label, r in roles.items()}
    assert not any(over.values()), over
    stderr_role = [line for line in proc.stderr.decode('utf-8').splitlines() if ' role layer ' in line]
    assert not stderr_role, stderr_role


# ---------------------------------------------------------------- briefs: legacy reproduces NGH-062

@pytest.mark.parametrize('label,slot', [('Apprentice', 5), ('Coder', 3), ('Tester-2', 4)])
def test_legacy_briefs_reproduce_ngh062(tmp_path, fake_bin, sleeper, python_launcher, label, slot):
    base = tmp_path / 'legacy pane space'
    env, d, repo, record = pane_world(base, fake_bin, sleeper)
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    write_json(d / 'crew.json', {**crew, 'briefs': 'legacy'})
    args = ['--pane', label, '--state-dir', str(d), '--start', '--cwd', str(repo), '--slot', str(slot)]

    def observe():
        proc = launcher_run(args, env, True, timeout=120)
        if unported(proc):
            pytest.skip('native binary has not ported --pane')
        assert proc.returncode == 0, proc.stderr
        info, slot_record, calls = pane_observation(env, d, record, label, slot)
        text = json.dumps([proc.stdout.decode(), proc.stderr.decode(), info, slot_record, calls],
                          ensure_ascii=False, indent=1)
        return normalise(text.encode(), python_launcher, True).decode('utf-8')

    def recorded():
        if not REBASE:
            pytest.fail('legacy goldens are recorded from the 26817ec binary only')
        return observe()
    expected = frozen(recorded, tmp_path, {'SLEEPER_PID': sleeper[0]}, record=REBASE)
    if REBASE:
        return
    actual = observe()
    assert actual == expected, f'briefs: legacy differs from NGH-062 for {label}'


def test_legacy_assets_are_embedded(start_world, tmp_path):
    """A copied binary in an empty folder renders a `briefs: legacy` crew from the embedded legacy sets."""
    lonely = tmp_path / 'empty folder legacy'
    lonely.mkdir()
    copied = lonely / 'nightshift.exe'
    shutil.copyfile(exe_path(), copied)
    w = start_world
    crews = w['configured'] / '.nightshift' / 'crews'
    (crews / 'oldbriefs.yaml').write_text(
        'name: oldbriefs\nlayout: 1x4\nbriefs: legacy\nroles:\n'
        '  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
        '  - {role: Coder, brief: coder-cli.md, tool: codex, model: gpt-6.1-sol}\n'
        '  - {role: Tester, brief: tester.md}\n', encoding='utf-8')
    proc = subprocess.run([str(copied), 'start', '--dry-run', '--cwd', str(w['configured']), '--crew', 'oldbriefs', 'x'],
                          cwd=lonely, env=w['env'], capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                          creationflags=subprocess.CREATE_NO_WINDOW)
    assert proc.returncode == 0, proc.stderr
    roles = json.loads(proc.stdout)['roles']
    # The legacy pages are the long ones: well over the new 250-word role budget.
    assert roles['Foreman']['brief_layers']['role'] > 1000, roles['Foreman']['brief_layers']
    assert sorted(p.name for p in lonely.iterdir()) == ['nightshift.exe']


# ---------------------------------------------------------------- c9: the compact Apprentice's breaker feeds needs-senior

@pytest.mark.parametrize('first_line,valid', [('APPRENTICE-NEEDS-SENIOR: the change needs a second file', True),
                                              ('BLOCKED: the change needs a second file', False)])
def test_compact_apprentice_breaker_report_makes_needs_senior_uplift_valid(tmp_path, first_line, valid):
    """End to end: the report the compact Apprentice's circuit breaker tells it to send (brief_pages_test.go pins the
    rendered line) is stored as the note the needs-senior trigger reads; the old BLOCKED wording is not."""
    from test_go_bus_commands import bus_world, cli, header, insert, rows
    env = bus_world(tmp_path / 'needs senior')
    insert(env, 'assignment', 'Boss', 'Junior', asg='code-a', state='acknowledged')   # Junior is the Apprentice
    msg_id, kind, asg = header(cli(env, 'report', 'alpha', 'Junior', first_line))
    assert asg == 'code-a' and kind == ('note' if valid else 'result')
    header(cli(env, 'ready', 'alpha', 'Senior'))
    proc = cli(env, 'uplift', 'alpha', 'code-a', 'take it over', '--reason', 'needs-senior')
    if valid:
        uplift_id, kind, asg = header(proc)
        row = next(r for r in rows(env) if r['id'] == uplift_id)
        assert (kind, asg, row['recipient'], row['reason']) == ('uplift', 'code-a', 'Senior', 'needs-senior')
        assert str(msg_id) in row['evidence']
    else:
        assert proc.returncode == 4 and b'uplift trigger not satisfied: needs-senior' in proc.stderr, proc.stderr

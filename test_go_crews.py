"""NGH-056: crew lookup (machine > repo > built-in), `nightshift crews`, `start --crew-file` and the start-time
overrides (--without, --role, --max-credits), recorded in the dry-run plan; plus crews/work.yaml.

Native only: nightshift_shifts.py is frozen, so there is no Python oracle for these flags. Expectations come from
the NGH-056 spec (C:/nightshift/shifts/Nightshift-shift-0b127660/spec-ngh056.md). Every case runs in a disposable
NIGHTSHIFT_STATE_ROOT, HOME and Git repos, and every start is --dry-run; spawn cases are --dry-run too and only
read an already open Terminal window (skipped when none is open). Nothing here launches a window or a session.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from test_go_launcher import (CREWS, DEAD_PID, INVALID_HWND, base_env, exe_path, fake_bin,  # noqa: F401
                              make_repo, real_terminal_hwnd, sleeper, state_snapshot)
from test_go_readonly import REPO, write_json

BUILT_IN = sorted(p.stem for p in (REPO / 'crews').glob('*.yaml'))
FLASH_ROLES = 'Foreman, Advisor, Apprentice, Coder, Tester'   # crews/copilot-flash.yaml, manifest order

MIX = '''name: not-the-stem
model: claude-opus-5-5
layout: 2x4
max_credits: 60
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true, colour: red}
  - {role: Coder, brief: coder-cli.md, colour: green, tool: copilot, model: gemini-3.8-flash, reasoning: high, context: long_context, permissions: skip}
  - {role: Tester, brief: tester-cli.md, colour: blue, tool: copilot, model: gemini-3.8-flash, reasoning: high, context: long_context, account: 2}
'''
PAIR = '''name: pair
layout: 1x4
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true}
  - {role: Coder, brief: coder.md}
'''
NO_HUMAN = '''name: headless
roles:
  - {role: Coder, brief: coder.md}
  - {role: Tester, brief: tester.md}
'''
# A crew only reachable through a recorded crew_file; its Tester is purple (spawn's default colour is yellow).
SPAWNABLE = '''name: spawnable
layout: 1x4
roles:
  - {role: Foreman, brief: foreman.md, human_contact: true}
  - {role: Tester, brief: tester.md, colour: purple}
'''


def codex_variant(model):
    return ('name: codex\nlayout: 2x4\nroles:\n'
            f'  - {{role: Foreman, brief: foreman-cli.md, human_contact: true, tool: codex, model: {model}}}\n'
            '  - {role: Coder, brief: coder.md}\n')


def native(args, env, cwd=REPO, timeout=60):
    if not exe_path().is_file():
        pytest.fail(f'standalone executable missing: {exe_path()}; build it first')
    return subprocess.run([str(exe_path()), *args], cwd=cwd, env=env, capture_output=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@pytest.fixture
def world(tmp_path):
    base = tmp_path / 'crews café'
    env = base_env(base, None)
    machine = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'crews'
    machine.mkdir(parents=True)
    repo = make_repo(base / 'repo space')
    (repo / '.nightshift' / 'crews').mkdir(parents=True)
    plain = base / 'plain folder'
    plain.mkdir()
    files = base / 'crew files'
    files.mkdir()
    for name, text in (('mixfile', MIX), ('pairfile', PAIR), ('headless', NO_HUMAN), ('spawnable', SPAWNABLE)):
        (files / f'{name}.yaml').write_text(text, encoding='utf-8')
    return {'env': env, 'machine': machine, 'repo': repo, 'repo_crews': repo / '.nightshift' / 'crews',
            'plain': plain, 'files': files, 'base': base}


def start(w, *extra, where='plain', cwd=REPO):
    return native(['start', '--dry-run', '--cwd', str(w[where]), *extra, 'Crew override task'], w['env'], cwd=cwd)


def plan_of(w, *extra, **kw):
    before = state_snapshot(w['env'])
    proc = start(w, *extra, **kw)
    assert proc.returncode == 0, (extra, proc.stderr.decode('utf-8', 'replace'))
    assert state_snapshot(w['env']) == before, f'start --dry-run {extra} wrote state'
    return json.loads(proc.stdout)


def refused(w, *extra, code=1, **kw):
    proc = start(w, *extra, **kw)
    err = proc.stderr.decode('utf-8', 'replace')
    assert proc.returncode == code, (extra, proc.returncode, err)
    assert proc.stdout == b'', (extra, proc.stdout)
    return err


def copilot_shares(plan):
    return {label: r.get('max_credits') for label, r in plan['roles'].items() if r.get('tool') == 'copilot'}


# ---------------------------------------------------------------- 1. lookup order


def test_machine_shadows_repo_shadows_builtin(world):
    w = world
    machine_file = w['machine'] / 'codex.yaml'
    machine_file.write_text(codex_variant('gpt-machine'), encoding='utf-8')
    (w['repo_crews'] / 'codex.yaml').write_text(codex_variant('gpt-repo'), encoding='utf-8')

    plan = plan_of(w, '--crew', 'codex', where='repo')
    assert plan['roles']['Foreman']['model'] == 'gpt-machine'
    assert list(plan['roles']) == ['Foreman', 'Coder']
    assert set(plan['overrides']) == {'crew_file'} and same_path(plan['overrides']['crew_file'], machine_file)

    machine_file.unlink()
    plan = plan_of(w, '--crew', 'codex', where='repo')
    assert plan['roles']['Foreman']['model'] == 'gpt-repo'
    assert 'overrides' not in plan, 'the repo layer is not a machine crew file'

    plan = plan_of(w, '--crew', 'codex', where='plain')   # no repo layer outside the repo
    assert plan['roles']['Foreman']['model'] == 'gpt-6.1-sol' and 'overrides' not in plan


def test_machine_default_crew_is_used_without_crew_flag(world):
    w = world
    (w['machine'] / 'default.yaml').write_text(PAIR, encoding='utf-8')
    plan = plan_of(w)
    assert list(plan['roles']) == ['Foreman', 'Coder']
    assert same_path(plan['overrides']['crew_file'], w['machine'] / 'default.yaml')
    assert 'crew' not in plan, 'the crew is still named default'


def test_machine_crew_is_found_outside_any_repo(world):
    (world['machine'] / 'onlymachine.yaml').write_text(PAIR, encoding='utf-8')
    plan = plan_of(world, '--crew', 'onlymachine')
    assert plan['crew'] == 'onlymachine' and list(plan['roles']) == ['Foreman', 'Coder']


def test_unknown_crew_lists_all_three_sources_once(world):
    w = world
    (w['machine'] / 'onlymachine.yaml').write_text(PAIR, encoding='utf-8')
    (w['machine'] / 'codex.yaml').write_text(PAIR, encoding='utf-8')
    (w['repo_crews'] / 'onlyrepo.yaml').write_text(PAIR, encoding='utf-8')
    (w['repo_crews'] / 'codex.yaml').write_text(PAIR, encoding='utf-8')
    err = refused(w, '--crew', 'nosuch', where='repo')
    assert 'no crew manifest' in err and 'available crews: ' in err, err
    listed = err.split('available crews: ', 1)[1].strip().split(', ')
    assert listed == sorted(set(BUILT_IN) | {'onlymachine', 'onlyrepo'}), listed


# ---------------------------------------------------------------- 2. nightshift crews


def crews_listing(w, *extra, cwd=REPO):
    before = state_snapshot(w['env'])
    proc = native(['crews', *extra], w['env'], cwd=cwd)
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')
    assert state_snapshot(w['env']) == before, 'crews is read-only'
    return json.loads(proc.stdout)


def test_crews_lists_every_manifest_with_source_and_active(world):
    w = world
    for d, names in ((w['machine'], ('codex', 'onlymachine')), (w['repo_crews'], ('codex', 'default', 'onlyrepo'))):
        for n in names:
            (d / f'{n}.yaml').write_text(PAIR, encoding='utf-8')
    listing = crews_listing(w, '--cwd', str(w['repo']))
    assert all(set(e) == {'name', 'source', 'path', 'active'} for e in listing), listing
    names = [e['name'] for e in listing]
    assert names == sorted(names), 'sorted by name'
    by = {}
    for e in listing:
        assert e['source'] in ('machine', 'repo', 'built-in'), e
        assert (e['name'], e['source']) not in by, f'duplicate entry {e}'
        by[(e['name'], e['source'])] = e
    expected = {(n, 'built-in') for n in BUILT_IN} | {('codex', 'machine'), ('onlymachine', 'machine'),
                                                      ('codex', 'repo'), ('default', 'repo'), ('onlyrepo', 'repo')}
    assert set(by) == expected
    active = {k for k, e in by.items() if e['active']}
    assert active == ({(n, 'built-in') for n in BUILT_IN if n not in ('codex', 'default')} |
                      {('codex', 'machine'), ('onlymachine', 'machine'), ('default', 'repo'), ('onlyrepo', 'repo')})
    assert sum(e['active'] for e in listing if e['name'] == 'codex') == 1
    assert same_path(by[('codex', 'machine')]['path'], w['machine'] / 'codex.yaml')
    assert same_path(by[('codex', 'repo')]['path'], w['repo_crews'] / 'codex.yaml')
    for n in BUILT_IN:
        assert by[(n, 'built-in')]['path'].replace('\\', '/').endswith(f'crews/{n}.yaml')
    assert by[('work', 'built-in')]['active'] is True


def test_crews_cwd_decides_the_repo_layer(world):
    w = world
    (w['repo_crews'] / 'onlyrepo.yaml').write_text(PAIR, encoding='utf-8')
    outside = crews_listing(w, '--cwd', str(w['plain']))
    assert {e['source'] for e in outside} == {'built-in'}
    assert all(e['active'] for e in outside)
    implicit = crews_listing(w, cwd=w['repo'])   # default --cwd: the current directory
    assert ('onlyrepo', 'repo', True) in {(e['name'], e['source'], e['active']) for e in implicit}
    assert implicit == crews_listing(w, '--cwd', str(w['repo']))


def test_crews_help_and_registry(world):
    proc = native(['crews', '-h'], world['env'])
    assert proc.returncode == 0 and proc.stdout.startswith(b'usage: nightshift crews'), proc
    assert b'--cwd' in proc.stdout
    top = native(['-h'], world['env'])
    assert top.returncode == 0 and b'crews' in top.stdout, top.stdout[:400]


# ---------------------------------------------------------------- 3. start --crew-file


def test_crew_file_absolute_names_crew_by_stem(world):
    path = world['files'] / 'mixfile.yaml'
    plan = plan_of(world, '--crew-file', str(path))
    assert plan['crew'] == 'mixfile'
    assert list(plan['roles']) == ['Foreman', 'Coder', 'Tester']
    assert plan['max_credits'] == 60 and copilot_shares(plan) == {'Coder': 30, 'Tester': 30}
    assert plan['overrides'] == {'crew_file': plan['overrides']['crew_file']}
    assert same_path(plan['overrides']['crew_file'], path) and os.path.isabs(plan['overrides']['crew_file'])


def test_crew_file_relative_resolves_against_current_dir(world):
    plan = plan_of(world, '--crew-file', 'mixfile.yaml', cwd=world['files'])
    assert plan['crew'] == 'mixfile'
    assert os.path.isabs(plan['overrides']['crew_file'])
    assert same_path(plan['overrides']['crew_file'], world['files'] / 'mixfile.yaml')


def test_crew_and_crew_file_together_is_a_usage_error(world):
    err = refused(world, '--crew', 'codex', '--crew-file', str(world['files'] / 'mixfile.yaml'), code=2)
    assert err.startswith('usage: nightshift start'), err
    assert 'give either --crew or --crew-file, not both' in err


def test_missing_crew_file(world):
    missing = world['files'] / 'nope.yaml'
    err = refused(world, '--crew-file', str(missing))
    assert err.strip().startswith('no crew manifest '), err
    assert same_path(err.strip().removeprefix('no crew manifest ').split(';')[0].strip(), missing)
    rel = refused(world, '--crew-file', 'nope.yaml', cwd=world['files'])
    assert same_path(rel.strip().removeprefix('no crew manifest ').split(';')[0].strip(), missing), rel


def test_crew_file_gets_ordinary_manifest_validation(world):
    err = refused(world, '--crew-file', str(world['files'] / 'headless.yaml'))
    assert 'crew headless: exactly one role needs human_contact: true, found 0' in err, err


# ---------------------------------------------------------------- 4. --without


def test_without_drops_role_and_resplits_budget(world):
    plan = plan_of(world, '--crew', 'copilot-flash', '--without', 'Tester')
    assert list(plan['roles']) == ['Foreman', 'Advisor', 'Apprentice', 'Coder']
    assert plan['max_credits'] == 120 and copilot_shares(plan) == {'Foreman': 40, 'Apprentice': 40, 'Coder': 40}
    assert plan['overrides'] == {'without': ['Tester']}
    # Advisor (slot 2) and Coder (slot 4, the crew still has an Apprentice) are reserved
    assert [s['role'] for s in plan['slots']][:4] == ['Foreman', None, 'Apprentice', None]
    assert plan['roles']['Coder']['reserved_slot'] == 4
    # ops_width: 2 merges the bottom-right two cells into the Ops pane, so 7 slots
    assert len(plan['slots']) == 7 and 'Tester' not in [s['role'] for s in plan['slots']]


def test_without_is_case_insensitive_and_idempotent(world):
    plan = plan_of(world, '--crew', 'copilot-flash', '--without', 'tester', '--without', 'TESTER', '--without', 'Coder')
    assert list(plan['roles']) == ['Foreman', 'Advisor', 'Apprentice']
    assert copilot_shares(plan) == {'Foreman': 60, 'Apprentice': 60}
    assert 'without' in plan['overrides']


def test_without_unknown_role(world):
    err = refused(world, '--crew', 'copilot-flash', '--without', 'Wizard')
    assert f'--without Wizard: crew copilot-flash has no role Wizard; roles: {FLASH_ROLES}' in err, err


def test_without_human_contact_role(world):
    err = refused(world, '--crew', 'copilot-flash', '--without', 'Foreman')
    assert '--without Foreman: the human_contact role cannot be dropped' in err, err


def test_without_leaving_one_pane_fails_pane_count(world):
    err = refused(world, '--crew-file', str(world['files'] / 'pairfile.yaml'), '--without', 'Coder')
    assert 'crew pairfile: 1 panes; a crew has 2 to 8' in err, err


def test_without_last_copilot_pane_drops_budget(world):
    plan = plan_of(world, '--crew', 'claude-copilot', '--without', 'Apprentice')
    assert 'max_credits' not in plan, 'budget dropped silently when no Copilot pane remains'
    assert all('max_credits' not in r for r in plan['roles'].values())
    assert plan['overrides'] == {'without': ['Apprentice']}


def test_without_last_copilot_pane_with_explicit_budget_is_refused(world):
    err = refused(world, '--crew', 'claude-copilot', '--without', 'Apprentice', '--max-credits', '30')
    assert 'max_credits is a Copilot credit budget, but the crew has no copilot panes' in err, err


# ---------------------------------------------------------------- 4. --role


def test_role_claude_to_copilot_on_default(world):
    plan = plan_of(world, '--role', 'Coder=copilot:gemini-3.8-flash:high')
    coder = plan['roles']['Coder']
    assert (coder['tool'], coder['model'], coder['reasoning']) == ('copilot', 'gemini-3.8-flash', 'high')
    assert 'context' not in coder and 'max_credits' not in coder and 'max_credits' not in plan
    assert '--reasoning-effort' in coder['command'] and '--max-ai-credits' not in coder['command']
    assert coder['permission_flags'] == ['--allow-all']   # crew permission_mode auto
    assert plan['roles']['Apprentice']['tool'] == 'agy', 'other roles untouched'
    assert plan['overrides'] == {'role': ['Coder=copilot:gemini-3.8-flash:high']}


def test_role_copilot_to_claude_drops_context_keeps_permissions(world):
    plan = plan_of(world, '--crew', 'copilot-flash', '--role', 'Coder=claude:claude-opus-5-5')
    coder = plan['roles']['Coder']
    assert 'tool' not in coder and coder['model'] == 'claude-opus-5-5'   # crew model is gemini, so shown
    for gone in ('reasoning', 'context', 'max_credits', 'command'):
        assert gone not in coder, gone
    assert coder['permission_flags'] == ['--dangerously-skip-permissions'], 'permissions: skip kept'
    assert copilot_shares(plan) == {'Foreman': 40, 'Apprentice': 40, 'Tester': 40}


def test_role_without_reasoning_removes_it_and_keeps_context(world):
    plan = plan_of(world, '--crew', 'claude-copilot', '--role', 'Apprentice=copilot:gemini-3.8-flash')
    app = plan['roles']['Apprentice']
    assert 'reasoning' not in app and '--reasoning-effort' not in app['command']
    assert app['context'] == 'long_context' and app['max_credits'] == 30
    assert app['permission_flags'] == ['--allow-all']


def test_role_to_codex_drops_context_keeps_account_and_resplits(world):
    plan = plan_of(world, '--crew-file', str(world['files'] / 'mixfile.yaml'),
                   '--role', 'Tester=codex:gpt-5.6-terra:xhigh')
    tester = plan['roles']['Tester']
    assert (tester['tool'], tester['model'], tester['reasoning'], tester['account']) == \
        ('codex', 'gpt-5.6-terra', 'xhigh', 2)
    assert 'context' not in tester and 'max_credits' not in tester
    assert copilot_shares(plan) == {'Coder': 60}
    assert list(plan['overrides']) == ['crew_file', 'role']


def test_role_is_case_insensitive_on_role_name(world):
    plan = plan_of(world, '--role', 'coder=codex:gpt-5.6-terra:high')
    assert plan['roles']['Coder']['tool'] == 'codex' and 'coder' not in plan['roles']


@pytest.mark.parametrize('spec', ['Coder', 'Coder=copilot', 'Coder=copilot:', 'Coder=:gemini-3.8-flash',
                                  '=copilot:gemini-3.8-flash', 'Coder:copilot:gemini-3.8-flash'])
def test_role_bad_syntax(world, spec):
    err = refused(world, '--role', spec)
    assert f'--role {spec}: use ROLE=TOOL:MODEL[:REASONING]' in err, err


def test_role_unknown_role(world):
    err = refused(world, '--crew', 'copilot-flash', '--role', 'Wizard=claude:claude-opus-5-5')
    assert err.startswith('--role ') and f'crew copilot-flash has no role Wizard; roles: {FLASH_ROLES}' in err, err


def test_role_on_role_dropped_by_without(world):
    err = refused(world, '--crew', 'copilot-flash', '--without', 'Tester', '--role', 'Tester=claude:claude-opus-5-5')
    assert err.startswith('--role ') and 'crew copilot-flash has no role Tester' in err, err


def test_role_given_twice_is_refused(world):
    err = refused(world, '--role', 'Coder=claude:claude-opus-5-5', '--role', 'coder=codex:gpt-5.6-terra')
    assert '--role' in err, err


def test_role_unsupported_tool(world):
    err = refused(world, '--role', 'Coder=qwen:qwen3')
    assert "tool 'qwen' for Coder is not supported; use one of: claude, agy, codex, copilot" in err, err


def test_role_reasoning_checked_per_tool(world):
    err = refused(world, '--role', 'Coder=codex:gpt-5.6-terra:max')
    assert "reasoning 'max' for Coder (tool codex) must be one of: low, medium, high, xhigh" in err, err
    plan = plan_of(world, '--role', 'Coder=copilot:gemini-3.8-flash:max')   # copilot accepts max
    assert plan['roles']['Coder']['reasoning'] == 'max'


def test_role_moving_last_copilot_pane_away_drops_budget(world):
    plan = plan_of(world, '--crew', 'work', '--role', 'Coder=claude:claude-opus-5-5',
                   '--role', 'Tester=claude:claude-opus-5-5')
    assert 'max_credits' not in plan and copilot_shares(plan) == {}


# ---------------------------------------------------------------- 4. --max-credits


def test_max_credits_replaces_budget(world):
    plan = plan_of(world, '--crew', 'copilot-flash', '--max-credits', '200')
    assert plan['max_credits'] == 200 and set(copilot_shares(plan).values()) == {50}
    assert plan['overrides'] == {'max_credits': 200}
    assert plan['roles']['Coder']['command'][plan['roles']['Coder']['command'].index('--max-ai-credits') + 1] == '50'


def test_max_credits_share_below_minimum(world):
    err = refused(world, '--crew', 'copilot-flash', '--max-credits', '90')
    assert "max_credits 90 over 4 Copilot panes is 22 each; Copilot's minimum --max-ai-credits is 30 per pane" in err


def test_max_credits_not_an_integer(world):
    proc = start(world, '--crew', 'copilot-flash', '--max-credits', 'lots')
    assert proc.returncode != 0 and proc.stdout == b''
    assert b'max_credits (the shift budget) must be a whole number' in proc.stderr, proc.stderr


def test_max_credits_without_copilot_panes(world):
    err = refused(world, '--max-credits', '60')
    assert 'crew default: max_credits is a Copilot credit budget, but the crew has no copilot panes' in err, err


def test_max_credits_gives_a_budget_to_a_role_override(world):
    plan = plan_of(world, '--role', 'Coder=copilot:gemini-3.8-flash:high', '--max-credits', '30')
    assert plan['max_credits'] == 30 and copilot_shares(plan) == {'Coder': 30}
    assert plan['overrides'] == {'role': ['Coder=copilot:gemini-3.8-flash:high'], 'max_credits': 30}


# ---------------------------------------------------------------- 5. recording


@pytest.mark.parametrize('crew', ['default', 'copilot-flash', 'work'])
def test_no_overrides_key_without_override_flags(world, crew):
    assert 'overrides' not in plan_of(world, '--crew', crew)


def test_all_overrides_recorded_in_order(world):
    path = world['files'] / 'mixfile.yaml'
    plan = plan_of(world, '--crew-file', str(path), '--without', 'Tester',
                   '--role', 'Coder=copilot:gemini-3.8-flash:medium', '--max-credits', '45')
    o = plan['overrides']
    assert list(o) == ['crew_file', 'without', 'role', 'max_credits'], o
    assert same_path(o['crew_file'], path)
    assert (o['without'], o['role'], o['max_credits']) == (['Tester'], ['Coder=copilot:gemini-3.8-flash:medium'], 45)
    assert list(plan['roles']) == ['Foreman', 'Coder']
    coder = plan['roles']['Coder']
    assert (coder['reasoning'], coder['max_credits'], plan['max_credits']) == ('medium', 45, 45)
    assert coder['context'] == 'long_context', 'copilot -> copilot keeps the context tier'


# ---------------------------------------------------------------- 6. crews/work.yaml


def test_work_crew_is_in_the_every_manifest_parametrization():
    assert 'work' in CREWS and 'work' in BUILT_IN


def test_work_crew_dry_run(world):
    plan = plan_of(world, '--crew', 'work')
    assert plan['crew'] == 'work' and plan['model'] == 'claude-opus-5-5' and plan['permission_mode'] == 'auto'
    assert list(plan['roles']) == ['Foreman', 'Advisor', 'Coder', 'Tester'], 'four roles, no Apprentice'
    assert plan['max_credits'] == 60
    for label in ('Foreman', 'Advisor'):
        r = plan['roles'][label]
        assert 'tool' not in r and 'model' not in r and 'reasoning' not in r, (label, r)
    advisor = plan['roles']['Advisor']
    assert (advisor['status'], advisor['reserved_slot']) == ('not_started', 2)
    for label in ('Coder', 'Tester'):
        r = plan['roles'][label]
        assert (r['tool'], r['model'], r['reasoning'], r['context'], r['max_credits']) == \
            ('copilot', 'gemini-3.8-flash', 'high', 'long_context', 30), label
        cmd = r['command']
        assert cmd[cmd.index('--max-ai-credits') + 1] == '30'
        assert cmd[cmd.index('--context') + 1] == 'long_context'
        assert cmd[cmd.index('--reasoning-effort') + 1] == 'high'
        assert r['permission_flags'] == ['--allow-all']
    assert [s['role'] for s in plan['slots']] == ['Foreman', None, 'Coder', 'Tester', None, None, None], 'Ops spans two cells'


def test_work_yaml_manifest_fields():
    import yaml
    data = yaml.safe_load((REPO / 'crews' / 'work.yaml').read_text(encoding='utf-8'))
    assert (data['name'], data['layout'], data['model'], data['permission_mode'], data['contract'],
            data['max_credits']) == ('work', '2x4', 'claude-opus-5-5', 'auto', 'result.json', 60)
    roles = {r['role']: r for r in data['roles']}
    assert list(roles) == ['Foreman', 'Advisor', 'Coder', 'Tester']
    assert roles['Foreman']['brief'] == 'foreman.md' and roles['Foreman']['human_contact'] is True
    assert (roles['Coder']['brief'], roles['Coder']['permissions']) == ('coder-cli.md', 'skip')
    assert (roles['Tester']['brief'], roles['Tester']['permissions']) == ('tester-cli.md', 'skip')
    assert [roles[r]['colour'] for r in roles] == ['red', 'purple', 'green', 'blue']
    assert all('reasoning' not in roles[r] for r in ('Foreman', 'Advisor'))


# ---------------------------------------------------------------- reserved Coder (crews with an Apprentice)


def test_default_crew_reserves_the_coder(world):
    plan = plan_of(world)
    coder, advisor = plan['roles']['Coder'], plan['roles']['Advisor']
    assert (coder['status'], coder['reserved_slot']) == ('not_started', 3)
    assert (advisor['status'], advisor['reserved_slot']) == ('not_started', 2)
    assert list(coder).index('status') == list(coder).index('first_message') + 1
    assert [s['role'] for s in plan['slots']] == ['Foreman', None, None, 'Tester', 'Apprentice', None, None, None]
    argv = plan['wt_argv']
    assert not any(argv[i] == '--pane' and argv[i + 1] == 'Coder' for i in range(len(argv) - 1))
    shells = [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == '--slot' and argv[i - 2] != '--start']
    assert '3' in shells, 'the Coder cell is an empty-slot shell'


@pytest.mark.parametrize('extra', [['--crew', 'work'], ['--without', 'Apprentice'],
                                   ['--crew', 'claude-copilot', '--without', 'apprentice']])
def test_coder_stays_live_without_an_apprentice(world, extra):
    plan = plan_of(world, *extra)
    coder = plan['roles']['Coder']
    assert 'status' not in coder and 'reserved_slot' not in coder
    assert 'Coder' in [s['role'] for s in plan['slots']]
    argv = plan['wt_argv']
    assert any(argv[i] == '--pane' and argv[i + 1] == 'Coder' for i in range(len(argv) - 1))


def test_role_override_keeps_the_apprentice_and_the_reservation(world):
    plan = plan_of(world, '--role', 'Apprentice=copilot:gemini-3.8-flash:high')
    assert plan['roles']['Coder']['status'] == 'not_started'


def test_split_apprentice_crew_is_promoted_to_a_slot_grid(world):
    path = world['files'] / 'splitapp.yaml'
    path.write_text('name: splitapp\nroles:\n'
                    '  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                    '  - {role: Coder, brief: coder.md}\n'
                    '  - {role: Apprentice, brief: apprentice.md}\n', encoding='utf-8')
    plan = plan_of(world, '--crew-file', str(path))
    assert [s['role'] for s in plan['slots']] == ['Foreman', None, 'Apprentice']
    assert plan['roles']['Coder']['reserved_slot'] == 2


def reserved_shift(w, name='Nightshift-shift-coderres', coder_info=None, summon=None, members=None):
    """A recorded shift with a reserved Coder (coder_slot 3) and a window that cannot exist, so nothing launches."""
    d = Path(w['env']['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / name

    def member(label, tool='claude', human=False):
        return {'role': label, 'label': label, 'identity': f'{name}-{label.lower()}', 'brief': f'{label.lower()}.md',
                'persona': None, 'colour': 'red' if human else 'green', 'human_contact': human, 'tool': tool,
                'model': 'claude-opus-5-5', 'peer': ''}
    record = {'crew': 'default', 'members': members if members is not None else
              [member('Foreman', human=True), member('Coder'), member('Tester'), member('Apprentice', 'agy')],
              'slots': 8, 'advisor_slot': None, 'coder_slot': 3, 'permission_mode': 'auto',
              'model': 'claude-opus-5-5', 'cwd': str(w['plain'])}
    if summon is not None:
        record['summon'] = summon
    write_json(d / 'crew.json', record)
    write_json(d / 'launch.json', {'hwnd': INVALID_HWND})
    if coder_info is not None:
        write_json(d / 'Coder.json', coder_info)
    return d


def summon(w, *args):
    return native(['summon', *args], w['env'])


def crew_json(d):
    return json.loads((d / 'crew.json').read_text(encoding='utf-8'))


def test_status_shows_reserved_coder_not_started(world):
    d = reserved_shift(world)
    proc = native(['status', '--all'], world['env'])
    assert proc.returncode == 0, proc.stderr
    records = json.loads(proc.stdout)
    record = next(r for r in (records if isinstance(records, list) else [records]) if r['shift'] == d.name)
    assert record['roles']['Coder'] == {'status': 'not_started', 'reserved_slot': 3, 'alive': False}


def test_summon_help(world):
    proc = summon(world, '-h')
    assert proc.returncode == 0 and proc.stdout.startswith(b'usage: nightshift summon [-h] --asg ASG'), proc


@pytest.mark.parametrize('args,message', [
    ([], 'the following arguments are required: shift, body, --asg'),
    (['coderres'], 'the following arguments are required: body, --asg'),
    (['coderres', 'Do it'], 'the following arguments are required: --asg'),
    (['coderres', '   ', '--asg', 'c-1'], 'the assignment body is empty'),
    (['coderres', 'Do it', '--asg', 'c-1', '--kind', 'bogus'], 'invalid choice'),
    (['coderres', 'Do it', '--asg', 'c-1', '--kind', 'uplift', '--reason', 'bored'], 'invalid choice'),
])
def test_summon_usage_errors(world, args, message):
    d = reserved_shift(world)
    before = state_snapshot(world['env'])
    proc = summon(world, *args)
    err = proc.stderr.decode('utf-8', 'replace')
    assert proc.returncode == 2 and err.startswith('usage: nightshift summon') and message in err, err
    assert state_snapshot(world['env']) == before and 'summon' not in crew_json(d)


@pytest.mark.parametrize('case,message', [
    ('no-coder-slot', 'has no reserved Coder'),
    ('no-coder-member', 'has no reserved Coder'),
    ('already-started', 'Coder is already started; use msg send for later assignments'),
    ('already-starting', 'Coder summon is already starting'),
])
def test_summon_refusals_before_any_launch(world, case, message):
    if case == 'no-coder-slot':
        d = reserved_shift(world)
        record = crew_json(d)
        del record['coder_slot']
        write_json(d / 'crew.json', record)
    elif case == 'no-coder-member':
        d = reserved_shift(world, members=[{'role': 'Foreman', 'label': 'Foreman', 'identity': 'x-foreman',
                                            'human_contact': True, 'tool': 'claude'},
                                           {'role': 'Tester', 'label': 'Tester', 'identity': 'x-tester',
                                            'human_contact': False, 'tool': 'claude'}])
    elif case == 'already-started':
        d = reserved_shift(world, coder_info={'pid': DEAD_PID, 'started': 1000.0})
    else:
        d = reserved_shift(world, summon={'kind': 'assignment', 'body': 'earlier', 'asg': 'c-0'})
    before = crew_json(d)
    proc = summon(world, 'coderres', 'Implement the thing', '--asg', 'c-1')
    err = proc.stderr.decode('utf-8', 'replace')
    assert proc.returncode == 1 and message in err, err
    if case == 'no-coder-slot' or case == 'no-coder-member':
        assert f'shift {d.name} has no reserved Coder' in err
    assert crew_json(d) == before


@pytest.mark.parametrize('args,message', [
    (['--reason', 'needs-senior'], '--reason requires --kind uplift'),
    (['--kind', 'uplift'], 'uplift requires Foreman -> Coder, --asg and --reason'),
    (['--kind', 'uplift', '--reason', 'needs-senior'], 'uplift requires a current Apprentice coding assignment'),
])
def test_summon_uplift_validation_matches_msg_send(world, args, message):
    d = reserved_shift(world)
    before = crew_json(d)
    proc = summon(world, 'coderres', 'Take over', '--asg', 'c-1', *args)
    err = proc.stderr.decode('utf-8', 'replace')
    assert proc.returncode == 1 and message in err, err
    assert crew_json(d) == before, 'a refused summon records nothing'
    assert not (d / 'Coder.json').exists()


def test_summon_whose_spawn_fails_restores_crew_json(world):
    """Validation passes, the window is gone (an HWND that cannot exist), so spawn refuses before typing anything;
    crew.json must go back to the unsummoned Coder and no message may be stored for it."""
    d = reserved_shift(world)
    before = crew_json(d)
    proc = summon(world, 'coderres', 'Implement the thing', '--asg', 'c-1')
    err = proc.stderr.decode('utf-8', 'replace')
    assert proc.returncode == 1 and 'window is gone' in err, err
    assert crew_json(d) == before
    assert not (d / 'Coder.json').exists()
    db = d / 'mailbox.sqlite3'
    if db.exists():
        conn = sqlite3.connect(db)
        try:
            assert conn.execute("SELECT COUNT(*) FROM messages WHERE recipient = 'Coder'").fetchone()[0] == 0
        finally:
            conn.close()


def test_spawn_respects_the_reserved_coder_slot(world, sleeper):
    hwnd = real_terminal_hwnd()
    if hwnd is None:
        pytest.skip('spawn dry-run needs an open Windows Terminal window to check (never launched here)')
    pid, started = sleeper
    d = reserved_shift(world)
    write_json(d / 'launch.json', {'hwnd': hwnd})
    write_json(d / 'Foreman.json', {'pid': pid, 'started': started, 'tool': 'claude', 'cwd': str(world['plain'])})
    for n in range(1, 9):
        role = {1: 'Foreman'}.get(n)
        write_json(d / 'slots' / f'{n}.json', {'slot': n, 'role': role, 'cmd_pid': pid, 'started': 900.0,
                                               **({'spawned': 500.0} if role else {})})

    def spawn(*args):
        return native(['spawn', 'coderres', *args, '--dry-run'], world['env'])
    for args, message in ((['--role', 'Helper', '--slot', '3'], 'Coder slot 3 is reserved until summon'),
                          (['--role', 'Coder'], 'use summon to start the Coder'),
                          (['--role', 'Coder', '--slot', '4'], 'Coder is reserved for slot 3')):
        proc = spawn(*args)
        assert proc.returncode == 1 and message.encode() in proc.stderr, (args, proc.stderr)
    helper = spawn('--role', 'Helper')
    assert helper.returncode == 0, helper.stderr
    assert json.loads(helper.stdout)['slot'] not in (1, 3), 'others skip the reserved Coder slot'


# ---------------------------------------------------------------- 5. spawn uses the recorded effective crew


def test_spawn_uses_recorded_crew_file_and_role_override(world, sleeper):
    """A --crew-file shift: spawn cannot find 'spawnable' by name, so the brief/colour/model of the new Tester can
    only come from the recorded overrides.crew_file with the recorded --role applied (brief swapped to -cli)."""
    hwnd = real_terminal_hwnd()
    if hwnd is None:
        pytest.skip('spawn dry-run needs an open Windows Terminal window to check (never launched here)')
    pid, started = sleeper
    w = world
    d = Path(w['env']['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-cfilefix'
    crew_file = w['files'] / 'spawnable.yaml'
    write_json(d / 'crew.json', {
        'crew': 'spawnable',
        'overrides': {'crew_file': str(crew_file), 'role': ['Tester=copilot:gemini-3.8-flash:high']},
        'members': [{'role': 'Foreman', 'label': 'Foreman', 'identity': f'{d.name}-foreman', 'brief': 'foreman.md',
                     'persona': None, 'colour': 'red', 'human_contact': True, 'tool': 'claude',
                     'model': 'claude-opus-5-5', 'peer': ''}],
        'slots': 4, 'advisor_slot': None, 'permission_mode': 'auto', 'model': 'claude-opus-5-5',
        'cwd': str(w['plain'])})
    write_json(d / 'launch.json', {'hwnd': hwnd})
    write_json(d / 'Foreman.json', {'pid': pid, 'started': started, 'tool': 'claude', 'cwd': str(w['plain'])})
    for n in range(1, 5):
        write_json(d / 'slots' / f'{n}.json', {'slot': n, 'role': 'Foreman' if n == 1 else None, 'cmd_pid': pid,
                                               'started': 900.0, **({'spawned': 500.0} if n == 1 else {})})
    proc = native(['spawn', 'cfilefix', '--role', 'Tester', '--cli', 'copilot', '--dry-run'], w['env'])
    assert proc.returncode == 0, proc.stderr.decode('utf-8', 'replace')
    member = json.loads(proc.stdout)['member']
    assert member['colour'] == 'purple', 'manifest read through overrides.crew_file'
    assert member['brief'] == 'tester-cli.md', 'claude -> copilot override swaps the brief'
    assert (member['model'], member['reasoning']) == ('gemini-3.8-flash', 'high')

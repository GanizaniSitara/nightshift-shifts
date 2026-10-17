"""NGH-062 site layer, doctor --cwd, start brief fields and protected paths (native only, no oracle).

Contract: C:/nightshift/shifts/<shift>/contract-ngh062.md plus Foreman msgs 73, 77, 79. Every fixture is a private
temp world: the launcher's start world (fake tasks-cli, throwaway repos; start runs --dry-run only), the NGH-060 bus
world for claim, and the lifecycle world for land. The site layer lives in the fixture's own state root.
"""
import json
import os
from pathlib import Path
import re

import pytest

from test_go_bus_commands import bus_world, cli, err, text
from test_go_launcher import fake_bin, make_repo, run as launcher_run, start_world
from test_go_readonly import write_json
from test_go_write_commands import shift


WARNING = re.compile(r'^brief (\S+): (role|repository|site) layer (\d+) words, budget (\d+)$')
DEFAULT_ROLES = ('Foreman', 'Advisor', 'Coder', 'Tester')


def state_root(env):
    return Path(env['NIGHTSHIFT_STATE_ROOT'])


def site(env, yaml=None, prose=None):
    if yaml is not None:
        (state_root(env) / 'site.yaml').write_text(yaml, encoding='utf-8')
    if prose is not None:
        (state_root(env) / 'instructions.md').write_text(prose, encoding='utf-8')


def lines(data):
    return [line for line in data.decode('utf-8').replace('\r\n', '\n').splitlines() if line.strip()]


def one_line(proc, code):
    assert proc.returncode == code, (proc.returncode, proc.stdout, proc.stderr)
    out = lines(proc.stderr)
    assert len(out) == 1, proc.stderr
    return out[0]


def words(n, word):
    return ' '.join([word] * n)


def doctor(w, *extra):
    return launcher_run(['doctor', '--cwd', str(w['configured']), *extra], w['env'], True)


def start_dry(w, *extra):
    return launcher_run(['start', '--dry-run', '--cwd', str(w['repo']), *extra, 'Brief café task'], w['env'], True)


def shifts(env):
    return sorted(p.name for p in (state_root(env) / 'shifts').iterdir())


# ---------------------------------------------------------------- doctor --cwd

def doctor_json(w, *extra):
    """doctor --cwd prints JSON when stdout is not a console (Coder2's doc): repo, crew, roles[{role, mode, layers}],
    warnings."""
    proc = doctor(w, *extra)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return json.loads(proc.stdout)


def layers_of(report, role):
    entry = next(r for r in report['roles'] if r['role'] == role)
    return {l['name']: (l['words'], l['budget']) for l in entry['layers']}, entry['mode']


def test_doctor_cwd_reports_every_role_and_layer_and_exits_0(start_world):
    w = start_world
    (w['configured'] / '.nightshift' / 'instructions.md').write_text(words(200, 'shared'), encoding='utf-8')
    site(w['env'], prose=words(80, 'site'))
    report = doctor_json(w, '--crew', 'default')
    assert report['crew'] == 'default'
    assert {r['role'] for r in report['roles']} >= set(DEFAULT_ROLES), report['roles']
    for role in DEFAULT_ROLES:
        layers, mode = layers_of(report, role)
        assert set(layers) == {'role', 'repository', 'site'} and mode in ('standard', 'compact')
        assert layers['repository'][1] == 150 and layers['site'] == (80, 50)
    warnings = [WARNING.match(x) for x in report['warnings']]
    assert warnings and all(warnings), report['warnings']
    for role in DEFAULT_ROLES:
        names = {m[2] for m in warnings if m[1] == role}
        assert {'repository', 'site'} <= names, (role, names)
    for m in warnings:
        assert int(m[3]) > int(m[4]), m[0]   # a warning only when over budget
        assert int(m[4]) in (250, 150, 50), m[0]


def test_doctor_cwd_word_counts_follow_the_files(start_world):
    w = start_world
    nd = w['configured'] / '.nightshift'
    (nd / 'instructions.md').write_text(words(40, 'shared'), encoding='utf-8')
    (nd / 'coder.md').write_text(words(30, 'coderonly'), encoding='utf-8')
    (nd / 'foreman.md').unlink()
    site(w['env'], prose=words(7, 'site'))
    report = doctor_json(w, '--crew', 'default')
    coder, _ = layers_of(report, 'Coder')
    foreman, _ = layers_of(report, 'Foreman')
    assert coder['repository'][0] == 70 and coder['site'][0] == 7, coder
    assert foreman['repository'][0] == 40, foreman
    assert not [x for x in report['warnings'] if 'repository layer' in x or 'site layer' in x]


def test_doctor_crew_without_cwd_is_usage_error(start_world):
    proc = launcher_run(['doctor', '--crew', 'default'], start_world['env'], True)
    assert proc.returncode == 2 and b'requires --cwd' in proc.stderr, proc.stderr


def test_plain_doctor_ignores_brief_rendering(start_world):
    w = start_world
    (w['configured'] / '.nightshift' / 'instructions.md').write_text(words(500, 'shared'), encoding='utf-8')
    proc = launcher_run(['doctor'], w['env'], True)
    assert b'repository layer' not in proc.stdout + proc.stderr


# ---------------------------------------------------------------- site allow-lists

@pytest.mark.parametrize('yaml', ['allowed_tools: [codex]\n',
                                  'allowed_models: [gpt-6.1-sol]\n',
                                  'allowed_tools: [claude]\nallowed_models: [claude-opus-5-5]\n'])
def test_doctor_and_start_refuse_a_crew_outside_the_site_lists(start_world, yaml):
    w = start_world
    site(w['env'], yaml=yaml)   # the default crew has claude members on claude-opus-5-5 and claude-fable-5-1
    line = one_line(doctor(w, '--crew', 'default'), 4)
    assert 'site.yaml' in line or 'allowed' in line, line
    before = shifts(w['env'])
    line = one_line(start_dry(w), 4)
    assert 'allowed' in line or 'site.yaml' in line, line
    assert shifts(w['env']) == before


def test_start_accepts_a_crew_inside_the_site_lists(start_world):
    w = start_world
    # every tool and model the default crew uses (it has an agy Apprentice); models as path.Match globs
    site(w['env'], yaml='allowed_tools: [claude, codex, agy, copilot]\nallowed_models: ["claude-*", "gemini-*", "gpt-*"]\n'
                        'env:\n  GOPROXY: "off"\nweb_search: false\n')
    proc = start_dry(w)
    assert proc.returncode == 0, proc.stderr
    assert doctor(w, '--crew', 'default').returncode == 0


def test_invalid_site_yaml_is_one_line_error(start_world):
    w = start_world
    site(w['env'], yaml='allowed_tools: claude\n')   # not a list
    proc = start_dry(w)
    assert proc.returncode != 0 and len(lines(proc.stderr)) == 1, proc.stderr


# ---------------------------------------------------------------- start --dry-run brief fields

def test_start_dry_run_plan_has_brief_mode_layers_and_warnings(start_world):
    w = start_world
    site(w['env'], prose=words(60, 'site'))
    proc = start_dry(w)
    assert proc.returncode == 0, proc.stderr
    plan = json.loads(proc.stdout)
    roles = plan['roles']
    assert set(DEFAULT_ROLES) <= set(roles), list(roles)
    for label in DEFAULT_ROLES:
        r = roles[label]
        assert r['brief_mode'] in ('standard', 'compact'), r
        assert set(r['brief_layers']) == {'role', 'repository', 'site'}, r['brief_layers']
        assert all(isinstance(v, int) for v in r['brief_layers'].values())
        assert r['brief_layers']['site'] == 60
        assert isinstance(r['brief_warnings'], list)
        assert any(WARNING.match(x) and 'site layer' in x for x in r['brief_warnings']), r['brief_warnings']


def test_start_dry_run_brief_mode_follows_crew_yaml(start_world):
    w = start_world
    crews = w['configured'] / '.nightshift' / 'crews'
    (crews / 'modes.yaml').write_text(
        'name: modes\nlayout: 1x4\nroles:\n'
        '  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
        '  - {role: Coder, brief: coder.md, brief_mode: compact}\n'
        '  - {role: Tester, brief: tester.md, tool: agy, model: gemini-3.8-flash-high}\n', encoding='utf-8')
    proc = launcher_run(['start', '--dry-run', '--cwd', str(w['configured']), '--crew', 'modes', 'x'], w['env'], True)
    assert proc.returncode == 0, proc.stderr
    roles = json.loads(proc.stdout)['roles']
    assert roles['Coder']['brief_mode'] == 'compact'
    assert roles['Tester']['brief_mode'] == 'compact'
    assert roles['Foreman']['brief_mode'] == 'standard'


# ---------------------------------------------------------------- protected_paths

@pytest.fixture
def protected_world(tmp_path):
    env = bus_world(tmp_path / 'protected world')
    repo = make_repo(tmp_path / 'protected repo')
    (repo / '.nightshift').mkdir()
    (repo / '.nightshift' / 'config.yaml').write_text(
        'build:\n  cmd: "exit /b 0"\nprotected_paths:\n  - "secrets/*"\n  - "docs/ARCHITECTURE.md"\n', encoding='utf-8')
    d = shift(env)
    crew = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
    write_json(d / 'crew.json', {**crew, 'cwd': str(repo)})
    write_json(d / 'worktree.json', {'repo_root': str(repo), 'worktree_dir': str(repo), 'branch': 'main',
                                     'target_branch': 'main'})
    return env, repo


def claims(env):
    p = shift(env) / 'claims.json'
    return json.loads(p.read_bytes()) if p.exists() else {}


@pytest.mark.parametrize('relative', ['secrets/key.txt', 'docs/ARCHITECTURE.md', 'SECRETS/deep/key.txt'])
def test_claim_refuses_protected_path(protected_world, relative):
    env, repo = protected_world
    target = repo / relative
    line = one_line(cli(env, 'claim', 'alpha', 'Senior', str(target)), 4)
    assert line.endswith('is protected by .nightshift/config.yaml; ask the human'), line
    assert Path(relative).name.lower() in line.lower()
    assert claims(env) == {}


def test_claim_with_any_protected_path_claims_nothing(protected_world):
    env, repo = protected_world
    proc = cli(env, 'claim', 'alpha', 'Senior', str(repo / 'src' / 'ok.py'), str(repo / 'secrets' / 'x'))
    one_line(proc, 4)
    assert claims(env) == {}


def test_claim_unprotected_path_still_works(protected_world):
    env, repo = protected_world
    proc = cli(env, 'claim', 'alpha', 'Senior', str(repo / 'src' / 'ok.py'), str(repo / 'docs' / 'other.md'))
    assert proc.returncode == 0, err(proc)
    assert len(claims(env)) == 2


# ---------------------------------------------------------------- build and land accept the new config keys

NEW_KEYS = ('build:\n  cmd: "exit /b 0"\ntest_one: "pytest -k NAME"\nlint: "ruff check ."\n'
            'typecheck: "mypy ."\nprotected_paths:\n  - "secrets/*"\n')


def test_build_accepts_new_config_keys(tmp_path):
    env = bus_world(tmp_path / 'keys world')
    repo = make_repo(tmp_path / 'keys repo')
    (repo / '.nightshift').mkdir()
    (repo / '.nightshift' / 'config.yaml').write_text(NEW_KEYS, encoding='utf-8')
    proc = cli(env, 'build', 'alpha', '--cwd', str(repo), timeout=120)
    assert proc.returncode == 0, (text(proc), err(proc))
    assert json.loads(proc.stdout)['cmd'] == 'exit /b 0'


def test_land_accepts_new_config_keys(tmp_path):
    from test_go_lifecycle import cli as lcli, commit as lcommit, evidence, world as lworld
    env, d, repo, work, info = lworld(tmp_path / 'land keys')
    (repo / '.nightshift').mkdir()
    (repo / '.nightshift' / 'config.yaml').write_text(NEW_KEYS, encoding='utf-8')
    lcommit(work, 'feature.txt', 'crew change\n')
    evidence(d, work)
    proc = lcli(['land', 'fixture'], env, True)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert json.loads(proc.stdout)['status'] == 'landed'


@pytest.mark.parametrize('bad', ['test_one: [a]\n', 'protected_paths: "secrets/*"\n', 'protected_paths: [1]\n'])
def test_bad_new_config_values_are_rejected(tmp_path, bad):
    env = bus_world(tmp_path / 'bad keys world')
    repo = make_repo(tmp_path / 'bad keys repo')
    (repo / '.nightshift').mkdir()
    (repo / '.nightshift' / 'config.yaml').write_text('build:\n  cmd: "exit /b 0"\n' + bad, encoding='utf-8')
    proc = cli(env, 'build', 'alpha', '--cwd', str(repo), timeout=120)
    assert proc.returncode != 0 and b'config' in proc.stderr, (proc.stdout, proc.stderr)

"""NGH-067 Codex pane identity through `start --dry-run` (native only; nothing launched).

The Windows Terminal colour scheme per role is the one identity signal that works for every tool; each started Codex
pane must carry its role's scheme, and its Codex command line the cost and credits status-line items the in-shift
patrol reads (NGH-066). Reserved roles (Advisor, the Coder of an Apprentice crew) start later and have no segment.
"""
import json
import re

import pytest
import yaml

from test_go_launcher import REPO, fake_bin, run, sleeper, start_world


CODEX_CREWS = sorted(p.stem for p in (REPO / 'crews').glob('*.yaml')
                     if any(r.get('tool') == 'codex'
                            for r in yaml.safe_load(p.read_text(encoding='utf-8'))['roles']))


def plan_for(w, crew):
    proc = run(['start', '--dry-run', '--cwd', str(w['plain']), '--crew', crew, 'Codex identity'], w['env'], True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def pane_segments(plan):
    """{pane label: its wt_argv segment} for every pane the window starts."""
    segments, current = {}, []
    for token in plan['wt_argv'] + [';']:
        if token != ';':
            current.append(token)
            continue
        if '--pane' in current:
            segments[current[current.index('--pane') + 1]] = current
        current = []
    return segments


def crew_colours(crew):
    return {r['role']: r.get('colour') for r in yaml.safe_load((REPO / 'crews' / f'{crew}.yaml')
                                                              .read_text(encoding='utf-8'))['roles']}


def test_there_are_codex_crews():
    assert 'codex' in CODEX_CREWS, CODEX_CREWS


@pytest.mark.parametrize('crew', CODEX_CREWS)
def test_every_started_codex_pane_has_its_role_colour_scheme(start_world, crew):
    plan = plan_for(start_world, crew)
    colours = crew_colours(crew)
    segments = pane_segments(plan)
    codex_started = [label for label, r in plan['roles'].items() if r.get('tool') == 'codex' and label in segments]
    assert codex_started, f'{crew}: no Codex pane starts with the window'
    for label in codex_started:
        seg = segments[label]
        assert '--colorScheme' in seg, f'{crew} {label}: no colour scheme in {seg}'
        scheme = seg[seg.index('--colorScheme') + 1]
        role = re.sub(r'-\d+$', '', label)
        assert scheme == f'Nightshift {colours[role]}', (crew, label, scheme)


@pytest.mark.parametrize('crew', CODEX_CREWS)
def test_codex_command_status_line_has_cost_and_credits(start_world, crew):
    plan = plan_for(start_world, crew)
    for label, r in plan['roles'].items():
        if r.get('tool') != 'codex':
            continue
        argv = r['command']
        status = next(argv[i + 1] for i in range(len(argv) - 1)
                      if argv[i] == '-c' and argv[i + 1].startswith('tui.status_line='))
        items = json.loads(status.split('=', 1)[1])
        for want in ('estimated-thread-cost', 'thread-credits', 'model-with-reasoning', 'context-remaining'):
            assert want in items, (crew, label, items)


def test_codex_panes_have_distinct_schemes_per_role(start_world):
    plan = plan_for(start_world, 'codex')
    segments = pane_segments(plan)
    schemes = {label: seg[seg.index('--colorScheme') + 1] for label, seg in segments.items() if '--colorScheme' in seg}
    assert len(set(schemes.values())) == len(schemes), schemes


def slot_segments(plan):
    """{slot number: wt_argv segment} for every segment that names a slot (pane or plain reserved shell)."""
    segments, current = {}, []
    for token in plan['wt_argv'] + [';']:
        if token != ';':
            current.append(token)
            continue
        if '--slot' in current:
            segments[int(current[current.index('--slot') + 1])] = current
        current = []
    return segments


def test_codex_reserved_slots_open_with_their_role_scheme(start_world):
    """c14 (Foreman msg 186): Codex resets the background (OSC 111), so only the wt scheme shows; the reserved
    Advisor (slot 2) and Coder (slot 3) slots of the codex crew now open with their role's scheme too."""
    plan = plan_for(start_world, 'codex')
    slots = slot_segments(plan)
    for slot, scheme in ((2, 'Nightshift purple'), (3, 'Nightshift green')):
        seg = slots[slot]
        assert '--pane' not in seg, f'slot {slot} is reserved: a plain shell, not a started pane'
        assert '--colorScheme' in seg and seg[seg.index('--colorScheme') + 1] == scheme, (slot, seg)


def test_codex_pane_start_sends_no_rename(tmp_path, fake_bin, sleeper):
    """c14/c15 (Foreman msg 186): the /rename injection into Codex panes is gone (it was typed as a message)."""
    from test_go_launcher import pane_world
    env, d, repo, record = pane_world(tmp_path / 'codex pane', fake_bin, sleeper)
    proc = run(['--pane', 'Coder', '--state-dir', str(d), '--start', '--cwd', str(repo), '--slot', '3'], env, True,
               timeout=120)
    assert proc.returncode == 0, proc.stderr
    calls = [json.loads(line) for line in (record / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    assert any(c['exe'] == 'codex' for c in calls), 'the stub codex must have been launched'
    renames = [c for c in calls if c['exe'] == 'agents-cli' and any('/rename' in a for a in c['argv'])]
    assert renames == [], renames

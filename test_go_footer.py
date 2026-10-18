"""NGH-070 short footer and legacy shift compatibility, using disposable state only."""
import json
from pathlib import Path
import re

import pytest

from test_go_launcher import INVALID_HWND, base_env, run, state_snapshot, write_json


def footer_world(base, name):
    env = base_env(base, None)
    env['NIGHTSHIFT_NUDGE'] = 'off'
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / name
    d.mkdir()
    write_json(d / 'crew.json', {'members': [{'role': 'Foreman', 'label': 'Foreman', 'human_contact': True,
                                             'identity': name + '-foreman', 'tool': 'claude'}]})
    write_json(d / 'Foreman.json', {'pid': 2147483000, 'started': 1, 'tool': 'claude',
                                   'session_id': 'fixture-session'})
    write_json(d / 'launch.json', {'hwnd': INVALID_HWND})
    (d / 'handover.md').write_text('Legacy carry-over sentinel.\n', encoding='utf-8')
    (d / 'prompt.txt').write_text('Fixture task\n', encoding='utf-8')
    return env, d


@pytest.mark.parametrize('name', ['shift-abc12345', 'Nightshift-shift-abc12345'])
def test_claude_footer_shortens_name_and_preserves_cost_and_context_callbacks(tmp_path, name):
    env, d = footer_world(tmp_path / 'footer', name)
    payload = {'session_id': 'fixture-session', 'model': {'display_name': 'Opus 5.5'},
               'context_window': {'used_percentage': 59, 'remaining_percentage': 41},
               'cost': {'total_cost_usd': 12.3}, 'cwd': 'C:/never-display-this'}
    for _ in range(2):
        result = run(['statusline', '--state-dir', str(d), '--role', 'Foreman',
                      '--identity', name + '-foreman'], env, True, stdin=json.dumps(payload).encode())
        assert result.returncode == 0, result.stderr
        assert result.stdout.decode().strip() == 'shift-abc12345 | Opus 5.5 | ctx 41% left | $12.30'
    usage = json.loads((d / 'Foreman-usage.json').read_text(encoding='utf-8'))
    assert usage['latest']['unit'] == 'usd' and usage['latest']['value'] == 12.3
    assert len(usage['samples']) == 1, 'same cost callback should not duplicate budget samples'
    context = json.loads((d / 'Foreman-context.json').read_text(encoding='utf-8'))
    assert context['context_used'] == 59 and context['pid'] == 2147483000 and context['started'] == 1


@pytest.mark.parametrize('name', ['shift-abc12345', 'Nightshift-shift-abc12345'])
def test_old_and_short_shift_status_queue_continue_and_down(tmp_path, name):
    env, d = footer_world(tmp_path / 'legacy', name)
    status = run(['status', '--all'], env, True)
    assert status.returncode == 0, status.stderr
    assert [row['shift'] for row in json.loads(status.stdout)] == [name]
    added = run(['add', '--shift', name, 'queued compatibility task'], env, True)
    assert added.returncode == 0, (added.stdout, added.stderr)
    queued = run(['queue', '--shift', name], env, True)
    assert queued.returncode == 0 and b'queued compatibility task' in queued.stdout, (queued.stdout, queued.stderr)
    plain = tmp_path / 'plain'
    plain.mkdir()
    before = state_snapshot(env)
    carried = run(['start', '--dry-run', '--cwd', str(plain), '--continue-from', str(d), 'Carry on'], env, True)
    assert carried.returncode == 0, carried.stderr
    assert 'Legacy carry-over sentinel.' in json.loads(carried.stdout)['prompt']
    assert state_snapshot(env) == before, '--continue-from dry run must not rename/write legacy source'
    down = run(['down', name, '--hold'], env, True)
    assert down.returncode == 0, (down.stdout, down.stderr)
    assert d.exists(), '--hold retains original shift directory and its exact legacy/short name'


def test_new_plan_uses_short_names_for_state_sessions_and_titles(tmp_path):
    env = base_env(tmp_path / 'new plan', None)
    plain = tmp_path / 'plain'
    plain.mkdir()
    result = run(['start', '--dry-run', '--cwd', str(plain), 'A short-name task'], env, True)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert re.fullmatch(r'shift-[0-9a-f]{8}', plan['shift']), plan['shift']
    assert Path(plan['state_dir']).name == plan['shift']
    for role in plan['roles'].values():
        assert role['session_name'].startswith(plan['shift'] + '-')
        assert role['pane_title'] == plan['shift']
    args = plan['wt_argv']
    titles = [args[i + 1] for i, value in enumerate(args[:-1]) if value == '--title']
    assert titles and all(title == plan['shift'] for title in titles), titles
    assert 'Nightshift-' + plan['shift'] not in json.dumps(plan), 'generated identities must use only the short prefix'

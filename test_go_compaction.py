"""NGH-069 compaction tests: disposable state and _attach helper, never a real pane."""
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from test_go_launcher import base_env, exe_path, fake_bin, go_toolchain, real_terminal_hwnd, run, sleeper, write_json
from test_go_nudge_retry import SHIFT, World

STUB_AGENTS_CLI = r'''package main

import (
	"encoding/json"
	"io"
	"os"
	"strings"
	"time"
)

func record(rec map[string]any) {
	path := os.Getenv("CONSOLE_RECORD")
	if path == "" {
		return
	}
	snapshot, _ := os.ReadFile(os.Getenv("COMPACTION_STATE"))
	rec["durable_state"] = string(snapshot)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		return
	}
	defer f.Close()
	b, _ := json.Marshal(rec)
	_, _ = f.Write(append(b, '\n'))
}

func main() {
	args := os.Args[1:]
	if len(args) >= 2 && args[0] == "tail" {
		screen := os.Getenv("STUB_SCREEN")
		lines := []string{}
		if screen != "" {
			lines = strings.Split(screen, "\n")
		}
		state := "idle"
		if strings.Contains(screen, "Working (") {
			state = "busy"
		} else if strings.Contains(screen, "Do you trust") {
			state = "prompt"
		}
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"lines": lines, "state": state})
		return
	}
	if len(args) >= 3 && args[0] == "send" {
		pid, text := args[1], args[2]
		record(map[string]any{"op": "send", "pid": pid, "text": text})
		if os.Getenv("COMPACTION_BLOCK_READY") != "" {
			_ = os.WriteFile(os.Getenv("COMPACTION_BLOCK_READY"), []byte("ready"), 0600)
			deadline := time.Now().Add(10 * time.Second)
			for time.Now().Before(deadline) {
				if _, err := os.Stat(os.Getenv("COMPACTION_BLOCK_RELEASE")); err == nil {
					break
				}
				time.Sleep(10 * time.Millisecond)
			}
		}
		if os.Getenv("COMPACTION_ERROR") != "" {
			_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"submitted": false})
			return
		}
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"submitted": true, "enter": true, "ok": true})
		return
	}
	if len(args) >= 3 && args[0] == "_attach" {
		op, pid := args[1], args[2]
		in, _ := io.ReadAll(os.Stdin)
		record(map[string]any{"op": op, "pid": pid, "text": string(in)})
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{})
		return
	}
	_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"ok": true})
}
'''


def console_calls(record_path: Path):
    if not record_path.exists():
        return []
    return [json.loads(line) for line in record_path.read_text(encoding='utf-8').splitlines() if line.strip()]


@pytest.fixture(scope='session')
def compaction_helper(tmp_path_factory):
    """Build a stub agents-cli.exe that records durable state before accepting a write."""
    go = go_toolchain()
    if not go:
        pytest.skip('no provisioned Go toolchain')
    root = tmp_path_factory.mktemp('compaction helper')
    (root / 'helper.go').write_text(STUB_AGENTS_CLI, encoding='utf-8')
    exe = root / 'agents-cli.exe'
    built = subprocess.run([go, 'build', '-o', str(exe), str(root / 'helper.go')], cwd=root,
                           capture_output=True, timeout=90,
                           env={**os.environ, 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off',
                                'GO111MODULE': 'off', 'GOFLAGS': ''},
                           creationflags=subprocess.CREATE_NO_WINDOW)
    assert built.returncode == 0, built.stderr
    return exe


@pytest.fixture
def compact_world(tmp_path, fake_bin, sleeper, compaction_helper):
    w = World(tmp_path / 'compaction world', fake_bin, sleeper, compaction_helper.parent)
    w.record = tmp_path / 'helper record'
    w.env['CONSOLE_RECORD'] = str(w.record)
    w.env['NIGHTSHIFT_NUDGE'] = 'off'
    w.compaction = w.d / 'Coder-compaction.json'
    w.env['COMPACTION_STATE'] = str(w.compaction)
    crew = json.loads((w.d / 'crew.json').read_text(encoding='utf-8'))
    for member in crew['members']:
        member['compact_at'] = 0
        if member['label'] == 'Coder':
            member.update(tool='codex', compact_at=70)
    write_json(w.d / 'crew.json', crew)
    info = json.loads((w.d / 'Coder.json').read_text(encoding='utf-8'))
    write_json(w.d / 'Coder.json', {**info, 'tool': 'codex'})
    return w


def tick(w, *, left=20, screen=None, entry='patrol'):
    if screen is None:
        screen = f'› Ask Codex to do anything\n~$1.23 · Context {left}% left'
    env = {**w.env, 'STUB_SCREEN': screen}
    args = ['patrol', SHIFT, '--once'] if entry == 'patrol' else ['warden', SHIFT]
    result = run(args, env, True, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return result


def compact_writes(w):
    return [c for c in console_calls(w.record) if c.get('text') in ('/compact', '/compress')]


@pytest.mark.parametrize('entry', ['patrol', 'warden'])
def test_compaction_persists_pending_before_single_send(compact_world, entry):
    w = compact_world
    tick(w, entry=entry)
    calls = compact_writes(w)
    assert len(calls) == 1, console_calls(w.record)
    assert calls[0]['pid'] == str(w.pid)
    saved_before_write = json.loads(calls[0]['durable_state'])
    assert saved_before_write.get('phase') == 'pending', saved_before_write
    assert w.events('compacted') == [], 'a send receipt is not proof of compaction'
    tick(w, entry=entry)
    assert len(compact_writes(w)) == 1, 'pending compaction must never automatically reinject'


@pytest.mark.parametrize('case', ['below', 'disabled', 'busy', 'draft', 'ask-codex-draft', 'old-idle-with-draft', 'permission', 'assignment', 'no-context'])
def test_compaction_suppresses_unsafe_or_unnecessary_send(compact_world, case):
    w = compact_world
    screen = None
    left = 20
    if case == 'below':
        left = 31
    if case == 'disabled':
        crew = json.loads((w.d / 'crew.json').read_text(encoding='utf-8'))
        for member in crew['members']:
            member['compact_at'] = 0
        write_json(w.d / 'crew.json', crew)
    if case == 'busy':
        screen = 'Working (2s • esc to interrupt)\n› Ask Codex to do anything\nContext 20% left'
    if case == 'draft':
        screen = '› preserve this unsent draft\nContext 20% left'
    if case == 'old-idle-with-draft':
        screen = '› Ask Codex to do anything\nold output\n› preserve this unsent draft\nContext 20% left'
    if case == 'ask-codex-draft':
        screen = '› Ask Codex to refactor this file\nContext 20% left'
    if case == 'permission':
        screen = 'Do you trust this folder?\n› Yes, I trust this folder\nContext 20% left'
    if case == 'assignment':
        w.insert(recipient='Coder', state='acknowledged', age=10)
    if case == 'no-context':
        screen = '› Ask Codex to do anything\n~$80.00 · 99 credits'
    tick(w, left=left, screen=screen)
    assert compact_writes(w) == [], console_calls(w.record)


def test_compaction_requires_observed_decrease_and_emits_once(compact_world):
    w = compact_world
    tick(w, left=20)
    tick(w, left=20)
    assert w.events('compacted') == []
    tick(w, left=80)
    events = w.events('compacted')
    assert len(events) == 1, w.events()
    assert events[0]['role'] == 'Coder'
    assert events[0]['before'] == 80 and events[0]['after'] == 20, events[0]
    state = json.loads(w.compaction.read_text(encoding='utf-8'))
    assert state.get('last_compacted'), state
    tick(w, left=80)
    assert len(w.events('compacted')) == 1
    assert len(compact_writes(w)) == 1


def set_tool(w, tool):
    crew = json.loads((w.d / 'crew.json').read_text(encoding='utf-8'))
    for member in crew['members']:
        if member['label'] == 'Coder':
            member['tool'] = tool
    write_json(w.d / 'crew.json', crew)
    info = json.loads((w.d / 'Coder.json').read_text(encoding='utf-8'))
    write_json(w.d / 'Coder.json', {**info, 'tool': tool})
    return info


@pytest.mark.parametrize('status', ['waiting', 'running'])
def test_compaction_waits_for_build_or_gate(compact_world, status):
    w = compact_world
    write_json(w.d / 'builds' / '1.json', {'n': 1, 'status': status, 'pid': os.getpid(), 'asg': 'gate-a'})
    tick(w)
    assert compact_writes(w) == [], console_calls(w.record)


@pytest.mark.parametrize('case', ['fresh', 'stale', 'old-incarnation', 'future'])
def test_claude_context_callback_signal_is_fresh_and_incarnation_fenced(compact_world, case):
    w = compact_world
    info = set_tool(w, 'claude')
    at = time.time() - (91 if case == 'stale' else 0)
    if case == 'future':
        at += 3600
    write_json(w.d / 'Coder-context.json', {'pid': w.pid, 'started': info['started'] - (1 if case == 'old-incarnation' else 0),
                                          'context_used': 80, 'context_at': at})
    tick(w, screen='❯')
    assert len(compact_writes(w)) == (1 if case == 'fresh' else 0), console_calls(w.record)


def test_compaction_at_exact_threshold(compact_world):
    tick(compact_world, left=30)
    assert len(compact_writes(compact_world)) == 1


def test_pending_compaction_survives_process_restart_without_reinjection(compact_world):
    w = compact_world
    info = json.loads((w.d / 'Coder.json').read_text(encoding='utf-8'))
    write_json(w.compaction, {'pid': w.pid, 'started': info['started'], 'phase': 'pending',
                             'before': 80, 'requested_at': time.time() - 1})
    tick(w, left=20)
    tick(w, left=20)
    assert compact_writes(w) == []
    assert w.events('compacted') == []


def test_pending_old_incarnation_does_not_block_new_member(compact_world):
    w = compact_world
    info = json.loads((w.d / 'Coder.json').read_text(encoding='utf-8'))
    write_json(w.compaction, {'pid': w.pid, 'started': info['started'] - 1, 'phase': 'pending',
                             'before': 80, 'requested_at': time.time() - 1})
    tick(w)
    assert len(compact_writes(w)) == 1


def anchor_writes(w):
    return [c for c in console_calls(w.record) if c.get('text', '').startswith('You are Coder in ')]


@pytest.mark.parametrize('uncertain', [False, True])
def test_reanchor_persists_intent_and_never_reinjects(compact_world, uncertain):
    w = compact_world
    tick(w, left=20)
    if uncertain:
        w.env['COMPACTION_ERROR'] = 'uncertain send'
    tick(w, left=80)
    notes = anchor_writes(w)
    assert len(notes) == 1, console_calls(w.record)
    assert str(w.d / 'Coder.brief.md') in notes[0]['text']
    assert 'reread it, then run nightshift wait' in notes[0]['text']
    assert json.loads(notes[0]['durable_state'])['phase'] == 'anchor_pending'
    state = json.loads(w.compaction.read_text(encoding='utf-8'))
    assert state['phase'] == ('anchor_pending' if uncertain else 'waiting'), state
    tick(w, left=80)
    assert len(anchor_writes(w)) == 1, 'uncertain or waiting reanchor must never be automatically repeated'


@pytest.mark.parametrize('waiter', ['fresh', 'old', 'dead', 'missing', 'late'])
def test_reanchor_requires_new_live_waiter_or_raises_stuck_once(compact_world, waiter):
    w = compact_world
    tick(w, left=20)
    tick(w, left=80)
    state = json.loads(w.compaction.read_text(encoding='utf-8'))
    state['anchor_at'] = time.time() - 61
    write_json(w.compaction, state)
    if waiter != 'missing':
        offset = 61 if waiter == 'late' else (1 if waiter == 'fresh' else -1)
        write_json(w.d / 'waiting' / 'Coder.json', {'pid': 2147483000 if waiter == 'dead' else os.getpid(),
                                                   'since': state['anchor_at'] + offset})
    tick(w, left=80)
    saved = json.loads(w.compaction.read_text(encoding='utf-8'))
    assert saved['phase'] == ('done' if waiter == 'fresh' else 'stuck'), saved
    expected = 0 if waiter == 'fresh' else 1
    assert len(w.events('member-stuck')) == expected, w.events()
    tick(w, left=80)
    assert len(w.events('member-stuck')) == expected, 'stuck event must be emitted once'
    assert len(anchor_writes(w)) == 1


def test_compaction_never_interrupts_live_waiter(compact_world):
    w = compact_world
    write_json(w.d / 'waiting' / 'Coder.json', {'pid': os.getpid(), 'since': time.time()})
    tick(w)
    assert compact_writes(w) == []


@pytest.mark.parametrize('tool', ['claude', 'copilot'])
@pytest.mark.parametrize('matching', [False, True])
def test_statusline_callback_routes_context_only_for_matching_session(compact_world, tool, matching):
    w = compact_world
    info = set_tool(w, tool)
    write_json(w.d / 'Coder.json', {**info, 'tool': tool, 'session_id': 'current-session'})
    env = {**w.env, 'NIGHTSHIFT_STATE_DIR': str(w.d), 'NIGHTSHIFT_ROLE': 'Coder'}
    args = ['statusline']
    if tool == 'claude':
        args += ['--state-dir', str(w.d), '--role', 'Coder']
    payload = {'session_id': 'current-session' if matching else 'previous-session',
               'context_window': {'used_percentage': 80}, 'cost': {'total_cost_usd': 1.23}}
    result = run(args, env, True, stdin=json.dumps(payload).encode())
    assert result.returncode == 0, result.stderr
    path = w.d / 'Coder-context.json'
    assert path.exists() == matching
    if matching:
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['pid'] == w.pid and saved['started'] == info['started'] and saved['context_used'] == 80


def test_two_patrols_cannot_repeat_pending_compaction(compact_world, tmp_path):
    w = compact_world
    ready, release = tmp_path / 'helper ready', tmp_path / 'helper release'
    env = {**w.env, 'STUB_SCREEN': '› Ask Codex to do anything\nContext 20% left',
           'COMPACTION_BLOCK_READY': str(ready), 'COMPACTION_BLOCK_RELEASE': str(release)}
    first = subprocess.Popen([str(exe_path()), 'patrol', SHIFT, '--once'], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    second = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline and first.poll() is None:
            time.sleep(0.01)
        assert ready.exists(), 'first patrol must reach the blocked helper'
        # A stale file must not let a second live owner steal the named mutex.
        lock = w.d / 'compaction.lock'
        lock.write_text('{}', encoding='utf-8')
        os.utime(lock, (time.time() - 60, time.time() - 60))
        second = subprocess.Popen([str(exe_path()), 'warden', SHIFT], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.2)
        release.touch()
        out, err = first.communicate(timeout=30)
        assert first.returncode == 0, (out, err)
        out, err = second.communicate(timeout=30)
        assert second.returncode == 0, (out, err)
        assert len(compact_writes(w)) == 1, console_calls(w.record)
    finally:
        release.touch()
        for child in (first, second):
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate(timeout=10)


@pytest.mark.parametrize('scope', ['crew', 'role'])
@pytest.mark.parametrize('value', ['-1', '101', '1.5', 'true', 'null'])
def test_compact_at_rejects_invalid_percentages(tmp_path, scope, value):
    env = base_env(tmp_path / 'config', None)
    plain = tmp_path / 'plain'
    plain.mkdir()
    crew_file = tmp_path / 'invalid.yaml'
    role_extra = f', compact_at: {value}' if scope == 'role' else ''
    crew_extra = f'compact_at: {value}\n' if scope == 'crew' else ''
    crew_file.write_text('name: fixture\nlayout: 1x4\n' + crew_extra +
                         'roles:\n  - {role: Foreman, brief: foreman.md, human_contact: true}\n' +
                         '  - {role: Coder, brief: coder.md' + role_extra + '}\n', encoding='utf-8')
    result = run(['start', '--dry-run', '--cwd', str(plain), '--crew-file', str(crew_file), 'fixture task'], env, True)
    assert result.returncode != 0 and b'compact_at' in result.stderr, (result.stdout, result.stderr)


def test_compact_at_inherits_crew_and_role_zero_disables(tmp_path):
    env = base_env(tmp_path / 'config', None)
    plain = tmp_path / 'plain'
    plain.mkdir()
    crew_file = tmp_path / 'threshold.yaml'
    crew_file.write_text('name: fixture\nlayout: 1x4\ncompact_at: 55\nroles:\n'
                         '  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                         '  - {role: Coder, brief: coder.md, compact_at: 0}\n', encoding='utf-8')
    result = run(['start', '--dry-run', '--cwd', str(plain), '--crew-file', str(crew_file), 'fixture task'], env, True)
    assert result.returncode == 0, result.stderr
    roles = json.loads(result.stdout)['roles']
    assert roles['Foreman']['compact_at'] == 55
    assert roles['Coder']['compact_at'] == 0


@pytest.mark.parametrize('screen', ['› Ask Codex to do anything\n~$1.23',
                                   '› Ask Codex to do anything\n-5% context left'])
def test_codex_requires_current_valid_footer_despite_fresh_saved_context(compact_world, screen):
    w = compact_world
    info = json.loads((w.d / 'Coder.json').read_text(encoding='utf-8'))
    write_json(w.d / 'Coder-context.json', {'pid': w.pid, 'started': info['started'],
                                          'context_used': 80, 'context_at': time.time()})
    tick(w, screen=screen)
    assert compact_writes(w) == []


@pytest.mark.parametrize('mode,threshold', [('tool-switch-zero', 0), ('tool-switch-override', 42),
                                           ('manifest-inherited', 33), ('respawn', 42)])
def test_spawn_and_respawn_preserve_compaction_setting(compact_world, tmp_path, mode, threshold):
    """Read an existing Terminal HWND; dry-run or stub helper only, never opens a pane."""
    hwnd = real_terminal_hwnd()
    if hwnd is None:
        pytest.skip('requires an existing Terminal HWND; never launched here')
    w = compact_world
    crew = json.loads((w.d / 'crew.json').read_text(encoding='utf-8'))
    manifest = tmp_path / 'recorded crew.yaml'
    manifest.write_text('name: fixture\nlayout: 1x4\ncompact_at: 33\nroles:\n'
                         '  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
                         '  - {role: Coder, brief: coder.md}\n', encoding='utf-8')
    crew.update(slots=4, cwd=str(tmp_path), overrides={'crew_file': str(manifest)})
    if mode == 'manifest-inherited':
        crew['members'] = [m for m in crew['members'] if m['label'] != 'Coder']
    else:
        for member in crew['members']:
            if member['label'] == 'Coder':
                member.update(tool='claude', compact_at=threshold)
    write_json(w.d / 'crew.json', crew)
    write_json(w.d / 'launch.json', {'hwnd': hwnd})
    write_json(w.d / 'Coder.json', {'pid': 2147483000, 'started': 1000.5, 'tool': 'claude'})
    for slot in range(1, 5):
        write_json(w.d / 'slots' / f'{slot}.json', {'slot': slot, 'role': 'Foreman' if slot == 1 else None,
                                                  'cmd_pid': w.pid, 'started': 900.0})
    if mode == 'respawn':
        write_json(w.d / 'slots' / '3.json', {'slot': 3, 'role': 'Coder', 'cmd_pid': w.pid, 'started': 900.0,
                                            'spawned': 950.0})
        result = run(['respawn', SHIFT, 'Coder'], w.env, True)
        assert result.returncode == 0, (result.stdout, result.stderr)
        member = next(m for m in json.loads((w.d / 'crew.json').read_text(encoding='utf-8'))['members']
                      if m['label'] == 'Coder')
        assert any(c['op'] == 'inject' for c in console_calls(w.record)), 'respawn must use only our helper'
    else:
        result = run(['spawn', SHIFT, '--role', 'Coder', '--cli', 'codex', '--slot', '3', '--dry-run'], w.env, True)
        assert result.returncode == 0, (result.stdout, result.stderr)
        member = json.loads(result.stdout)['member']
        assert member['tool'] == 'codex'
    assert member['compact_at'] == threshold, member

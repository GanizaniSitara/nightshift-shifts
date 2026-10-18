"""NGH-068 console package: nightshift drives panes itself; agents-cli is no longer needed (native only).

The only test seam is NIGHTSHIFT_CONSOLE_HELPER: nightshift re-execs that binary as `<helper> _attach <op> <pid> ...`
(text on stdin, one JSON line out, the agents-cli helper protocol). The stub helper below speaks that protocol: `peek`
returns a scripted screen (STUB_SCREEN), `inject`, `key` and `copilot-paste` record what they were given in
STUB_RECORD_DIR/console.jsonl and touch nothing. Other test modules import `console_helper` and `console_env`.

Acceptance (contract-ngh068.md, Tester): (a) with no agents-cli on PATH, a nudge, `patrol --once` and usage sampling
work through the helper; (b) a real hidden console, OPT-IN ONLY (NIGHTSHIFT_REAL_CONSOLE_TEST=1; the owner's rule
forbids opening windows, Foreman msg 202): `_attach peek` reads it and `_attach inject` types into it.
"""
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from test_go_launcher import exe_path, fake_bin, go_toolchain, run, sleeper
from test_go_nudge_retry import SHIFT, TEN_MIN, World

STUB_HELPER = r'''package main

import (
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// A stand-in for `nightshift _attach`: records every op, returns the scripted screen for peek, types nothing.
func main() {
	if len(os.Args) < 4 || os.Args[1] != "_attach" {
		os.Stderr.WriteString("stub helper: expected _attach <op> <pid> ...\n")
		os.Exit(2)
	}
	op, pid, rest := os.Args[2], os.Args[3], os.Args[4:]
	rec := map[string]any{"op": op, "pid": pid, "args": rest}
	if op == "inject" || op == "copilot-paste" {
		text, _ := io.ReadAll(os.Stdin)
		rec["text"] = string(text)
	}
	if dir := os.Getenv("STUB_RECORD_DIR"); dir != "" {
		line, _ := json.Marshal(rec)
		if f, err := os.OpenFile(filepath.Join(dir, "console.jsonl"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644); err == nil {
			f.Write(append(line, '\n'))
			f.Close()
		}
	}
	var out any = map[string]string{}
	if op == "peek" {
		screen := os.Getenv("STUB_SCREEN")
		lines := []string{}
		if screen != "" {
			lines = strings.Split(screen, "\n")
		}
		out = map[string]any{"title": os.Getenv("STUB_TITLE"), "lines": lines, "width": 120}
	}
	encoded, _ := json.Marshal(out)
	os.Stdout.Write(append(encoded, '\n'))
}
'''


@pytest.fixture(scope='session')
def console_helper(tmp_path_factory):
    go = go_toolchain()
    if not go:
        pytest.skip('no Go toolchain to build the stub console helper')
    base = tmp_path_factory.mktemp('console helper')
    (base / 'helper.go').write_text(STUB_HELPER, encoding='utf-8')
    exe = base / 'console helper.exe'
    built = subprocess.run([go, 'build', '-o', str(exe), str(base / 'helper.go')], cwd=base, capture_output=True,
                           timeout=300, env={**os.environ, 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off', 'GOFLAGS': '',
                                             'GO111MODULE': 'off'})
    assert built.returncode == 0, built.stderr
    return exe


def without_agents_cli(path):
    """PATH with every folder holding an agents-cli executable removed."""
    keep = [p for p in path.split(os.pathsep)
            if p and not any((Path(p) / name).exists() for name in ('agents-cli.exe', 'agents-cli.cmd', 'agents-cli'))]
    return os.pathsep.join(keep)


def console_env(env, helper, record_dir, screen='❯'):
    record_dir.mkdir(parents=True, exist_ok=True)
    return {**env, 'NIGHTSHIFT_CONSOLE_HELPER': str(helper), 'STUB_RECORD_DIR': str(record_dir), 'STUB_SCREEN': screen,
            'PATH': without_agents_cli(env['PATH'])}


def console_calls(record_dir, op=None):
    p = record_dir / 'console.jsonl'
    calls = [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines()] if p.exists() else []
    return [c for c in calls if op is None or c['op'] == op]


# ---------------------------------------------------------------- acceptance (a): no agents-cli on PATH

@pytest.fixture
def quiet_world(tmp_path, fake_bin, sleeper, console_helper):
    stub_dir = tmp_path / 'no stub'   # World puts a stub folder first on PATH; an empty one here
    stub_dir.mkdir()
    world = World(tmp_path / 'console world', fake_bin, sleeper, stub_dir)
    world.console_record = tmp_path / 'console record'
    world.env = console_env(world.env, console_helper, world.console_record)
    return world


def test_no_agents_cli_on_path():
    env_path = without_agents_cli(os.environ['PATH'])
    probe = subprocess.run(['where', 'agents-cli'], capture_output=True, env={**os.environ, 'PATH': env_path})
    assert probe.returncode != 0, probe.stdout


def test_store_time_nudge_goes_through_the_console_helper(quiet_world):
    w = quiet_world
    assert run(['ready', SHIFT, 'Coder'], w.env, True).returncode == 0
    proc = run(['note', SHIFT, 'Coder', 'heads-up', '--ready-timeout', '5'], w.env, True)
    assert proc.returncode == 0, proc.stderr
    msg_id = int(proc.stdout.split(b'msg:')[1].split()[0])
    injected = [c for c in console_calls(w.console_record, 'inject') if f'msg:{msg_id} ' in c['text']]
    assert injected, console_calls(w.console_record)
    assert injected[0]['pid'] == str(w.pid)
    assert any(e.get('id') == msg_id and e.get('via') == 'nudge' for e in w.events('sent')), w.events()


def test_patrol_once_renudges_through_the_console_helper(quiet_world):
    w = quiet_world
    msg_id = w.insert(age=TEN_MIN + 60)
    assert run(['patrol', SHIFT, '--once'], w.env, True, timeout=120).returncode == 0
    assert [e['attempt'] for e in w.events('nudge-retry')] == [1]
    assert any(f'msg:{msg_id} ' in c['text'] for c in console_calls(w.console_record, 'inject'))


def test_patrol_usage_sampling_reads_the_screen_through_the_helper(quiet_world):
    w = quiet_world
    crew = json.loads((w.d / 'crew.json').read_text(encoding='utf-8'))
    for m in crew['members']:
        if m['label'] == 'Coder':
            m['tool'] = 'copilot'
    (w.d / 'crew.json').write_text(json.dumps(crew), encoding='utf-8')
    info = json.loads((w.d / 'Coder.json').read_text(encoding='utf-8'))
    (w.d / 'Coder.json').write_text(json.dumps({**info, 'tool': 'copilot'}), encoding='utf-8')
    env = {**w.env, 'STUB_SCREEN': 'working...\n Session: 7 AIC used | gemini-3.8-flash'}
    assert run(['patrol', SHIFT, '--once'], env, True, timeout=120).returncode == 0
    usage = json.loads((w.d / 'Coder-usage.json').read_text(encoding='utf-8'))
    assert (usage['latest']['unit'], usage['latest']['value']) == ('credits', 7), usage['latest']
    assert console_calls(w.console_record, 'peek'), 'the screen must be read through the helper'


def test_attach_subcommand_is_hidden_from_top_help():
    proc = run(['--help'], {**os.environ}, True)
    assert b'_attach' not in proc.stdout


def test_attach_rejects_bad_arguments():
    for args in (['_attach'], ['_attach', 'peek'], ['_attach', 'peek', 'notapid'], ['_attach', 'bogus', '123']):
        proc = run(args, {**os.environ}, True, timeout=30)
        assert proc.returncode != 0, (args, proc.stdout, proc.stderr)


# ---------------------------------------------------------------- acceptance (b): a real console, opt-in only

@pytest.mark.skipif(os.environ.get('NIGHTSHIFT_REAL_CONSOLE_TEST') != '1',
                    reason='opens a hidden console window: run only with NIGHTSHIFT_REAL_CONSOLE_TEST=1 (Foreman msg 202)')
def test_real_console_peek_and_inject():
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0   # SW_HIDE
    shell = subprocess.Popen(['cmd.exe', '/k'], creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
    try:
        time.sleep(1.5)
        peek = run(['_attach', 'peek', str(shell.pid), '0'], {**os.environ}, True, timeout=30)
        assert peek.returncode == 0, peek.stderr
        screen = json.loads(peek.stdout)
        assert isinstance(screen['lines'], list) and screen['lines'], screen
        inject = subprocess.run([str(exe_path()), '_attach', 'inject', str(shell.pid), '1'], input=b'echo ngh068',
                                capture_output=True, timeout=30)
        assert inject.returncode == 0 and 'error' not in json.loads(inject.stdout), inject.stdout
        deadline = time.monotonic() + 10
        text = ''
        while time.monotonic() < deadline:
            text = '\n'.join(json.loads(run(['_attach', 'peek', str(shell.pid), '0'], {**os.environ}, True,
                                            timeout=30).stdout)['lines'])
            if text.count('ngh068') >= 2:   # the typed command line and its echo
                break
            time.sleep(0.3)
        assert text.count('ngh068') >= 2, text
    finally:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(shell.pid)], capture_output=True)

"""Launcher parity (NGH-052): start/spawn/warden/gaffer/resume dry runs and every -h, oracle vs native.
The oracle side is a recorded golden (NGH-055): only testdata/record_goldens.py runs the Python oracle.

Run with NIGHTSHIFT_GO_EXE pointing at the built executable (default build/nightshift.exe). Every
case runs in disposable state roots, a disposable HOME and disposable Git repos. Nothing here
launches Windows Terminal, types into a pane or runs a non-dry-run start/spawn/gaffer: the only
"live crew" process is our own copied fake program renamed claude.exe that just sleeps, and
the only real thing read is an existing Terminal HWND that spawn's dry run checks for membership.

Normalised, and nothing else: the random 8-hex shift id; Python's launcher argv pair
"<sys.executable>", "<nightshift_shifts.py>" against the native executable's own path (Foreman's
rule, msg 10); wall-clock stamps written within the last three minutes (created_at, reaped_at,
a slot record's started). A command the native binary does not have yet ("unknown command") is
skipped, not weakened.

Deliberately not run: the top-level parser with NO arguments. The Python oracle then starts a
layout trial and launches wt.exe, which these tests must never do.
"""
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from test_go_readonly import ORACLE_BOOT, REBASE, RECORD, REPO, frozen, oracle, require_recording, write_json


# argparse's top-level prog is basename(argv[0]); the oracle gets the native executable's name so the
# top-level usage line compares like for like (subcommands set prog='nightshift X' explicitly).
BOOT = ORACLE_BOOT.replace("sys.argv = ['nightshift',",
                           "sys.argv = [os.environ.get('NIGHTSHIFT_ORACLE_ARGV0', 'nightshift'),")
assert BOOT != ORACLE_BOOT, 'ORACLE_BOOT argv line changed; update the argv0 patch'
BOOT = BOOT.replace('n.main()',"n.FLOOR_FILE = n.SOLID_DIR / 'floor.json'\n"
                                       "n.WORKTREES_DIR = n.SOLID_DIR / 'worktrees'\n"
                                       "n.BUILDS_DIR = n.SOLID_DIR / 'builds'\n"
                                       "n.TRIALS_DIR = n.SOLID_DIR / 'layout-trials'\n"
                                       # import-time constants of the live floor bus (Foreman msg 79)
                                       "n.FLOOR_DIR = n.SOLID_DIR / 'floor'\n"
                                       "n.FLOOR_BUS = n.FLOOR_DIR / 'bus.jsonl'\n"
                                       "n.FLOOR_CURSORS = n.FLOOR_DIR / 'cursors'\n"
                                       'n.main()')
DEAD_PID = 2147483000
INVALID_HWND = 0x7FFFFFFF
SHIFT_ID = re.compile(rb'Nightshift-shift-([0-9a-f]{8})(?![0-9A-Za-z])')
STAMP = re.compile(rb'"(created_at|reaped_at|dispatched_at|last_warden_at)": "'
                   rb'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"')
CREWS = sorted(p.stem for p in (REPO / 'crews').glob('*.yaml'))
FAKE_SOURCE = r'''package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// A fake tasks-cli (get ID prints FAKE_TASKS_DIR/ID.json), renamed claude.exe a harmless sleeper, and renamed
// agy/codex/copilot/agents-cli a stub that appends its argv, cwd and crew env to FAKE_RECORD_DIR/calls.jsonl
// and exits 7 without doing anything.
func main() {
	if len(os.Args) == 3 && os.Args[1] == "sleep" {
		n, _ := strconv.Atoi(os.Args[2])
		time.Sleep(time.Duration(n) * time.Second)
		return
	}
	name := strings.TrimSuffix(strings.ToLower(filepath.Base(os.Args[0])), ".exe")
	if name != "tasks-cli" {
		cwd, _ := os.Getwd()
		env := map[string]string{}
		for _, kv := range os.Environ() {
			k, v, _ := strings.Cut(kv, "=")
			u := strings.ToUpper(k)
			for _, p := range []string{"CLAUDE", "CODEX_", "COPILOT_", "DISABLE_", "TERM", "COLORTERM", "FORCE_COLOR", "NO_COLOR"} {
				if strings.HasPrefix(u, p) {
					env[u] = v
				}
			}
		}
		line, _ := json.Marshal(map[string]any{"exe": name, "argv": os.Args[1:], "cwd": cwd, "env": env})
		if dir := os.Getenv("FAKE_RECORD_DIR"); dir != "" {
			f, err := os.OpenFile(filepath.Join(dir, "calls.jsonl"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
			if err == nil {
				f.Write(append(line, '\n'))
				f.Close()
			}
		}
		os.Exit(7)
	}
	if len(os.Args) != 3 || os.Args[1] != "get" {
		fmt.Fprintln(os.Stderr, "fake tasks-cli: unsupported arguments")
		os.Exit(2)
	}
	data, err := os.ReadFile(filepath.Join(os.Getenv("FAKE_TASKS_DIR"), os.Args[2]+".json"))
	if err != nil {
		fmt.Fprintf(os.Stderr, "task %s not found\n", os.Args[2])
		os.Exit(1)
	}
	os.Stdout.Write(data)
}
'''


def exe_path():
    return Path(os.environ.get('NIGHTSHIFT_GO_EXE', REPO / 'build' / 'nightshift.exe'))


def go_toolchain():
    return os.environ.get('GOEXE') or shutil.which('go')


@pytest.fixture(scope='session')
def fake_bin(tmp_path_factory):
    """Directory holding the fake tasks-cli.exe (first on PATH) and a sleeper copy named claude.exe."""
    go = go_toolchain()
    if not go:
        pytest.skip('no Go toolchain to build the fake tasks-cli / sleeper')
    base = tmp_path_factory.mktemp('fake bin')
    (base / 'fake.go').write_text(FAKE_SOURCE, encoding='utf-8')
    bin_dir = base / 'bin'
    bin_dir.mkdir()
    built = subprocess.run([go, 'build', '-o', str(bin_dir / 'tasks-cli.exe'), str(base / 'fake.go')],
                           cwd=base, capture_output=True, timeout=300,
                           env={**os.environ, 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off', 'GOFLAGS': '',
                                'GO111MODULE': 'off'},
                           creationflags=subprocess.CREATE_NO_WINDOW)
    assert built.returncode == 0, built.stderr
    sleeper_dir = base / 'sleeper'
    sleeper_dir.mkdir()
    shutil.copyfile(bin_dir / 'tasks-cli.exe', sleeper_dir / 'claude.exe')
    shutil.copyfile(bin_dir / 'tasks-cli.exe', bin_dir / 'agents-cli.exe')
    return bin_dir, sleeper_dir / 'claude.exe'


@pytest.fixture(scope='session')
def sleeper(fake_bin):
    """(pid, started) of our own harmless claude.exe; the only process any fixture treats as a live member."""
    proc = subprocess.Popen([str(fake_bin[1]), 'sleep', '1800'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        time.sleep(0.5)
        assert proc.poll() is None
        started = oracle.process_start_epoch(proc.pid)
        assert started is not None, 'wmic must report the sleeper creation time (slot_state compares it)'
        assert oracle.pid_alive(proc.pid, started), 'sleeper must count as a live crew CLI'
        yield proc.pid, started
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture(scope='session')
def python_launcher():
    """The (sys.executable, script) pair the oracle child writes into wt_argv."""
    exe = subprocess.run([sys.executable, '-c', 'import sys; print(sys.executable)'],
                         capture_output=True, timeout=30).stdout.decode().strip()
    return exe, str((REPO / 'nightshift_shifts.py').resolve())


def git_env():
    return {'GIT_AUTHOR_NAME': 'Launcher Tester', 'GIT_AUTHOR_EMAIL': 'tester@example.invalid',
            'GIT_COMMITTER_NAME': 'Launcher Tester', 'GIT_COMMITTER_EMAIL': 'tester@example.invalid',
            'GIT_AUTHOR_DATE': '2026-01-02T03:04:05+0000', 'GIT_COMMITTER_DATE': '2026-01-02T03:04:05+0000',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0'}


def git(repo, *args):
    proc = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, timeout=30,
                          env={**os.environ, **git_env()}, creationflags=subprocess.CREATE_NO_WINDOW)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout.decode().strip()


def make_repo(path, branches=('main',)):
    path.mkdir(parents=True)
    git(path, 'init', '-q', '-b', 'main')
    git(path, 'config', 'core.autocrlf', 'false')
    git(path, 'config', 'commit.gpgsign', 'false')
    (path / 'readme.txt').write_text('fixture\n', encoding='utf-8')
    git(path, 'add', 'readme.txt')
    git(path, 'commit', '-qm', 'initial')
    for branch in branches[1:]:
        git(path, 'branch', branch)
    return path


def base_env(base, fake_bin):
    root, legacy, home = (base / name for name in ('state space', 'legacy space', 'home'))
    for d in (root / 'shifts', legacy / 'shifts', home):
        d.mkdir(parents=True, exist_ok=True)
    tasks = base / 'tasks'
    tasks.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if k not in ('COLUMNS', 'LINES', 'NIGHTSHIFT_ROLE', 'NIGHTSHIFT_IDENTITY', 'NIGHTSHIFT_ROLE_COLOUR')}
    env.update({'NIGHTSHIFT_STATE_ROOT': str(root), 'NIGHTSHIFT_LEGACY_ROOT': str(legacy),
                'NIGHTSHIFT_HOME': str(home), 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1',
                'FAKE_TASKS_DIR': str(tasks), 'NIGHTSHIFT_ORACLE_ARGV0': exe_path().name, **git_env(),
                # %TEMP% lies inside the home-folder git repo (Foreman msg 27): never let a fixture folder
                # discover that repository; only the fixture repos below base are repositories.
                'GIT_CEILING_DIRECTORIES': str(base)})
    if fake_bin:
        env['PATH'] = str(fake_bin[0]) + os.pathsep + env.get('PATH', '')
    user = os.environ.get('USERPROFILE', '').lower()
    paths = [p for p in env.get('PATH', '').split(os.pathsep)
             if p.strip() and not (
                 p.lower().startswith('c:\\tools') or
                 p.lower().startswith('c:\\scripts') or
                 'npm' in p.lower() or
                 'nvm4w' in p.lower() or
                 'codex' in p.lower() or
                 (user and p.lower().startswith(os.path.join(user, 'bin').lower()))
             )]
    env['PATH'] = os.pathsep.join(paths)
    return env


def run(args, env, native, timeout=60, stdin=None):
    if native:
        if not exe_path().is_file():
            pytest.fail(f'standalone executable missing: {exe_path()}; build it before parity verification')
        command = [str(exe_path()), *args]
    else:
        require_recording()
        command = [sys.executable, '-c', BOOT, *args]
    return subprocess.run(command, cwd=REPO, env=env, capture_output=True, timeout=timeout,
                          **({'input': stdin} if stdin is not None else {'stdin': subprocess.DEVNULL}),
                          creationflags=subprocess.CREATE_NO_WINDOW)


def normalise(data, launcher=None, native=False, exe=None):
    # Launcher first: the repository itself may live in a Nightshift-shift-<hex> worktree.
    if launcher and not native:
        pair = (re.escape(json.dumps(launcher[0], ensure_ascii=False).encode()) + rb',\r?\n\s*' +
                re.escape(json.dumps(launcher[1], ensure_ascii=False).encode()))
        data = re.sub(pair, b'"<launcher>"', data)
        # The {nightshift} brief placeholder: SUITE_PYTHON plus the script, posix form, inside text.
        data = data.replace(f'{oracle.SUITE_PYTHON} {Path(launcher[1]).as_posix()}'.encode(), b'<launcher>')
    if native:
        exe = Path(exe or exe_path())
        for path in {str(exe), str(exe.resolve()), os.path.abspath(exe)}:
            data = data.replace(json.dumps(path, ensure_ascii=False).encode(), b'"<launcher>"')
            data = data.replace(Path(path).as_posix().encode(), b'<launcher>')
    own = set(SHIFT_ID.findall(str(REPO).encode()))
    for hexid in set(SHIFT_ID.findall(data)) - own:
        data = data.replace(hexid, b'<id>')

    def stamp(match):
        value = time.mktime(time.strptime(match[2].decode(), '%Y-%m-%d %H:%M:%S'))
        return match[0].replace(match[2], b'<wall-clock>') if abs(time.time() - value) < 180 else match[0]
    return STAMP.sub(stamp, data)


def unported(proc):
    return b'unknown command' in proc.stdout + proc.stderr


def state_snapshot(env):
    roots = [Path(env[k]) for k in ('NIGHTSHIFT_STATE_ROOT', 'NIGHTSHIFT_LEGACY_ROOT', 'NIGHTSHIFT_HOME')]
    return {str(p): None if p.is_dir() else (p.read_bytes(), p.stat().st_mtime_ns)
            for root in roots if root.exists() for p in [root, *root.rglob('*')]}


def assert_equal_bytes(actual, expected, label):
    if actual == expected:
        return
    offset = next((i for i, (a, e) in enumerate(zip(actual, expected)) if a != e), min(len(actual), len(expected)))
    pytest.fail(f'{label} byte {offset}: expected={expected[max(0, offset - 60):offset + 140]!r}; '
                f'actual={actual[max(0, offset - 60):offset + 140]!r}')


# NGH-056 (Foreman msg 16): the frozen oracle has no crew lookup flags, so exactly these help lines differ. Each pair
# is (oracle lines, native lines); the oracle bytes are rewritten to the native lines and everything else stays
# byte-compared. The start usage block appears in `start -h` and in every start usage error.
NGH056_HELP_DELTAS = [
    ([b'                        [--continue-from SHIFT_DIR] [--crew CREW] [--dry-run]'],
     [b'                        [--continue-from SHIFT_DIR] [--crew CREW]',
      b'                        [--crew-file CREW_FILE] [--without ROLE]',
      b'                        [--role ROLE=TOOL:MODEL[:REASONING]]',
      b'                        [--max-credits N] [--dry-run]']),
    ([b'  --crew CREW           crew manifest crews/CREW.yaml: roles, briefs, colours,',
      b'                        counts, model (default: default = one Foreman, one',
      b'                        Advisor, one Coder, one Tester)'],
     [b'  --crew CREW           crew manifest CREW.yaml, first found in',
      b'                        <state root>/crews, <repo>/.nightshift/crews, then the',
      b'                        built-in crews/: roles, briefs, colours, counts, model',
      b'                        (default: default = one Foreman, one Advisor, one',
      b'                        Coder, one Tester); nightshift crews lists them',
      b'  --crew-file CREW_FILE',
      b'                        use this crew manifest file instead of --crew',
      b'  --without ROLE        drop ROLE from the crew (repeatable; not the',
      b'                        human_contact role)',
      b'  --role ROLE=TOOL:MODEL[:REASONING]',
      b'                        run ROLE on TOOL with MODEL and optional reasoning',
      b'                        (repeatable)',
      b'  --max-credits N       the shift\'s Copilot credit budget, split across the',
      b'                        Copilot panes (replaces the manifest\'s max_credits)']),
    ([b'                                              start a crew (git worktree by default; --no-worktree opts out)'],
     [b'                                              start a crew (git worktree by default; --no-worktree opts out)',
      b'  crews [--cwd DIR]                           crew manifests in lookup order (machine > repo > built-in)']),
    # Reserved Coder (Coder msg 18): the summon command follows consult.
    ([b'  consult SHIFT QUESTION --asg ID             start the reserved Advisor with a durable first assignment'],
     [b'  consult SHIFT QUESTION --asg ID             start the reserved Advisor with a durable first assignment',
      b'  summon SHIFT BODY --asg ID [--kind assignment|uplift] [--reason R]',
      b'                                              start the reserved Coder (crews with an Apprentice) the same way']),
]

# NGH-060 pull-based bus: the five commands follow ack in the top-level Commands list.
NGH060_HELP_DELTAS = [
    ([b'  ack SHIFT ROLE ID                           acknowledge and print the stored body; exit 5 = do not act'],
     [b'  ack SHIFT ROLE ID                           acknowledge and print the stored body; exit 5 = do not act',
      b'  ready SHIFT ROLE                            record READY for the lead; exit 4 = already READY',
      b'  assign SHIFT ROLE TEXT [--asg ID] [--coding-asg CID] [--ready-timeout S]',
      b'                                              store once ROLE is READY and nudge it; exit 3 = not ready',
      b'  note SHIFT ROLE TEXT [--asg ID] [--ready-timeout S]',
      b'                                              the same for a lead -> member note',
      b'  report SHIFT ROLE TEXT [--asg ID]           store ROLE\'s result for the lead; first line = verdict',
      b'  wait SHIFT ROLE [--timeout S] [--json]      block for the next message, ack it, print the body; exit 3 = timeout']),
]


# NGH-061: respawn/uplift after summon; build gains --asg and the loop exit; commit/handover/result after build --cancel;
# build -h gains the --asg option. Applied after NGH060_HELP_DELTAS (the wait line it anchors on comes from those).
NGH061_HELP_DELTAS = [
    ([b'                                              start the reserved Coder (crews with an Apprentice) the same way'],
     [b'                                              start the reserved Coder (crews with an Apprentice) the same way',
      b'  respawn SHIFT ROLE                          restart a dead member in its slot; its active assignment is the first prompt',
      b'  uplift SHIFT ASG TEXT --reason needs-senior|tester-failed-twice|timeout',
      b'                                              Apprentice -> Coder uplift (summons a not-started Coder); exit 4 = refused']),
    ([b"  build SHIFT [--cwd DIR] [--cmd 'COMMAND'] [--timeout MIN]",
      b'                                              run a build one at a time per folder; evidence in builds/<n>.json',
      b'  build --cancel SHIFT N                      kill build N and mark it cancelled'],
     [b"  build SHIFT [--cwd DIR] [--cmd 'COMMAND'] [--timeout MIN] [--asg ID]",
      b'                                              run a build one at a time per folder; evidence in builds/<n>.json;',
      b'                                              exit 4 = loop detected (a 4th same failure for --asg)',
      b'  build --cancel SHIFT N                      kill build N and mark it cancelled',
      b'  commit SHIFT ASG -m MSG                     commit exactly the Tester-passed tree for ASG; exit 4 = differs/no pass',
      b'  handover SHIFT                              write handover.md from live state (assignments, claims, waits, reports)',
      b"  result SHIFT done|blocked --stage RUNG --summary TEXT [--changed PATH ...] [--evidence 'stage|what|outcome' ...]",
      b'                                              write result.json with bus, builds, uplift counts and usage']),
    ([b'  --cancel           cancel build N of SHIFT'],
     [b'  --cancel           cancel build N of SHIFT',
      b'  --asg ASG          the assignment this build verifies; a fourth build after',
      b'                     three failures with the same error is refused (exit 4)']),
]
# NGH-061: spawn's no-free-slot refusal adds one stderr line naming reserved slots and the Ops pane (native only).
NGH061_SPAWN_LINE = re.compile(rb'reserved slots: [^\r\n]*\r?\n$')


def apply_help_deltas(data, deltas):
    applied = 0
    for old, new in deltas:
        if new[1] in data:   # already present (a golden re-recorded from the native side): never duplicate
            continue
        pattern = rb'\r?\n'.join(re.escape(line) for line in old) + rb'(?=\r?\n)'
        match = re.search(pattern, data)
        if match:
            nl = b'\r\n' if b'\r\n' in match[0] or b'\r\n' in data else b'\n'
            data = data[:match.start()] + nl.join(new) + data[match.end():]
            applied += 1
    return data, applied


def reserve_coder(plan):
    """NGH-056 reserved Coder (Coder msg 18), applied to the oracle's start --dry-run plan: when the crew has an
    Apprentice, the Coder is held like the Advisor. Exactly three things change: roles.Coder gains status and
    reserved_slot after first_message, its slots entry has role null, and its wt_argv cell becomes the empty-slot
    shell (cmd.exe /k <launcher> --slot N --state-dir D, no colour scheme). Returns the plan unchanged otherwise."""
    roles = plan.get('roles') or {}
    if 'Apprentice' not in roles or 'Coder' not in roles:
        return plan
    slots = plan.get('slots')
    assert slots, 'a split-layout Apprentice crew would need slot promotion; no parity fixture has one'
    n = next(s['slot'] for s in slots if s['role'] == 'Coder')
    coder = {}
    for key, value in roles['Coder'].items():
        coder[key] = value
        if key == 'first_message':
            coder['status'], coder['reserved_slot'] = 'not_started', n
    roles['Coder'] = coder
    slots[n - 1]['role'] = None
    argv, out, segment = plan['wt_argv'], [], []
    for token in argv + [';']:
        if token != ';':
            segment.append(token)
            continue
        if '--pane' in segment and segment[segment.index('--pane') + 1] == 'Coder':
            d, pane = segment.index('-d'), segment.index('--pane')
            head = segment[:d + 2]
            if '--colorScheme' in head:
                i = head.index('--colorScheme')
                del head[i:i + 2]
            segment = head + ['cmd.exe', '/k', *segment[d + 2:pane], '--slot',
                              segment[segment.index('--slot') + 1], '--state-dir',
                              segment[segment.index('--state-dir') + 1]]
        out.extend(segment + [';'])
        segment = []
    plan['wt_argv'] = out[:-1]
    return plan


# NGH-062: the start --dry-run plan gains per-role brief_mode/brief_layers/brief_warnings and start prints the brief
# warnings on stderr (native only, outside the frozen oracle). Parity compares the rest; test_go_brief_render.py and
# brief_render_test.go check the additions themselves.
NGH062_PLAN_KEYS = ('brief_mode', 'brief_layers', 'brief_warnings')
NGH062_WARNING_LINE = re.compile(rb'(?m)^brief \S+: (role|repository|site) layer \d+ words, budget \d+\r?\n')


def without_ngh065(args, stdout):
    """NGH-065: warden (and gaffer's patrol) reports gain a per-shift `renudged` list (native only). test_go_nudge_retry.py
    checks it; parity compares the rest."""
    if args[:1] not in (['warden'], ['gaffer']) or not stdout.lstrip().startswith(b'{') or b'"renudged"' not in stdout:
        return stdout

    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != 'renudged'}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    ascii_only = all(b < 0x80 for b in stdout)   # gaffer escapes non-ASCII as \uXXXX; keep the native style
    rendered = (json.dumps(strip(json.loads(stdout)), indent=2, ensure_ascii=ascii_only) + '\n').encode('utf-8')
    return rendered.replace(b'\n', b'\r\n') if b'\r\n' in stdout else rendered


def without_ngh062(args, stdout, stderr):
    stdout = without_ngh065(args, stdout)
    stderr = NGH062_WARNING_LINE.sub(b'', stderr)
    if args[:1] != ['start'] or '--dry-run' not in args or not stdout.lstrip().startswith(b'{'):
        return stdout, stderr
    plan = json.loads(stdout)
    roles = plan.get('roles')
    if not isinstance(roles, dict) or not any(isinstance(r, dict) and set(NGH062_PLAN_KEYS) & set(r)
                                              for r in roles.values()):
        return stdout, stderr
    plan['roles'] = {k: {f: v for f, v in r.items() if f not in NGH062_PLAN_KEYS} if isinstance(r, dict) else r
                     for k, r in roles.items()}
    rendered = (json.dumps(plan, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    return (rendered.replace(b'\n', b'\r\n') if b'\r\n' in stdout else rendered), stderr


def oracle_with_reserved_coder(args, data):
    """The oracle's start --dry-run stdout re-rendered with reserve_coder (json.dumps as the oracle prints it)."""
    if args[:1] != ['start'] or '--dry-run' not in args or not data.lstrip().startswith(b'{'):
        return data
    plan = json.loads(data)
    rendered = (json.dumps(reserve_coder(plan), indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    return rendered.replace(b'\n', b'\r\n') if b'\r\n' in data else rendered


def ngh056_expected(data):
    """The oracle's bytes with the NGH-056 help deltas applied; (bytes, number of deltas applied)."""
    applied = 0
    for old, new in NGH056_HELP_DELTAS:
        pattern = rb'\r?\n'.join(re.escape(line) for line in old) + rb'(?=\r?\n)'
        match = re.search(pattern, data)
        if match:
            nl = b'\r\n' if b'\r\n' in match[0] or b'\r\n' in data else b'\n'
            data = data[:match.start()] + nl.join(new) + data[match.end():]
            applied += 1
    return data, applied


def test_ngh056_help_deltas_hit_the_oracle_text(tmp_path):
    """Guard the exception itself: every delta matches the oracle exactly once per help it belongs to."""
    env = base_env(tmp_path / 'help space', None)
    start_help = frozen(lambda: run(['start', '-h'], env, native=False).stdout, tmp_path)
    top_help = frozen(lambda: run(['-h'], env, native=False).stdout, tmp_path)
    assert ngh056_expected(start_help)[1] == 2 and ngh056_expected(top_help)[1] == 2
    for old, _ in NGH056_HELP_DELTAS:
        assert sum(normalise(h).count(old[0]) for h in (start_help, top_help)) == 1, old[0]
    # NGH-060: its delta anchors on the oracle's ack line, once in the top-level help and never in start -h.
    assert apply_help_deltas(top_help, NGH060_HELP_DELTAS)[1] == 1
    assert apply_help_deltas(start_help, NGH060_HELP_DELTAS)[1] == 0


def volatile(env):
    """Live values a world wrote into its fixtures (sleeper PID and start epoch, a real Terminal HWND)."""
    return json.loads(env.get('GOLDEN_VOLATILE', '{}'))


def world_root(env):
    return Path(env['NIGHTSHIFT_HOME']).parent


def oracle_expected(args, env, launcher, before):
    """The oracle's run as the native side must reproduce it: launcher pair, NGH-056 deltas and the reserved
    Coder applied. Frozen (NGH-055), so only recording runs the oracle."""
    def compute():
        expected = run(args, env, native=False)
        assert state_snapshot(env) == before, f'Python oracle wrote state for dry-run/usage case {args}'
        return (ngh056_expected(normalise(oracle_with_reserved_coder(args, expected.stdout), launcher, False))[0],
                ngh056_expected(normalise(expected.stderr, launcher, False))[0], expected.returncode)
    def native():
        actual = run(args, env, native=True)
        stdout, stderr = without_ngh062(args, actual.stdout, actual.stderr)
        return normalise(stdout, launcher, True), normalise(stderr, launcher, True), actual.returncode
    stdout, stderr, code = frozen(native if REBASE else compute, world_root(env), volatile(env), record=REBASE)
    if not REBASE:
        # NGH-060 postdates the recorded goldens: its help lines go on top of the frozen value, never into it.
        stdout = apply_help_deltas(stdout, NGH060_HELP_DELTAS + NGH061_HELP_DELTAS)[0]
    return subprocess.CompletedProcess(args, code, stdout, stderr)


def parity(args, env, launcher=None, expect_exit=None):
    """Frozen oracle then native on the same world; both must leave the state roots untouched."""
    before = state_snapshot(env)
    expected = oracle_expected(args, env, launcher, before)
    if expect_exit is not None:
        assert expected.returncode == expect_exit, (expected.stdout, expected.stderr)
    actual = run(args, env, native=True)
    if unported(actual):
        pytest.skip(f'native binary has not ported {args[0]} yet: {actual.stderr[:80]!r}')
    assert state_snapshot(env) == before, f'native binary wrote state for dry-run/usage case {args}'
    stdout, stderr = without_ngh062(args, actual.stdout, actual.stderr)
    for label, a, e in (('stdout', stdout, expected.stdout),
                        ('stderr', NGH061_SPAWN_LINE.sub(b'', stderr), expected.stderr)):
        assert_equal_bytes(normalise(a, launcher, True), e, f'{args} {label}')
    assert actual.returncode == expected.returncode, (args, actual.stderr)
    return expected


# ---------------------------------------------------------------- start --dry-run


@pytest.fixture
def start_world(tmp_path, fake_bin):
    base = tmp_path / 'launcher café'
    env = base_env(base, fake_bin)
    repo = make_repo(base / 'repo space Ω', ('main', 'develop'))
    configured = make_repo(base / 'configured repo', ('main', 'release'))
    (configured / '.nightshift' / 'crews').mkdir(parents=True)
    (configured / '.nightshift' / 'config.yaml').write_text(
        'target_branch: release\ndefault_crew: local\nbuild:\n  cmd: pytest -q\n  timeout: 30\n', encoding='utf-8')
    (configured / '.nightshift' / 'crews' / 'local.yaml').write_text(
        'name: local\nlayout: 1x4\nroles:\n'
        '  - {role: Foreman, brief: foreman.md, human_contact: true}\n'
        '  - {role: Coder, brief: coder.md, count: 2, colour: purple}\n', encoding='utf-8')
    # The repository layer of the prompt stack (NGH-046): shared instructions plus per-role files.
    (configured / '.nightshift' / 'instructions.md').write_text('Repository rule café: {cwd} {shift}\n',
                                                                encoding='utf-8')
    (configured / '.nightshift' / 'coder.md').write_text('Coder-only repository rule.\n', encoding='utf-8')
    (configured / '.nightshift' / 'foreman.md').write_text('Foreman-only repository rule.\n', encoding='utf-8')
    plain = base / 'plain folder'
    plain.mkdir()
    semicolon = base / 'semi;colon'
    semicolon.mkdir()
    for folder in (plain, semicolon, base / 'tasks'):
        probe = subprocess.run(['git', '-C', str(folder), 'rev-parse', '--show-toplevel'], env=env,
                               capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        assert probe.returncode != 0, f'fixture folder {folder} is inside a git repository: {probe.stdout!r}'
    earlier = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-earlier'
    earlier.mkdir()
    (earlier / 'handover.md').write_text('# Handover\n\nCommitted 1234abc; tests café 🚀 pending.\n' + 'x' * 7000,
                                         encoding='utf-8')
    prompt = base / 'task file.txt'
    prompt.write_text('Fix "quoted" & | < > ^ % café 🚀\nrequires: deployed\n', encoding='utf-8')
    tasks = Path(env['FAKE_TASKS_DIR'])
    write_json(tasks / 'NGH-901.json', {'task_id': 'NGH-901', 'title': 'Café ticket "one"',
                                        'body': 'Do the thing.\nrequires: validated\n',
                                        'frontmatter': {'codebase': str(repo)}})
    write_json(tasks / 'NGH-902.json', {'task_id': 'NGH-902', 'title': 'Body codebase',
                                        'body': f'codebase: {plain}\nNo frontmatter codebase.\n',
                                        'frontmatter': {}})
    write_json(tasks / 'NGH-903.json', {'task_id': 'NGH-903', 'title': 'No codebase', 'body': '  ',
                                        'frontmatter': {}})
    return {'env': env, 'repo': repo, 'configured': configured, 'plain': plain, 'semicolon': semicolon,
            'earlier': earlier, 'prompt': prompt, 'base': base}


START_CASES = {
    'default-git-worktree': lambda w: ['start', '--dry-run', '--cwd', str(w['repo']), 'Add café "flag" 🚀'],
    'no-worktree-lock': lambda w: ['start', '--dry-run', '--no-worktree', '--cwd', str(w['repo']), 'In place'],
    'plain-folder-lock': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']), 'Plain folder task'],
    'target-branch': lambda w: ['start', '--dry-run', '--cwd', str(w['repo']), '--target-branch', 'develop', 'T'],
    'target-branch-missing': lambda w: ['start', '--dry-run', '--cwd', str(w['repo']), '--target-branch',
                                        'nope', 'T'],
    'prompt-file-requires': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']),
                                       '--prompt-file', str(w['prompt'])],
    'continue-from-handover': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']),
                                         '--continue-from', str(w['earlier']), 'Carry on'],
    'continue-from-missing-handover': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']),
                                                 '--continue-from', str(w['base'] / 'nowhere'), 'Carry on'],
    'free-text-ticket-header': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']),
                                          'Ticket ABC-12: header only\nrequires: implemented'],
    'ticket-frontmatter-codebase': lambda w: ['start', '--dry-run', 'NGH-901'],
    'ticket-body-codebase': lambda w: ['start', '--dry-run', ' NGH-902 '],
    'ticket-cwd-override': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']), 'NGH-901'],
    'ticket-tasks-cli-fails': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']), 'NGH-999'],
    'ticket-empty-body-no-codebase': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']), 'NGH-903'],
    'repo-config-local-crew': lambda w: ['start', '--dry-run', '--cwd', str(w['configured']), 'Configured'],
    'repo-config-explicit-crew': lambda w: ['start', '--dry-run', '--cwd', str(w['configured']),
                                            '--crew', 'codex', 'Configured'],
    'unknown-crew': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']), '--crew', 'nosuch', 'T'],
    'error-prompt-and-file': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']),
                                        '--prompt-file', str(w['prompt']), 'both'],
    'error-empty-prompt': lambda w: ['start', '--dry-run', '--cwd', str(w['plain']), '   '],
    'error-no-prompt': lambda w: ['start', '--dry-run', '--cwd', str(w['plain'])],
    'error-missing-cwd': lambda w: ['start', '--dry-run', '--cwd', str(w['base'] / 'absent folder'), 'T'],
    'error-semicolon-cwd': lambda w: ['start', '--dry-run', '--cwd', str(w['semicolon']), 'T'],
    'error-unknown-flag': lambda w: ['start', '--dry-run', '--bogus', 'T'],
}


@pytest.mark.parametrize('name', START_CASES)
def test_start_dry_run_matches_oracle(start_world, python_launcher, name):
    parity(START_CASES[name](start_world), start_world['env'], python_launcher)


def test_start_missing_prompt_file_reports_the_oracle_error_line(start_world):
    """Python dies with an uncaught FileNotFoundError traceback (not contract, Foreman msg 18); native must
    print exactly the traceback's final message line, exit like it and print nothing on stdout."""
    args = ['start', '--dry-run', '--prompt-file', str(start_world['base'] / 'none.txt')]
    env = start_world['env']

    def oracle_side():
        expected = run(args, env, native=False)
        assert expected.returncode == 1 and expected.stderr.startswith(b'Traceback')
        return expected.stdout, expected.returncode, expected.stderr.rstrip().splitlines()[-1]
    stdout, code, last = frozen(oracle_side, world_root(env))
    assert last.startswith(b'FileNotFoundError: ')
    actual = run(args, env, native=True)
    if unported(actual):
        pytest.skip('native binary has not ported start yet')
    assert (actual.stdout, actual.returncode) == (stdout, code)
    assert actual.stderr.rstrip(b'\r\n') == last.removeprefix(b'FileNotFoundError: ')


@pytest.mark.parametrize('crew', CREWS)
@pytest.mark.parametrize('where', ['repo', 'plain'])
def test_start_dry_run_every_crew_manifest(start_world, python_launcher, crew, where):
    args = ['start', '--dry-run', '--cwd', str(start_world[where]), '--crew', crew, f'Crew {crew} task']
    expected = parity(args, start_world['env'], python_launcher, expect_exit=0)
    plan = json.loads(expected.stdout)
    assert plan['dry_run'] is True and plan.get('crew', 'default') == crew


def test_start_dry_run_work_crew_through_native_binary(start_world):
    """Native start dry-run of the work crew exposes copilot coder and reserved advisor without writing state."""
    args = ['start', '--dry-run', '--crew', 'work', '--cwd', str(start_world['repo']), 'dry']
    env = start_world['env']
    before = state_snapshot(env)
    proc = run(args, env, native=True)
    if unported(proc):
        pytest.skip('native binary has not ported start yet')
    assert proc.returncode == 0, proc.stderr
    assert state_snapshot(env) == before
    plan = json.loads(proc.stdout)
    assert plan['crew'] == 'work'
    assert plan['max_credits'] == 60
    roles = plan['roles']
    assert roles['Advisor']['status'] == 'not_started' and roles['Advisor']['reserved_slot'] == 3
    for label in ('Coder', 'Tester'):
        assert roles[label]['tool'] == 'copilot' and roles[label]['model'] == 'gemini-3.8-flash'
        cmd = roles[label]['command']
        idx = cmd.index('--max-ai-credits')
        assert cmd[idx + 1] == '30'
    assert 'tool' not in roles['Foreman']


def test_start_dry_run_launcher_normalisation_is_the_only_argv_difference(start_world, python_launcher):
    """Guard the normaliser itself: the oracle's wt_argv really holds the pair, and it collapses to one token."""
    args = START_CASES['default-git-worktree'](start_world)
    out = frozen(lambda: run(args, start_world['env'], native=False).stdout, world_root(start_world['env']),
                 roots={'PYTHON': Path(python_launcher[0])})
    argv = json.loads(out)['wt_argv']
    pairs = [i for i in range(len(argv) - 1) if (argv[i], argv[i + 1]) == python_launcher]
    assert pairs, 'oracle wt_argv lost the python/script pair; the normaliser would hide nothing'
    collapsed = json.loads(normalise(out, python_launcher))['wt_argv']
    assert collapsed.count('<launcher>') == len(pairs) and len(collapsed) == len(argv) - len(pairs)


def test_native_copy_without_crews_beside_it_uses_embedded_assets(start_world, python_launcher, tmp_path):
    """NGH-046: a copied nightshift.exe in an empty folder must still load the default crew and briefs."""
    if not exe_path().is_file():
        pytest.fail(f'standalone executable missing: {exe_path()}')
    lonely = tmp_path / 'empty folder Ω'
    lonely.mkdir()
    copied = lonely / 'nightshift.exe'
    shutil.copyfile(exe_path(), copied)
    env = start_world['env']
    for args in (['start', '--dry-run', '--cwd', str(start_world['plain']), 'Embedded assets'],
                 ['start', '--dry-run', '--cwd', str(start_world['plain']), '--crew', 'codex', 'Embedded'],
                 ['start', '--dry-run', '--cwd', str(start_world['configured']), 'Repo layer beside embed']):
        def oracle_side():
            expected = run(args, env, native=False)
            return (normalise(oracle_with_reserved_coder(args, expected.stdout), python_launcher, False),
                    expected.returncode)
        actual = subprocess.run([str(copied), *args], cwd=lonely, env=env, capture_output=True, timeout=60,
                                stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)

        native_stdout = without_ngh062(args, actual.stdout, b'')[0]   # NGH-062 plan fields, native only

        def native_side():
            return normalise(native_stdout, python_launcher, True, exe=copied), actual.returncode
        stdout, code = frozen(native_side if REBASE else oracle_side, tmp_path, record=REBASE)
        if unported(actual):
            pytest.skip('native binary has not ported start yet')
        assert actual.returncode == code == 0, actual.stderr
        assert_equal_bytes(normalise(native_stdout, python_launcher, True, exe=copied), stdout, f'embedded {args}')
    assert sorted(p.name for p in lonely.iterdir()) == ['nightshift.exe'], 'embedded assets must not be unpacked'


# ---------------------------------------------------------------- loop usage (never a real loop)


LOOP_CASES = {
    'neither-tickets-nor-project': ['loop'],
    'both-tickets-and-project': ['loop', 'NGH-1', '--project', 'NGH'],
    'max-not-int': ['loop', 'NGH-1', '--max', 'x'],
    'stall-not-int': ['loop', 'NGH-1', '--stall-minutes', 'soon'],
    'unknown-flag': ['loop', 'NGH-1', '--bogus'],
}


@pytest.mark.parametrize('name', LOOP_CASES)
def test_loop_usage_errors_match_oracle(tmp_path, fake_bin, name):
    env = base_env(tmp_path / 'loop space', fake_bin)
    # Belt and braces should a native loop mis-parse and carry on: no wt.exe alias on PATH, and the fake
    # tasks-cli knows no ticket, so any accidental start fails before a window could open.
    env['PATH'] = os.pathsep.join(p for p in env['PATH'].split(os.pathsep) if 'windowsapps' not in p.lower())
    expected = parity(LOOP_CASES[name], env)
    assert expected.returncode == 2


# ---------------------------------------------------------------- spawn --dry-run


def real_terminal_hwnd():
    windows = oracle.terminal_windows()
    return next(iter(windows), None)


@pytest.fixture
def spawn_world(tmp_path, sleeper):
    hwnd = real_terminal_hwnd()
    if hwnd is None:
        pytest.skip('spawn dry-run needs an open Windows Terminal window to check (never launched here)')
    pid, started = sleeper
    base = tmp_path / 'spawn space'
    env = base_env(base, None)
    shifts = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts'
    work = base / 'work café'
    work.mkdir()

    def member(label, role=None, tool='claude', model='claude-opus-5-5', human=False, **extra):
        return {'role': role or label, 'label': label, 'identity': f'{{shift}}-{label.lower()}',
                'brief': f'{(role or label).lower()}.md', 'colour': 'red' if human else 'green',
                'human_contact': human, 'tool': tool, 'model': model, **extra}

    def shift(name, members, slots, crew='default', live=(), dead=(), max_credits=None, window=True,
              slot_roles=None):
        d = shifts / name
        d.mkdir()
        for m in members:
            m['identity'] = m['identity'].replace('{shift}', name)
        record = {'crew': crew, 'members': members, 'slots': slots, 'permission_mode': 'auto',
                  'model': 'claude-opus-5-5', 'cwd': str(work)}
        if max_credits is not None:
            record['max_credits'] = max_credits
        write_json(d / 'crew.json', record)
        if window:
            write_json(d / 'launch.json', {'hwnd': hwnd})
        for label in live:
            write_json(d / f'{label}.json', {'pid': pid, 'started': started, 'tool': 'claude', 'cwd': str(work)})
        for label in dead:
            write_json(d / f'{label}.json', {'pid': DEAD_PID, 'started': 1000.0, 'cwd': str(work)})
        for n in range(1, (slots or 0) + 1):
            role = (slot_roles or {}).get(n)
            write_json(d / 'slots' / f'{n}.json', {'slot': n, 'role': role, 'cmd_pid': pid, 'started': 900.0,
                                                    **({'spawned': 500.0} if role else {})})
        return d

    shift('Nightshift-shift-spawnfix',
          [member('Foreman', human=True), member('Coder'),
           member('Apprentice', tool='agy', model='gemini-3.8-flash-high', permissions='skip')],
          slots=4, live=['Foreman'], dead=['Coder', 'Apprentice'],
          slot_roles={1: 'Foreman', 2: 'Coder', 3: 'Apprentice'})
    shift('Nightshift-shift-fullfix', [member('Foreman', human=True), member('Coder')], slots=2,
          live=['Foreman', 'Coder'], slot_roles={1: 'Foreman', 2: 'Coder'})
    shift('Nightshift-shift-budgetfix',
          [member('Foreman', human=True),
           member('Coder', tool='copilot', model='gemini-3.8-flash', max_credits=30)],
          slots=8, crew='copilot', live=['Foreman'], max_credits=90, slot_roles={1: 'Foreman', 2: 'Coder'},
          dead=['Coder'])
    shift('Nightshift-shift-brokefix',
          [member('Foreman', human=True),
           member('Coder', tool='copilot', model='gemini-3.8-flash', max_credits=30),
           member('Tester', tool='copilot', model='gemini-3.8-flash', max_credits=30)],
          slots=8, live=['Foreman', 'Coder', 'Tester'], max_credits=60,
          slot_roles={1: 'Foreman', 2: 'Coder', 3: 'Tester'})
    shift('Nightshift-shift-splitfix', [member('Foreman', human=True), member('Coder')], slots=None)
    shift('Nightshift-shift-windowless', [member('Foreman', human=True), member('Coder')], slots=4, window=False)
    # acc1f52: the manifest Advisor's slot is reserved until `consult` records a consultation.
    for name, consult in (('Nightshift-shift-advisorres', None),
                          ('Nightshift-shift-advisorgo', {'question': 'Why?', 'asg': 'adv-1'})):
        d = shift(name, [member('Foreman', human=True), member('Advisor', model='claude-fable-5-1'), member('Coder')],
                  slots=4, live=['Foreman'], dead=['Coder'], slot_roles={1: 'Foreman', 3: 'Coder'})
        record = json.loads((d / 'crew.json').read_text(encoding='utf-8'))
        write_json(d / 'crew.json', {**record, 'advisor_slot': 2, 'consult': consult})
    # Deterministic mtimes keep "latest" and unique-suffix resolution independent of creation timing.
    for i, d in enumerate(sorted(shifts.iterdir())):
        os.utime(d, (1700000000 + i, 1700000000 + i))
    env['GOLDEN_VOLATILE'] = json.dumps({'SLEEPER_PID': pid, 'SLEEPER_STARTED': started, 'TERMINAL_HWND': hwnd})
    return env


SPAWN_CASES = {
    'new-manifest-role-advisor': ['spawnfix', '--role', 'Advisor'],
    'new-label-coder2': ['spawnfix', '--role', 'Coder2'],
    'dead-member-own-slot': ['spawnfix', '--role', 'coder'],
    'dead-member-other-cli': ['spawnfix', '--role', 'Coder', '--cli', 'codex'],
    'dead-agy-member-keeps-tool': ['spawnfix', '--role', 'Apprentice'],
    'cli-claude': ['spawnfix', '--role', 'Helper', '--cli', 'claude'],
    'cli-gemini': ['spawnfix', '--role', 'Helper', '--cli', 'gemini'],
    'cli-codex': ['spawnfix', '--role', 'Helper', '--cli', 'codex'],
    'cli-copilot': ['spawnfix', '--role', 'Helper', '--cli', 'copilot'],
    'cli-opencode-unwired': ['spawnfix', '--role', 'Helper', '--cli', 'opencode'],
    'cli-invalid-choice': ['spawnfix', '--role', 'Helper', '--cli', 'qwen'],
    'crew-apprentice-codex': ['spawnfix', '--role', 'Apprentice', '--crew', 'apprentice-codex'],
    'crew-apprentice-gemini': ['spawnfix', '--role', 'Apprentice', '--crew', 'apprentice-gemini'],
    'crew-apprentice-copilot': ['spawnfix', '--role', 'Apprentice', '--crew', 'apprentice-copilot'],
    'crew-with-explicit-cli': ['spawnfix', '--role', 'Apprentice', '--crew', 'apprentice-codex', '--cli', 'claude'],
    'crew-missing-role': ['spawnfix', '--role', 'Wizard', '--crew', 'apprentice-codex'],
    'crew-unknown': ['spawnfix', '--role', 'Coder', '--crew', 'nosuch'],
    'explicit-free-slot': ['spawnfix', '--role', 'Helper', '--slot', '4'],
    'explicit-dead-slot': ['spawnfix', '--role', 'Helper', '--slot', '2'],
    'explicit-taken-slot': ['spawnfix', '--role', 'Helper', '--slot', '1'],
    'slot-out-of-range': ['spawnfix', '--role', 'Helper', '--slot', '5'],
    'slot-not-integer': ['spawnfix', '--role', 'Helper', '--slot', 'two'],
    'live-role-refused': ['spawnfix', '--role', 'Foreman'],
    'all-slots-taken': ['fullfix', '--role', 'Helper'],
    'copilot-budget-share-new': ['budgetfix', '--role', 'Tester', '--cli', 'copilot'],
    'copilot-budget-share-own': ['budgetfix', '--role', 'Coder'],
    'copilot-budget-share-crew': ['budgetfix', '--role', 'Apprentice', '--crew', 'apprentice-copilot'],
    'copilot-budget-exhausted': ['brokefix', '--role', 'Helper', '--cli', 'copilot'],
    'non-copilot-in-budget-shift': ['budgetfix', '--role', 'Helper', '--cli', 'codex'],
    'split-layout-refused': ['splitfix', '--role', 'Helper'],
    'window-gone': ['windowless', '--role', 'Helper'],
    'bad-role-name': ['spawnfix', '--role', '2bad'],
    'missing-shift': ['nosuchshift', '--role', 'Helper'],
    'ambiguous-shift': ['fix', '--role', 'Helper'],
    'missing-role-flag': ['spawnfix'],
    'advisor-slot-refused-to-others': ['advisorres', '--role', 'Helper', '--slot', '2'],
    'others-skip-advisor-slot': ['advisorres', '--role', 'Helper'],
    'advisor-other-slot-refused': ['advisorres', '--role', 'Advisor', '--slot', '4'],
    'advisor-without-consult': ['advisorres', '--role', 'Advisor'],
    'advisor-with-consult': ['advisorgo', '--role', 'Advisor'],
    'advisor-with-consult-own-slot': ['advisorgo', '--role', 'Advisor', '--slot', '2'],
    'dead-coder-beside-reserved-advisor': ['advisorres', '--role', 'Coder'],
}


@pytest.mark.parametrize('name', SPAWN_CASES)
def test_spawn_dry_run_matches_oracle(spawn_world, name):
    # --dry-run on every case, usage errors included (Foreman msg 27): a mis-parse must never go live.
    parity(['spawn', *SPAWN_CASES[name], '--dry-run'], spawn_world)


def test_spawn_dry_run_oracle_cases_reach_the_intended_branch(spawn_world):
    """The fixture must actually exercise slot reuse and the budget split, not fail early on both sides."""
    def plan(name):
        out = run(['spawn', *SPAWN_CASES[name], '--dry-run'], spawn_world, native=True)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    assert plan('dead-member-own-slot')['slot'] == 2
    assert plan('dead-member-own-slot')['member']['replacement'] is True
    assert plan('new-label-coder2')['slot'] == 2
    assert plan('explicit-free-slot')['slot'] == 4
    assert plan('copilot-budget-share-new')['member']['max_credits'] == 30
    assert plan('crew-apprentice-codex')['tool'] == 'codex'
    exhausted = run(['spawn', *SPAWN_CASES['copilot-budget-exhausted'], '--dry-run'], spawn_world, native=True)
    assert exhausted.returncode == 1 and b'no budget share left' in exhausted.stderr
    assert plan('others-skip-advisor-slot')['slot'] == 3 and plan('advisor-with-consult')['slot'] == 2
    for name, text in [('advisor-slot-refused-to-others', b'Advisor slot 2 is reserved until consultation'),
                       ('advisor-other-slot-refused', b'Advisor is reserved for slot 2'),
                       ('advisor-without-consult', b'use consult to start the Advisor')]:
        refused = run(['spawn', *SPAWN_CASES[name], '--dry-run'], spawn_world, native=True)
        assert refused.returncode == 1 and text in refused.stderr, name


# ---------------------------------------------------------------- warden / gaffer / cleaner / resume


def make_mailbox_rows(d, rows):
    conn = sqlite3.connect(d / 'mailbox.sqlite3')
    try:
        conn.execute(oracle.MAILBOX_SCHEMA)
        for name, kind in [('delivered_epoch', 'REAL'), ('coding_asg', 'TEXT'), ('source_dispatch_id', 'INTEGER'),
                           ('reason', 'TEXT'), ('evidence', 'TEXT')]:
            conn.execute(f'ALTER TABLE messages ADD COLUMN {name} {kind}')
        for row in rows:
            conn.execute('INSERT INTO messages (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')',
                         list(row.values()))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def floor_world(tmp_path, sleeper):
    pid, started = sleeper
    base = tmp_path / 'floor space'
    env = base_env(base, None)
    root = Path(env['NIGHTSHIFT_STATE_ROOT'])
    shifts = root / 'shifts'
    home = Path(env['NIGHTSHIFT_HOME'])

    def shift(name, members, alive=(), ticket=None, result=None, slots=None, slot_roles=None, handover=False):
        d = shifts / name
        d.mkdir()
        write_json(d / 'crew.json', {'crew': 'default', 'slots': slots, 'members': [
            {'label': label, 'role': label.rstrip('0123456789'), 'identity': f'{name}-{label.lower()}',
             'human_contact': label == 'Foreman', 'tool': 'claude'} for label in members]})
        write_json(d / 'launch.json', {'hwnd': INVALID_HWND})
        (d / 'prompt.txt').write_text(f'Task for {name} café\nmore', encoding='utf-8')
        for label in members:
            live = label in alive
            write_json(d / f'{label}.json', {'pid': pid if live else DEAD_PID, 'started': started if live else 1000.0,
                                             'name': f'{name}-{label.lower()}', 'tool': 'claude'})
        if ticket:
            (d / 'ticket.txt').write_text(ticket, encoding='utf-8')
        if result:
            write_json(d / 'result.json', result)
        if handover:
            (d / 'handover.md').write_text('# Handover\n', encoding='utf-8')
        for n in range(1, (slots or 0) + 1):
            role = (slot_roles or {}).get(n)
            write_json(d / 'slots' / f'{n}.json', {'slot': n, 'role': role, 'cmd_pid': pid, 'started': 900.0,
                                                    **({'spawned': 500.0} if role else {})})
        return d

    live = shift('Nightshift-shift-livefix', ['Foreman', 'Coder', 'Tester'], alive=['Foreman', 'Tester'],
                 ticket='NGH-910', slots=4, slot_roles={1: 'Foreman', 2: 'Coder', 3: 'Tester'})
    # A stale claims.lock (old), a fresh bus.lock (kept), claims to normalise, mail to deliver, an idle Foreman.
    (live / 'claims.lock').write_text('', encoding='utf-8')
    os.utime(live / 'claims.lock', (1000, 1000))
    (live / 'bus.lock').write_text('', encoding='utf-8')
    write_json(live / 'claims.json', {r'C:\work\a.py': {'role': 'tester', 'time': 't1'},
                                      r'C:\work\b.py': {'role': 'Coder', 'time': 't2'},
                                      r'C:\work\c.py': {'role': f'{live.name}-tester', 'time': 't3'}})
    make_mailbox_rows(live, [
        dict(kind='assignment', sender='Foreman', sender_incarnation=started, recipient='Tester',
             recipient_incarnation=started, asg='verify-1', body='stored for live Tester', created='2026-10-16 00:00:00',
             state='stored'),
        dict(kind='note', sender='Foreman', sender_incarnation=started, recipient='Tester',
             recipient_incarnation=1.0, asg=None, body='stale incarnation', created='2026-10-16 00:00:01',
             state='stored'),
        dict(kind='note', sender='Tester', sender_incarnation=started, recipient='Coder',
             recipient_incarnation=1000.0, asg=None, body='to dead Coder', created='2026-10-16 00:00:02',
             state='stored'),
    ])
    write_json(home / '.claude' / 'sessions' / f'{pid}.json', {'status': 'idle', 'statusUpdatedAt': 1000})
    shift('Nightshift-shift-donefix', ['Foreman', 'Coder'], ticket='NGH-905',
          result={'status': 'done', 'stage': 'tested'})
    shift('Nightshift-shift-blockedfix', ['Foreman', 'Coder'], ticket='NGH-906',
          result={'status': 'blocked'}, handover=True)
    shift('Nightshift-shift-diedfix', ['Foreman', 'Coder'], ticket='NGH-907')
    shift('Nightshift-shift-otherproj', ['Foreman', 'Coder'], ticket='ABC-1', result={'status': 'halted'})
    # resume: an older shift of NGH-905 that did not finish (the newer done one wins), an unparseable result,
    # a ticket.txt holding only whitespace, and a ticket whose number sorts numerically (NGH-10 after NGH-9).
    shift('Nightshift-shift-olderfix', ['Foreman', 'Coder'], ticket='NGH-905', result={'status': 'blocked'})
    bad = shift('Nightshift-shift-invalidfix', ['Foreman', 'Coder'], ticket='NGH-908')
    (bad / 'result.json').write_text('{not json', encoding='utf-8')
    shift('Nightshift-shift-blankticket', ['Foreman', 'Coder'], ticket='  \n')
    shift('Nightshift-shift-ten', ['Foreman', 'Coder'], ticket='NGH-10')
    shift('Nightshift-shift-nine', ['Foreman', 'Coder'], ticket='NGH-9', handover=True)
    write_json(root / 'locks' / 'c__landing.json', {'kind': 'landing', 'shift': 'Nightshift-shift-diedfix',
                                                    'cwd': r'C:\landing', 'pid': pid, 'started_epoch': started})
    write_json(root / 'locks' / 'c__landing_dead.json', {'kind': 'landing', 'shift': 'Nightshift-shift-diedfix',
                                                         'cwd': r'C:\landed', 'pid': DEAD_PID})
    (root / 'locks' / 'c__unparseable.json').write_text('{broken', encoding='utf-8')
    write_json(root / 'locks' / 'c__no_shift.json', {'shift': 'Nightshift-shift-vanished', 'cwd': r'C:\gone'})
    write_json(root / 'locks' / 'c__dead_folder.json', {'shift': 'Nightshift-shift-diedfix', 'cwd': r'C:\dead folder'})
    write_json(root / 'locks' / 'c__live_folder.json', {'shift': live.name, 'cwd': r'C:\live folder',
                                                        'state_dir': str(live)})
    write_json(root / 'floor.json', {'queue': ['NGH-911', {'ticket': 'NGH-912', 'crew': 'codex', 'repo': r'C:\repo'}],
                                     'reaped': [{'shift': 'Nightshift-shift-otherproj', 'task': 'old',
                                                 'result': 'halted', 'reaped_at': '2026-01-01 00:00:00'}]})
    for i, d in enumerate(sorted(shifts.iterdir())):
        os.utime(d, (1700000000 + i, 1700000000 + i))
        if (d / 'ticket.txt').exists():
            os.utime(d / 'ticket.txt', (1700000000 + i, 1700000000 + i))
    os.utime(shifts / 'Nightshift-shift-olderfix' / 'ticket.txt', (1600000000, 1600000000))
    env['GOLDEN_VOLATILE'] = json.dumps({'SLEEPER_PID': pid, 'SLEEPER_STARTED': started})
    return env


FLOOR_CASES = {
    'warden-all': ['warden', '--dry-run'],
    'warden-one-live': ['warden', 'livefix', '--dry-run'],
    'warden-one-dead': ['warden', 'diedfix', '--dry-run'],
    'warden-missing': ['warden', 'nosuch', '--dry-run'],
    'warden-extra-arg': ['warden', 'a', 'b', '--dry-run'],
    'cleaner': ['cleaner', '--dry-run'],
    'gaffer-queue-string': ['gaffer', '--dry-run'],
    'gaffer-at-capacity': ['gaffer', '--dry-run', '--max-shifts', '1'],
    'gaffer-already-running': ['gaffer', '--dry-run', '--ticket', 'NGH-910'],
    'gaffer-ticket-crew-repo': ['gaffer', '--dry-run', '--ticket', 'NGH-913', '--crew', 'codex', '--repo', r'C:\r p'],
    'gaffer-tick': ['gaffer', '--dry-run', '--tick'],
    'gaffer-tick-at-capacity': ['gaffer', '--dry-run', '--tick', '--max-shifts', '1'],
    'gaffer-max-zero': ['gaffer', '--dry-run', '--max-shifts', '0'],
    'gaffer-max-not-int': ['gaffer', '--dry-run', '--max-shifts', 'x'],
    'resume-all': ['resume', '--dry-run'],
    'resume-project': ['resume', '--dry-run', '--project', 'NGH'],
    'resume-other-project': ['resume', '--dry-run', '--project', 'ABC'],
}


@pytest.mark.parametrize('name', FLOOR_CASES)
def test_floor_dry_runs_match_oracle(floor_world, name):
    parity(FLOOR_CASES[name], floor_world)


@pytest.mark.parametrize('floor', [None, {'queue': []}, {'queue': [{'ticket': 'NGH-912', 'crew': 'codex',
                                                                     'repo': r'C:\repo'}]}, ['not', 'an', 'object']])
def test_gaffer_floor_variants_match_oracle(floor_world, floor):
    path = Path(floor_world['NIGHTSHIFT_STATE_ROOT']) / 'floor.json'
    if floor is None:
        path.unlink()
    else:
        write_json(path, floor)
    parity(['gaffer', '--dry-run'], floor_world)


def test_floor_oracle_cases_reach_the_intended_branch(floor_world):
    def report(args):
        out = run(args, floor_world, native=True)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    patrol = report(['warden', '--dry-run'])['shifts']
    assert [p['shift'] for p in patrol] == ['Nightshift-shift-livefix']
    assert patrol[0]['delivered'] == [1] and patrol[0]['nudged'] == ['Foreman']
    assert patrol[0]['locks_released'] and patrol[0]['claims_normalized']
    assert patrol[0]['recovered'] == [{'slot': 2, 'role': 'Coder', 'recovered': False, 'would_recover': True}]
    assert report(['gaffer', '--dry-run'])['reason'] == 'would_dispatch'
    assert report(['gaffer', '--dry-run', '--max-shifts', '1'])['reason'] == 'at_capacity'
    assert report(['gaffer', '--dry-run', '--ticket', 'NGH-910'])['reason'] == 'already_running'
    reaped = {r['shift'] for r in report(['cleaner', '--dry-run'])['reaped']}
    assert {'Nightshift-shift-donefix', 'Nightshift-shift-diedfix'} <= reaped
    plan = report(['resume', '--dry-run'])
    assert {r['reason'] for r in plan['skipped']} == {'done', 'running'}
    assert [r['ticket'] for r in plan['resume']] == ['ABC-1', 'NGH-9', 'NGH-10', 'NGH-906', 'NGH-907', 'NGH-908']
    assert next(r for r in plan['resume'] if r['ticket'] == 'NGH-908')['last_result'] == 'invalid'
    stale = {Path(s['lock']).name for s in plan['stale_locks']}
    assert {'c__dead_folder.json', 'c__landing_dead.json', 'c__no_shift.json'} <= stale
    assert not {'c__landing.json', 'c__unparseable.json', 'c__live_folder.json'} & stale


def test_warden_delivery_attempt_keys_match_python_float_repr(tmp_path, fake_bin, sleeper):
    """Non-dry-run warden, made safe: no dead slot (so no recover/spawn), no idle session (so no nudge), and the
    fake agents-cli first on PATH so every send fails. The attempt file must key on id:<Python float repr of the
    recipient's current incarnation>, and only rows for the current incarnation are attempted."""
    pid, created = sleeper
    incarnations = {'Tester': created, 'Coder': created + 1 / 3}   # 'x.0' and a long shortest-repr float
    base = tmp_path / 'delivery space'

    def observe(native):
        if base.exists():
            shutil.rmtree(base)
        env = base_env(base, fake_bin)
        env['FAKE_RECORD_DIR'] = str(base)
        d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-deliverfix'
        members = ['Foreman', 'Coder', 'Tester']
        write_json(d / 'crew.json', {'crew': 'default', 'members': [
            {'label': m, 'role': m, 'identity': f'{d.name}-{m.lower()}', 'human_contact': m == 'Foreman',
             'tool': 'claude'} for m in members]})
        write_json(d / 'launch.json', {'hwnd': INVALID_HWND})
        for m in members:
            write_json(d / f'{m}.json', {'pid': pid, 'started': incarnations.get(m, created), 'tool': 'claude'})
        rows = [dict(kind='note', sender='Foreman', sender_incarnation=created, recipient=r,
                     recipient_incarnation=inc, asg=None, body=f'to {r} at {inc!r}',
                     created='2026-10-16 00:00:00', state='stored')
                for r, inc in [('Tester', incarnations['Tester']), ('Coder', incarnations['Coder']),
                               ('Coder', created - 50.0)]]
        make_mailbox_rows(d, rows)
        proc = run(['warden', 'deliverfix'], env, native)
        if native and unported(proc):
            pytest.skip('native binary has not ported warden yet')
        attempts = json.loads((d / 'warden-delivery.json').read_text(encoding='utf-8'))
        assert all(isinstance(v.pop('at'), float) and v.pop('pid') == pid for v in attempts.values())
        conn = sqlite3.connect(d / 'mailbox.sqlite3')
        try:
            states = conn.execute('SELECT id, state FROM messages ORDER BY id').fetchall()
        finally:
            conn.close()
        calls = [c['argv'][0] for c in map(json.loads, (base / 'calls.jsonl').read_text().splitlines())]
        return (proc.returncode, normalise(proc.stdout), proc.stderr, attempts, states,
                calls.count('send'))
    observed = [frozen(lambda: observe(False), tmp_path,
                       {'SLEEPER_PID': pid, 'SLEEPER_STARTED': created, 'CODER_INCARNATION': incarnations['Coder'],
                        'STALE_INCARNATION': created - 50.0})]
    observed.append(observe(True))
    assert observed[0][3] == {f'1:{incarnations["Tester"]!r}': {}, f'2:{incarnations["Coder"]!r}': {}}
    assert observed[0][4] == [(1, 'stored'), (2, 'stored'), (3, 'stored')], 'failed sends must not mark sent'
    assert observed[0][5] == 2, 'exactly the two current-incarnation rows are attempted'
    assert observed[1] == observed[0]


# ---------------------------------------------------------------- hidden --slot entry point


def test_hidden_slot_entry_point_records_parent_shell(tmp_path):
    """`--slot N --state-dir D` (what an empty container cell runs) writes the same slot record."""
    def observe(native):
        base = tmp_path / ('native' if native else 'oracle')
        env = base_env(base, None)
        d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-slotfix'
        write_json(d / 'slots' / '3.json', {'slot': 3, 'role': 'Coder', 'identity': 'x-coder', 'spawned': 5.0,
                                            'cmd_pid': 1, 'junk': True})
        proc = run(['--slot', '3', '--state-dir', str(d)], env, native)
        if native and unported(proc):
            pytest.skip('native binary has not ported the hidden --slot entry point yet')
        record = (d / 'slots' / '3.json').read_bytes()
        record = re.sub(rb'"started": [0-9.e+]+', b'"started": <wall-clock>', record)
        return (proc.stdout, proc.stderr, proc.returncode, record,
                sorted(p.name for p in (d / 'slots').iterdir()))
    observed = [frozen(lambda: observe(False), tmp_path, {'PYTEST_PID': os.getpid()})]
    observed.append(observe(True))
    assert json.loads(re.sub(rb'<wall-clock>', b'0', observed[0][3]))['cmd_pid'] == os.getpid()
    assert observed[1] == observed[0]


# ---------------------------------------------------------------- consult / status / down for a reserved Advisor


TS = re.compile(rb'(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?)')
LIVE_FLOOR = Path('C:/nightshift/floor')


def stable_ts(data):
    """Wall-clock stamps written in the last three minutes, any of the oracle's formats."""
    def stamp(match):
        text = match[1].decode().replace('T', ' ')
        try:
            value = time.mktime(time.strptime(text, '%Y-%m-%d %H:%M:%S' if text.count(':') == 2 else '%Y-%m-%d %H:%M'))
        except ValueError:
            return match[0]
        return b'<wall-clock>' if abs(time.time() - value) < 180 else match[0]
    return TS.sub(stamp, data)


def state_files(env):
    root = Path(env['NIGHTSHIFT_STATE_ROOT'])
    return {str(p.relative_to(root)): stable_ts(p.read_bytes()) for p in sorted(root.rglob('*'))
            if p.is_file() and not p.name.endswith(('.lock', '.tmp'))}


def live_floor_snapshot():
    bus = LIVE_FLOOR / 'bus.jsonl'
    cursors = LIVE_FLOOR / 'cursors'
    return (bus.stat().st_size if bus.exists() else 0,
            {p.name: p.stat().st_mtime_ns for p in cursors.iterdir()} if cursors.exists() else {})


def assert_live_floor_untouched(before, env):
    """Live crews say and hear on the live floor at any time, so only this fixture's own writes count: no new
    live bus line carries a message the fixture floor received, and no fixture reader's cursor appeared or moved."""
    floor = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'floor'
    size, cursors = before
    fixture_bus = floor / 'bus.jsonl'
    said = set()
    if fixture_bus.exists():
        for line in fixture_bus.read_bytes().splitlines():
            row = json.loads(line)
            said.add((row.get('from'), row.get('to'), row.get('text')))
    live_bus = LIVE_FLOOR / 'bus.jsonl'
    if said and live_bus.exists():
        data = live_bus.read_bytes()
        for line in (data[size:] if len(data) >= size else data).splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            assert (row.get('from'), row.get('to'), row.get('text')) not in said, \
                f'a fixture floor message reached the LIVE floor bus: {line!r}'
    live_now = live_floor_snapshot()[1]
    for p in (floor / 'cursors').glob('*') if (floor / 'cursors').exists() else ():
        assert live_now.get(p.name) == cursors.get(p.name), f'a fixture reader cursor moved on the LIVE floor: {p.name}'


def test_live_floor_guard_ignores_live_traffic_but_catches_fixture_writes(tmp_path, monkeypatch):
    """Guard the guard on a stand-in live floor: other crews' lines and cursors pass, the fixture's do not."""
    live = tmp_path / 'live floor'
    (live / 'cursors').mkdir(parents=True)
    (live / 'bus.jsonl').write_bytes(b'{"from": "launcher", "to": "all", "text": "old", "id": 1}\n')
    write_json(live / 'cursors' / 'other.json', {'id': 1})
    monkeypatch.setattr(sys.modules[__name__], 'LIVE_FLOOR', live)
    env = base_env(tmp_path / 'fixture', None)
    fixture = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'floor'
    (fixture / 'cursors').mkdir(parents=True)
    (fixture / 'bus.jsonl').write_bytes(b'{"ts": "x", "from": "Boss", "to": "QA", "text": "hello", "id": 1}\n')
    write_json(fixture / 'cursors' / 'qa.json', {'id': 1})
    before = live_floor_snapshot()
    with (live / 'bus.jsonl').open('ab') as f:
        f.write(b'{"from": "launcher", "to": "all", "text": "started shift", "id": 2}\n')
    write_json(live / 'cursors' / 'other.json', {'id': 2})
    assert_live_floor_untouched(before, env)
    with (live / 'bus.jsonl').open('ab') as f:
        f.write(b'{"ts": "y", "from": "Boss", "to": "QA", "text": "hello", "id": 3}\n')
    with pytest.raises(AssertionError, match='reached the LIVE floor bus'):
        assert_live_floor_untouched(before, env)
    before = live_floor_snapshot()
    write_json(live / 'cursors' / 'qa.json', {'id': 3})
    with pytest.raises(AssertionError, match='cursor moved'):
        assert_live_floor_untouched(before, env)


def twin(tmp_path, build, steps, stdin=None):
    """Each side in a freshly built identical world at the same path: CLI bytes and state files must match."""
    base = tmp_path / 'twin space'

    def observe(native):
        live_before = live_floor_snapshot()
        if base.exists():
            assert base.resolve().is_relative_to(tmp_path.resolve())
            shutil.rmtree(base, onexc=lambda fn, p, _: (os.chmod(p, 0o700), fn(p)))
        env = build(base)
        outputs = []
        for args in steps:
            proc = run(args, env, native, stdin=stdin)
            if native and unported(proc):
                pytest.skip(f'native binary has not ported {args[0]} yet')
            outputs.append((stable_ts(normalise(proc.stdout)), stable_ts(proc.stderr), proc.returncode))
        assert_live_floor_untouched(live_before, env)
        return outputs, state_files(env)
    observed = [frozen(lambda: observe(False), tmp_path, {'PYTEST_PID': os.getpid()})]
    observed.append(observe(True))
    for i, (actual, expected) in enumerate(zip(observed[1][0], observed[0][0])):
        for label, a, e in zip(('stdout', 'stderr', 'exit'), actual, expected):
            if label == 'exit':
                assert a == e, (steps[i], observed[1][0][i])
            else:
                assert_equal_bytes(a, e, f'{steps[i]} {label}')
    assert observed[1][1].keys() == observed[0][1].keys()
    for name, data in observed[0][1].items():
        assert_equal_bytes(observed[1][1][name], data, f'state file {name}')
    return observed[0]


def advisor_world(fake_bin, variant):
    """A reserved-Advisor shift whose slot shells are dead and whose window is gone: consult can never type."""
    def build(base):
        env = base_env(base, fake_bin)
        d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-advisorfix'
        labels = [('Foreman', 'claude-opus-5-5'), ('Advisor', 'claude-fable-5-1'), ('Coder', 'claude-opus-5-5')]
        members = [{'role': label, 'label': label, 'identity': f'{d.name}-{label.lower()}',
                    'brief': f'{label.lower()}.md', 'persona': f'{label.lower()}.yaml',
                    'colour': {'Foreman': 'red', 'Advisor': 'purple'}.get(label, 'green'),
                    'human_contact': label == 'Foreman', 'tool': 'claude', 'model': model}
                   for label, model in labels]
        for m in members:
            m['peer'] = (', '.join(o['identity'] for o in members if o is not m) if m['human_contact']
                         else members[0]['identity'])
        crew = {'crew': 'default', 'members': members, 'slots': 4, 'permission_mode': 'auto',
                'model': 'claude-opus-5-5', 'cwd': str(base), 'advisor_slot': 2}
        if variant == 'unreserved':
            crew.pop('advisor_slot')
        if variant == 'starting':
            crew['consult'] = {'question': 'earlier question', 'asg': 'adv-0'}
        write_json(d / 'crew.json', crew)
        write_json(d / 'launch.json', {'hwnd': INVALID_HWND})
        (d / 'prompt.txt').write_text('Advisor fixture café\nline two', encoding='utf-8')
        for label in ('Foreman', 'Coder') + (('Advisor',) if variant == 'started' else ()):
            write_json(d / f'{label}.json', {'pid': DEAD_PID, 'started': 1000.0, 'name': f'{d.name}-{label.lower()}',
                                             'cwd': str(base), 'tool': 'claude'})
        for n in range(1, 5):
            write_json(d / 'slots' / f'{n}.json', {'slot': n, 'role': {1: 'Foreman', 3: 'Coder'}.get(n),
                                                    'cmd_pid': DEAD_PID, 'started': 900.0})
        return env
    return build


CONSULT_CASES = {
    'help': ('reserved', [['consult', '-h']]),
    'help-long': ('reserved', [['consult', '--help']]),
    'missing-asg': ('reserved', [['consult', 'advisorfix', 'Why?']]),
    'missing-question': ('reserved', [['consult', 'advisorfix', '--asg', 'a']]),
    'empty-question': ('reserved', [['consult', 'advisorfix', '   ', '--asg', 'a']]),
    'missing-shift': ('reserved', [['consult', 'nosuch', 'Why?', '--asg', 'a']]),
    'no-reserved-advisor': ('unreserved', [['consult', 'advisorfix', 'Why?', '--asg', 'a']]),
    'already-started': ('started', [['consult', 'advisorfix', 'Why?', '--asg', 'a']]),
    'already-starting': ('starting', [['consult', 'advisorfix', 'Why?', '--asg', 'a']]),
    # The one path that reaches spawn: the window is gone, so spawn refuses and consult restores crew.json.
    'window-gone-rolls-back': ('reserved', [['consult', 'advisorfix', 'Why café?', '--asg', 'adv-1']]),
    'status-reserved-advisor': ('reserved', [['status', '--all']]),
    'down-hold-reserved-advisor': ('reserved', [['down', 'advisorfix', '--hold']]),
}


@pytest.mark.parametrize('name', CONSULT_CASES)
def test_consult_status_down_for_reserved_advisor_match_oracle(tmp_path, fake_bin, name):
    variant, steps = CONSULT_CASES[name]
    outputs, files = twin(tmp_path, advisor_world(fake_bin, variant), steps)
    if name == 'window-gone-rolls-back':
        assert outputs[0][2] == 1 and b'window is gone' in outputs[0][1]
        assert b'"consult"' not in files['shifts/Nightshift-shift-advisorfix/crew.json'.replace('/', os.sep)]
    if name == 'status-reserved-advisor':
        roles = json.loads(outputs[0][0])[0]['roles']
        assert roles['Advisor'] == {'status': 'not_started', 'reserved_slot': 2, 'alive': False}
    if name == 'down-hold-reserved-advisor':
        handover = files['shifts/Nightshift-shift-advisorfix/handover.md'.replace('/', os.sep)]
        assert b'## Advisor (not_started)' in handover


# ---------------------------------------------------------------- floor bus and agy status line


def floor_bus_world(base):
    return base_env(base, None)


FLOOR_BUS_CASES = {
    'say-hear-cursor': [['floor', 'say', 'Boss', 'hello', 'café 🚀', '--to', 'QA'], ['floor', 'say', 'Other', 'to all'],
                        ['floor', 'hear', 'QA'], ['floor', 'hear', 'QA'], ['floor', 'hear', 'QA', '--since', '0'],
                        ['floor', 'hear', 'Nobody'], ['floor', 'hear', 'QA', '--since', '1']],
    'hear-empty-bus': [['floor', 'hear', 'QA']],
    'help': [['floor', '-h'], ['floor', 'say', '-h'], ['floor', 'hear', '--help']],
    'errors': [['floor'], ['floor', 'bogus'], ['floor', 'say', 'Boss'], ['floor', 'hear'],
               ['floor', 'hear', 'QA', '--since', '-1'], ['floor', 'hear', 'QA', '--since', 'x']],
}


@pytest.mark.parametrize('name', FLOOR_BUS_CASES)
def test_floor_bus_matches_oracle_in_disposable_root(tmp_path, name):
    outputs, files = twin(tmp_path, floor_bus_world, FLOOR_BUS_CASES[name])
    if name == 'say-hear-cursor':
        assert [len(o[0].splitlines()) for o in outputs[2:]] == [2, 0, 2, 1, 1]
        assert any(n.startswith('floor' + os.sep + 'cursors') for n in files)


@pytest.mark.parametrize('env_extra,stdin', [
    ({'NIGHTSHIFT_ROLE': 'Coder', 'NIGHTSHIFT_IDENTITY': 'shift-coder', 'NIGHTSHIFT_ROLE_COLOUR': 'green'}, b'{"a":1}'),
    ({'NIGHTSHIFT_ROLE': 'Advisor', 'NIGHTSHIFT_IDENTITY': 'shift-advisor é', 'NIGHTSHIFT_ROLE_COLOUR': 'purple'}, b''),
    ({'NIGHTSHIFT_ROLE': 'Odd', 'NIGHTSHIFT_IDENTITY': 'x', 'NIGHTSHIFT_ROLE_COLOUR': 'chartreuse'}, b'junk'),
    ({'NIGHTSHIFT_ROLE': 'Coder'}, b'{}'),
    ({'NIGHTSHIFT_IDENTITY': 'only-identity'}, b'{}'),
    ({}, b'{"model": "x"}'),
])
def test_agy_statusline_matches_oracle(tmp_path, env_extra, stdin):
    env = {**{k: v for k, v in base_env(tmp_path / 'line', None).items()
              if k not in ('NIGHTSHIFT_ROLE', 'NIGHTSHIFT_IDENTITY', 'NIGHTSHIFT_ROLE_COLOUR')}, **env_extra}
    expected = frozen(lambda: run(['statusline'], env, native=False, stdin=stdin), tmp_path)
    actual = run(['statusline'], env, native=True, stdin=stdin)
    if unported(actual):
        pytest.skip('native binary has not ported statusline yet')
    assert (actual.stdout, actual.stderr, actual.returncode) == (expected.stdout, expected.stderr, expected.returncode)
    assert (expected.stdout == b'') is not ('NIGHTSHIFT_ROLE' in env_extra and 'NIGHTSHIFT_IDENTITY' in env_extra)


# ---------------------------------------------------------------- hidden --pane --start (stub tools only)


PANE_MEMBERS = [
    {'role': 'Foreman', 'label': 'Foreman', 'brief': 'foreman.md', 'persona': 'foreman.yaml', 'colour': 'red',
     'human_contact': True, 'tool': 'claude', 'model': 'claude-opus-5-5'},
    {'role': 'Apprentice', 'label': 'Apprentice', 'brief': 'apprentice.md', 'persona': 'apprentice.yaml',
     'colour': 'yellow', 'human_contact': False, 'tool': 'agy', 'model': 'gemini-3.8-flash-high',
     'permissions': 'skip'},
    {'role': 'Coder', 'label': 'Coder', 'brief': 'coder.md', 'persona': 'coder.yaml', 'colour': 'green',
     'human_contact': False, 'tool': 'codex', 'model': 'gpt-6.1-sol', 'reasoning': 'high', 'account': 2,
     'replacement': True},
    {'role': 'Tester', 'label': 'Tester-2', 'brief': 'tester.md', 'persona': 'tester.yaml', 'colour': 'blue',
     'human_contact': False, 'tool': 'copilot', 'model': 'gemini-3.8-flash', 'reasoning': 'high',
     'context': 'long_context', 'max_credits': 30, 'permissions': 'default'},
]


def pane_world(base, fake_bin, sleeper):
    """A shift whose non-Claude members run our recording stub at the exact TOOL_EXES paths."""
    env = base_env(base, fake_bin)
    stubs = base / 'stub appdata'
    local, roaming = stubs / 'Local', stubs / 'Roaming'
    for exe in (local / 'Microsoft' / 'WinGet' / 'Links' / 'agy.exe',
                roaming / 'npm' / 'node_modules' / '@openai' / 'codex' / 'node_modules' / '@openai' /
                'codex-win32-x64' / 'vendor' / 'x86_64-pc-windows-msvc' / 'bin' / 'codex.exe',
                roaming / 'npm' / 'node_modules' / '@github' / 'copilot' / 'node_modules' / '@github' /
                'copilot-win32-x64' / 'copilot.exe'):
        exe.parent.mkdir(parents=True)
        shutil.copyfile(fake_bin[0] / 'tasks-cli.exe', exe)
    stub_bin = base / 'stub bin'
    stub_bin.mkdir(parents=True, exist_ok=True)
    for name in ('agy.exe', 'codex.exe', 'copilot.exe'):
        shutil.copyfile(fake_bin[0] / 'tasks-cli.exe', stub_bin / name)
    record = base / 'record'
    record.mkdir()
    env.update(LOCALAPPDATA=str(local), APPDATA=str(roaming), FAKE_RECORD_DIR=str(record),
               CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='inherited', CLAUDE_AFK_TIMEOUT_MS='9', NO_COLOR='1',
               # Python's Path.home() is USERPROFILE outside the oracle patch; give both the fixture home.
               USERPROFILE=env['NIGHTSHIFT_HOME'])
    env['PATH'] = str(stub_bin) + os.pathsep + env['PATH']
    repo = make_repo(base / 'pane repo', ('main',))
    (repo / '.nightshift').mkdir()
    (repo / '.nightshift' / 'config.yaml').write_text('build:\n  cmd: pytest -q\n', encoding='utf-8')
    (repo / '.nightshift' / 'instructions.md').write_text('Shared rule café {cwd} {shift} {nightshift}\n',
                                                          encoding='utf-8')
    (repo / '.nightshift' / 'coder.md').write_text('Coder-only repository rule.\n', encoding='utf-8')
    d = Path(env['NIGHTSHIFT_STATE_ROOT']) / 'shifts' / 'Nightshift-shift-panefix'
    members = [dict(m, identity=f"{d.name}-{m['label'].lower()}") for m in PANE_MEMBERS]
    lead = members[0]['identity']
    for m in members:
        m['peer'] = ', '.join(o['identity'] for o in members if o is not m) if m is members[0] else lead
    write_json(d / 'crew.json', {'crew': 'default', 'members': members, 'slots': 8, 'permission_mode': 'auto',
                                 'model': 'claude-opus-5-5', 'cwd': str(repo), 'max_credits': 60})
    (d / 'prompt.txt').write_text('The task café', encoding='utf-8')
    pid = sleeper[0]
    for n in range(1, 9):
        write_json(d / 'slots' / f'{n}.json', {'slot': n, 'role': None, 'cmd_pid': pid, 'started': 900.0})
    # An earlier Coder incarnation: the wrapper must carry it into history.
    write_json(d / 'Coder.json', {'pid': DEAD_PID, 'wrapper_pid': DEAD_PID, 'role': 'Coder', 'started': 1000.5,
                                  'name': 'old', 'history': [{'pid': 1, 'started': 10.0}]})
    env['GOLDEN_VOLATILE'] = json.dumps({'SLEEPER_PID': pid})
    return env, d, repo, record


def pane_observation(env, d, record, label, slot):
    info = json.loads((d / f'{label}.json').read_text(encoding='utf-8'))
    assert isinstance(info.pop('pid'), int) and info.pop('wrapper_pid') and isinstance(info.pop('started'), float)
    slot_record = json.loads((d / 'slots' / f'{slot}.json').read_text(encoding='utf-8'))
    assert slot_record.pop('wrapper_pid') and isinstance(slot_record.pop('started'), float)
    calls = [json.loads(line) for line in (record / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
    # Trust-dialog peeks and Codex rename attempts race the stub's exit; the launch and the agents-cli name are not.
    calls = [c for c in calls if c['exe'] != 'agents-cli' or c['argv'][:1] == ['name']]
    for c in calls:
        if c['exe'] == 'agents-cli':
            assert c['argv'][1].isdigit()
            c['argv'][1] = '<child pid>'
            c['env'] = {k: v for k, v in c['env'].items()
                        if k in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_AFK_COUNTDOWN_MS',
                                 'CLAUDE_AFK_TIMEOUT_MS', 'COLORTERM', 'DISABLE_AUTOUPDATER',
                                 'FORCE_COLOR', 'NO_COLOR', 'TERM')}
    return info, slot_record, calls


@pytest.mark.parametrize('label,slot', [('Apprentice', 5), ('Coder', 3), ('Tester-2', 4)])
def test_hidden_pane_start_runs_same_stub_argv_and_records(tmp_path, fake_bin, sleeper, python_launcher,
                                                           label, slot):
    base = tmp_path / 'pane space'   # the same paths for both runs, so nothing path-shaped needs normalising

    def observe(native):
        if base.exists():
            assert base.resolve().is_relative_to(tmp_path.resolve())
            # Git writes its objects read-only; clear the bit and retry, inside tmp_path only.
            shutil.rmtree(base, onexc=lambda fn, p, _: (os.chmod(p, 0o700), fn(p)))
        env, d, repo, record = pane_world(base, fake_bin, sleeper)
        args = ['--pane', label, '--state-dir', str(d), '--start', '--cwd', str(repo), '--slot', str(slot)]
        proc = run(args, env, native, timeout=120)
        if native and unported(proc):
            pytest.skip('native binary has not ported --pane yet')
        assert proc.returncode == 0, proc.stderr
        info, slot_record, calls = pane_observation(env, d, record, label, slot)
        text = json.dumps([proc.stdout.decode(), proc.stderr.decode(), info, slot_record, calls],
                          ensure_ascii=False, indent=1)
        return text, info, slot_record, calls

    def oracle_side():
        text, info, slot_record, calls = observe(False)
        return normalise(text.encode(), python_launcher, False), info, slot_record, calls

    def native_side():
        # NGH-062 re-record: the rendered brief in the stub argv is the renderer's, no longer the oracle's.
        text, info, slot_record, calls = observe(True)
        return normalise(text.encode(), python_launcher, True), info, slot_record, calls
    observed = [frozen(native_side if REBASE else oracle_side, tmp_path, {'SLEEPER_PID': sleeper[0]}, record=REBASE)]
    if not observed[0][3]:
        pytest.fail('oracle did not run the stub; refusing to run the native pane (could reach a real CLI)')
    observed.append(observe(True))
    assert [c['exe'] for c in observed[0][3]][:1] in (['agy'], ['codex'], ['copilot'])
    assert observed[0][1]['tool'] == observed[0][3][0]['exe']
    if label == 'Coder':
        assert observed[0][1]['history'][-1]['started'] == 1000.5
        assert observed[0][3][0]['env'].get('CODEX_HOME', '').endswith('.codex-second')
    assert 'CLAUDECODE' not in observed[0][3][0]['env'] and observed[0][3][0]['env']['CLAUDE_AFK_TIMEOUT_MS'] == '9'
    assert_equal_bytes(normalise(observed[1][0].encode(), python_launcher, True), observed[0][0], f'--pane {label}')


def test_hidden_pane_start_rejects_label_outside_crew(tmp_path, fake_bin, sleeper):
    env, d, repo, record = pane_world(tmp_path / 'w', fake_bin, sleeper)
    parity(['--pane', 'Stranger', '--state-dir', str(d), '--start', '--cwd', str(repo), '--slot', '6'], env)
    assert not (record / 'calls.jsonl').exists()


# ---------------------------------------------------------------- help


HELP_CASES = {**{f'{cmd}': [cmd, '-h'] for cmd in oracle.COMMANDS},
              **{f'{cmd}-long': [cmd, '--help'] for cmd in oracle.COMMANDS},
              'top-h': ['-h'], 'top-help': ['--help'],
              'msg-send': ['msg', 'send', '-h'], 'msg-sent': ['msg', 'sent', '-h'],
              'msg-no-sub': ['msg'], 'msg-bad-sub': ['msg', 'bogus'],
              'top-unknown-flag': ['--bogus']}


@pytest.mark.parametrize('name', HELP_CASES)
def test_help_matches_oracle(tmp_path, name):
    env = base_env(tmp_path / 'help space', None)
    parity(HELP_CASES[name], env)


@pytest.mark.xfail(strict=True, reason='Foreman msg 37: COLUMNS wrapping is not contract; Go renders the '
                                       '80-column argparse help (accepted difference in docs)')
@pytest.mark.parametrize('args', [['start', '-h'], ['-h']])
def test_help_wraps_to_columns_like_argparse(tmp_path, args):
    env = {**base_env(tmp_path / 'help space', None), 'COLUMNS': '120'}
    parity(args, env)

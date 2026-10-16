"""Nightshift launcher: one Windows Terminal tab, Foreman and Coder side by side.

`start "task"` (via C:/scripts/nightshift.cmd) persists the task to
<state_dir>/prompt.txt and starts interactive Claude Opus 5.5 Foreman and Coder
in the "Nightshift Crew" Terminal profile (Cascadia Mono 12, dark). Only Foreman receives the
task; Coder gets its role and peer identity, sends READY and awaits work. The task
reaches the panes by file path only, never as argv text. Panes run in the
caller's current directory (no worktree is created) in Claude's auto permission
mode (--permission-mode auto). Each `start` is a new shift: mesh names are
<shift>-foreman / <shift>-coder, pane labels are just Foreman / Coder, and
<state_dir>/<Role>.json records pid, name, peer and model. There is no restart or
resume; a closed pane is not relaunched. `start --dry-run` prints the plan as
JSON without launching or writing anything. A working folder holds one live shift at a time:
start takes a lock (C:/Data/nightshift/locks), refuses with exit code 3 while a live shift holds it,
and `down` releases it.
Coders claim files before editing them (`claim`/`release`/`claims`, kept in <state_dir>/claims.json); a file
held by another role makes `claim` exit 4 and change nothing.
Crew messages go through a durable mailbox (<state_dir>/mailbox.sqlite3): `msg send`/`msg sent`, `inbox`, `ack` (exit 5 = protocol violation).
`resume` reruns, through `loop`, every ticket whose latest shift never reached done, continuing from that shift.
Result ladder: result.json carries a stage (implemented < tested < deployed < validated; none = implemented).
A ticket line `requires: <rung>` sets the bar (default tested); a done below it is recorded as 'short' and stops the loop.
The crew comes from a YAML manifest: crews/default.yaml unless `start --crew NAME` picks
crews/NAME.yaml (roles, brief files, colours, counts, model, permission mode; 2-8 panes,
one row up to 4, two rows above). A role with count > 1 gets labels Role-1..N and names <shift>-role-N.
Legacy modes: no args = placeholder layout; --live = ping/pong communication trial.
WT separators are native argv, not shell chaining. No software installs.
"""
import argparse
import contextlib
import ctypes
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import yaml

MODEL = 'claude-opus-5-5'
PROFILE = 'Nightshift Crew'
SHIFTS_DIR = Path('C:/Data/nightshift/shifts')
LOCKS_DIR = Path('C:/Data/nightshift/locks')
TRIALS_DIR = Path('C:/Data/nightshift/layout-trials')
SUITE_PYTHON = 'C:/miniconda3/envs/python312/python.exe'
REPO_DIR = Path(__file__).resolve().parent                 # C:/git/nightshift-shifts
SUITE_TESTS = str(REPO_DIR / 'test_nightshift_shifts.py')  # run by `down` for the handover
BRIEFS_DIR = REPO_DIR / 'briefs'                           # common.md, foreman.md, coder.md
CREWS_DIR = REPO_DIR / 'crews'                             # <crew>.yaml manifests; default.yaml
START_PERMISSION_MODE = 'auto'  # owner decision 2026-10-15: auto (classifier-guarded), not manual or bypass
START_MESSAGES = {'Foreman': 'Begin the assignment: the task text is at the end of your role instructions, after '
                             '"The human task for this shift follows, verbatim:".',
                  'Coder': 'Begin your Coder role: send READY to Foreman and await assignments.'}
MAILKEEP = 'C:/Tools/mailkeep/Mailkeep.Cli.exe'
NOTIFY_IDENTITY = 'owner_id'                # Mailkeep identity for owner@example.com
NOTIFY_TO = 'owner@example.com'        # `loop` emails the owner here when it halts or finishes
DEFAULT_LABELS = ('Foreman', 'Coder')   # panes of the trial modes and of shifts started before crew.json existed
STAGES = ('implemented', 'tested', 'deployed', 'validated')   # the result ladder, lowest rung first
DEFAULT_REQUIRES = 'tested'


def stage_rank(stage):
    """Position of stage on the ladder; unknown or missing counts as the bottom rung (0)."""
    return STAGES.index(stage) if stage in STAGES else 0


def result_stage(result):
    """The rung a result claims; a result without a valid stage counts as implemented (pre-ladder results)."""
    stage = result.get('stage') if isinstance(result, dict) else None
    return stage if stage in STAGES else 'implemented'


def meets_rung(stage, required):
    return stage_rank(stage) >= stage_rank(required)


def parse_requires(text):
    """The rung a task asks for with a 'requires: <rung>' line; DEFAULT_REQUIRES when absent or not a rung."""
    match = re.search(r'^requires:\s*(\w+)\s*$', text or '', re.MULTILINE | re.IGNORECASE)
    value = match.group(1).lower() if match else None
    return value if value in STAGES else DEFAULT_REQUIRES


def shift_requires(state_dir):
    """The rung recorded for a shift in <state_dir>/requires.txt; DEFAULT_REQUIRES when missing or unknown."""
    try:
        value = (Path(state_dir) / 'requires.txt').read_text(encoding='utf-8').strip().lower()
    except OSError:
        return DEFAULT_REQUIRES
    return value if value in STAGES else DEFAULT_REQUIRES


def load_crew(name='default'):
    """Read and check CREWS_DIR/<name>.yaml; SystemExit with a plain reason when it is unusable."""
    path = CREWS_DIR / f'{name}.yaml'
    if not path.is_file():
        available = sorted(p.stem for p in CREWS_DIR.glob('*.yaml')) if CREWS_DIR.is_dir() else []
        raise SystemExit(f'no crew manifest {path}; available crews: {", ".join(available) or "(none)"}')
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except yaml.YAMLError as err:
        raise SystemExit(f'crew {name}: {path} is not valid YAML: {err}')
    if not isinstance(data, dict):
        raise SystemExit(f'crew {name}: {path} must be a mapping with a roles: list')
    roles = data.get('roles')
    if not isinstance(roles, list) or not roles:
        raise SystemExit(f'crew {name}: roles must be a non-empty list')
    checked = []
    for i, entry in enumerate(roles, 1):
        if not isinstance(entry, dict) or not entry.get('role') or not entry.get('brief'):
            raise SystemExit(f'crew {name}: role {i} needs both role: and brief:')
        role, brief = str(entry['role']), str(entry['brief'])
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', role):
            raise SystemExit(f'crew {name}: role name {role!r} must be letters, digits or _ (it names files and panes)')
        if role in (r['role'] for r in checked):
            raise SystemExit(f'crew {name}: role {role} is listed twice; use count: instead')
        if not (BRIEFS_DIR / brief).is_file():
            raise SystemExit(f'crew {name}: brief {brief} for {role} is not in {BRIEFS_DIR}')
        count = entry.get('count', 1)
        if type(count) is not int or count < 1:
            raise SystemExit(f'crew {name}: count for {role} must be a whole number >= 1, not {count!r}')
        human = entry.get('human_contact') is True
        checked.append({'role': role, 'brief': brief, 'count': count, 'human_contact': human,
                        'colour': str(entry.get('colour') or ('red' if human else 'green'))})
    humans = [r['role'] for r in checked if r['human_contact']]
    if len(humans) != 1:
        raise SystemExit(f'crew {name}: exactly one role needs human_contact: true, found {len(humans)}')
    if next(r for r in checked if r['human_contact'])['count'] != 1:
        raise SystemExit(f'crew {name}: the human_contact role {humans[0]} must have count 1')
    panes = sum(r['count'] for r in checked)
    if not 2 <= panes <= 8:
        raise SystemExit(f'crew {name}: {panes} panes; a crew has 2 to 8')
    layout = data.get('layout', 'split')
    if layout != 'split':
        raise SystemExit(f"crew {name}: layout {layout!r} is not supported; use 'split'")
    return {'name': name, 'model': str(data.get('model') or MODEL),
            'permission_mode': str(data.get('permission_mode') or START_PERMISSION_MODE),
            'contract': str(data.get('contract') or 'result.json'), 'layout': layout, 'roles': checked}


def crew_members(crew, shift):
    """Panes in launch order: [{role, label, identity, brief, colour, human_contact, peer}].

    A role with count 1 is labelled Role and named <shift>-role; count N gives Role-1..N / <shift>-role-1..N.
    The human-contact member's peer is every other member (comma-separated); everyone else's is it.
    """
    members = []
    for r in crew['roles']:
        for n in range(1, r['count'] + 1):
            suffix = f'-{n}' if r['count'] > 1 else ''
            members.append({'role': r['role'], 'label': r['role'] + suffix,
                            'identity': f"{shift}-{r['role'].lower()}{suffix}", 'brief': r['brief'],
                            'colour': r['colour'], 'human_contact': r['human_contact']})
    lead = next(m for m in members if m['human_contact'])
    for m in members:
        m['peer'] = ', '.join(o['identity'] for o in members if o is not m) if m is lead else lead['identity']
    return members


def opening_message(member):
    """The opening user message for a pane: the task pointer for the human contact, else a READY instruction."""
    if member['human_contact']:
        return START_MESSAGES['Foreman']
    return START_MESSAGES.get(member['role']) or \
        f"Begin your {member['role']} role: send READY to Foreman and await assignments."


def role_brief(role, identity, peer_name, state_dir='', brief=None):
    """Crew instructions: BRIEFS_DIR/common.md + the role's brief file (default <role>.md), with
    {role}/{identity}/{peer_name}/{state_dir}/{shift} filled in ({shift} is the state folder's name, '' without one).

    Edit those files to change how a crew behaves; nothing here needs touching.
    """
    text = ''
    for name in ('common.md', brief or role.lower() + '.md'):
        text += (BRIEFS_DIR / name).read_text(encoding='utf-8').strip() + '\n\n'
    shift = Path(state_dir).name if state_dir else ''
    # Plain replace, not str.format: the briefs contain literal JSON braces.
    for key, value in (('{role}', role), ('{identity}', identity), ('{peer_name}', peer_name),
                       ('{state_dir}', str(state_dir)), ('{shift}', shift)):
        text = text.replace(key, value)
    return text


def crew_env(environ):
    """Environment for a crew member's claude.exe.

    Drops inherited Claude Code markers: when start is run from inside a Claude session the crew
    would otherwise become child sessions of it (name inherited, transcript saving off, no mesh
    registration). CLAUDE_AFK_* are the user's own settings (AFK auto-answer disabled) and stay.
    Colour is forced on because a parent Codex shell can carry NO_COLOR=1 / TERM=dumb (NGH-002).
    """
    env = {k: v for k, v in environ.items()
           if not k.upper().startswith('CLAUDE') or k.upper().startswith('CLAUDE_AFK_')}
    env.pop('NO_COLOR', None)
    env.update(TERM='xterm-256color', COLORTERM='truecolor', FORCE_COLOR='3')
    return env


def accept_trust_prompt(child):
    """Answer Claude Code's "trust this folder?" dialog in this pane, if it appears (owner decision 15 Oct).

    Only acts when the dialog text is actually on screen; otherwise it gives up after ~20s.
    """
    for _ in range(40):
        if child.poll() is not None:
            return
        try:
            peek = subprocess.run(['agents-cli', '_attach', 'peek', str(child.pid), '0'],
                                  capture_output=True, timeout=10)
            screen = (peek.stdout or b'').decode('utf-8', 'replace')
            if 'Yes, I trust this folder' in screen:
                # Press Enter only once the "Yes" line is the highlighted one, else Enter picks "No, exit".
                for _attempt in range(6):
                    if re.search(r'(❯|\\u276f)\s*Yes, I trust this folder', screen):  # peek JSON may escape ❯
                        subprocess.run(['agents-cli', 'key', str(child.pid), 'enter'], capture_output=True, timeout=10)
                        return
                    subprocess.run(['agents-cli', 'key', str(child.pid), 'down'], capture_output=True, timeout=10)
                    time.sleep(1)
                    peek = subprocess.run(['agents-cli', '_attach', 'peek', str(child.pid), '0'],
                                          capture_output=True, timeout=10)
                    screen = (peek.stdout or b'').decode('utf-8', 'replace')
                return
            if 'Claude Code v' in screen:
                return  # welcome banner: already past the dialog
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.5)


def peek_lines(pid):
    """Visible pane lines via agents-cli, [] when unreadable."""
    try:
        peek = subprocess.run(['agents-cli', '_attach', 'peek', str(pid), '0'], capture_output=True, timeout=10)
        return json.loads((peek.stdout or b'{}').decode('utf-8', 'replace')).get('lines', [])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def prompt_is_empty(lines):
    """True when Claude's input box (the last '❯' line) holds no text - the only safe time to inject."""
    prompt_lines = [line for line in lines if line.lstrip().startswith('❯')]
    return bool(prompt_lines) and prompt_lines[-1].strip() == '❯'


def inject_command(pid, command, tries=10):
    """Type a slash command into the pane only when its input box is empty; False if it never is.

    Injecting into a non-empty box appends to whatever the human (or their dictation) was typing
    there - which is how an owner sentence ended up glued to '/color red' on 15 Oct.
    """
    for _ in range(tries):
        if prompt_is_empty(peek_lines(pid)):
            subprocess.run(['agents-cli', '_attach', 'inject', str(pid), '1', command], capture_output=True, timeout=10)
            return True
        time.sleep(1)
    return False


def set_role_colour(child, colour, cwd):
    """Reuse launch.ps1's transcript-ready gate before sending Claude /color <colour> (from the crew manifest)."""
    registry = Path.home() / '.claude' / 'sessions' / f'{child.pid}.json'
    # Claude Code names the project folder after the cwd with ':' and path separators turned into '-'.
    project_dir = Path.home() / '.claude' / 'projects' / re.sub(r'[:\\/]', '-', str(Path(cwd)))
    for _ in range(60):
        if child.poll() is not None:
            return
        try:
            session = json.loads(registry.read_text(encoding='utf-8'))
            transcript = project_dir / (session['sessionId'] + '.jsonl')
            if transcript.exists() and transcript.stat().st_size > 200:
                time.sleep(3)
                # Only the role colour is set; the theme is whatever the owner's global Claude theme is.
                inject_command(child.pid, '/color ' + colour)
                return
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            pass
        time.sleep(0.5)


def pane(role, state_dir, live=False, brief_file=None, start=False, cwd='C:/git/nightshift', crew=None):
    if live or start:
        model, permission_mode = MODEL, START_PERMISSION_MODE
        colour = 'red' if role == 'Foreman' else 'green'
        shift = Path(state_dir).name
        peer = 'Coder' if role == 'Foreman' else 'Foreman'
        identity = f'{shift}-{role.lower()}'
        peer_name = f'{shift}-{peer.lower()}'
        first_message = 'Begin the Nightshift communication exercise described in your role instructions.'
        brief = (
            f'You are {role} in the Nightshift two-agent proof of concept. Your session name is {identity}. '
            f'Your only crew peer is {peer_name}. Foreman is the master and sole human contact. '
            'Remain an interactive session. This first exercise is communication only: do not edit repository files, '
            'run builds, launch additional agents or change settings. Use native ListAgents and SendMessage if available. '
            'Report honestly if either tool is unavailable or peer discovery fails. Do not contact unrelated sessions. '
        )
        if role == 'Foreman':
            brief += (f'Discover the Coder and send it NIGHTSHIFT-PING-{shift}. Ask it to reply with '
                      f'NIGHTSHIFT-PONG-{shift} and its role. Report success only after receiving its reply. '
                      'If the Coder is not registered yet, state that and remain ready for its READY message.')
        else:
            brief += (f'Discover Foreman and send READY from {identity}. When it sends NIGHTSHIFT-PING-{shift}, '
                      f'reply to it with NIGHTSHIFT-PONG-{shift} and your role. Then await assignments from Foreman.')
        if brief_file:
            brief = (f'You are {role} in Nightshift. Your unique session name is {identity}; your only crew peer is '
                     f'{peer_name}. Foreman is master and sole human contact. Use native ListAgents and SendMessage '
                     'to coordinate only with that peer. Remain interactive. Read the following authorized assignment '
                     'and act according to your role. Coder sends READY to Foreman, then waits for its assignment. '
                     'Foreman delegates implementation to Coder, reviews and checks the result, then reports. '
                     'Do not spawn additional agents.\n' + Path(brief_file).read_text(encoding='utf-8'))
        if start:
            # `role` is the pane label (Foreman, Coder-2, ...); everything else comes from the crew manifest.
            crew_def = load_crew(crew or 'default')
            member = next((m for m in crew_members(crew_def, shift) if m['label'] == role), None)
            if member is None:
                raise SystemExit(f"pane {role} is not a member of crew {crew_def['name']}")
            identity, peer_name, colour = member['identity'], member['peer'], member['colour']
            model, permission_mode = crew_def['model'], crew_def['permission_mode']
            first_message = opening_message(member)
            brief = role_brief(member['role'], identity, peer_name, state_dir, member['brief'])
            if member['human_contact']:
                brief += Path(state_dir, 'prompt.txt').read_text(encoding='utf-8')
        exe = Path('C:/nvm4w/nodejs/node_modules/@anthropic-ai/claude-code/bin/claude.exe')
        command = [str(exe), '--name', identity, '--model', model,
                   '--permission-mode', permission_mode, '--append-system-prompt', brief, first_message]
        child = subprocess.Popen(command, cwd=cwd, env=crew_env(os.environ))
        Path(state_dir, role + '.json').write_text(json.dumps({
            'pid': child.pid, 'wrapper_pid': os.getpid(), 'role': role, 'started': time.time(),
            'name': identity, 'peer': peer_name, 'model': model, 'cwd': cwd}), encoding='utf-8')
        accept_trust_prompt(child)
        set_role_colour(child, colour, cwd)
        code = child.wait()
        print(f'{role} exited with code {code}.')
        input('Press Enter to close this pane.')
        return
    kernel = ctypes.windll.kernel32
    handle = kernel.GetStdHandle(-11)
    mode = ctypes.c_ulong()
    kernel.GetConsoleMode(handle, ctypes.byref(mode))
    kernel.SetConsoleMode(handle, mode.value | 4)
    colour = '91' if role == 'Foreman' else '92' if role == 'Coder' else '90'
    print(f'\033[2J\033[H\033[{colour}mNIGHTSHIFT / {role.upper()}\033[0m\n')
    print('Layout trial - no agent running.\n')
    print('Foreman + Coder is the first crew.' if role in ('Foreman', 'Coder') else 'Reserved pane / inactive.')
    print('\nEnter status to show pane dimensions.\nCtrl+C exits this placeholder.\n')
    if state_dir:
        Path(state_dir, role + '.json').write_text(json.dumps({'pid': os.getpid(), 'role': role}), encoding='utf-8')
    while True:
        try:
            message = input(f'{role}> ')
            size = shutil.get_terminal_size()
            print(f'{role} received: {message} | {size.columns} columns x {size.lines} rows')
            if state_dir:
                Path(state_dir, role + '-received.json').write_text(json.dumps({'pid': os.getpid(), 'role': role, 'message': message, 'columns': size.columns, 'rows': size.lines}), encoding='utf-8')
        except (EOFError, KeyboardInterrupt):
            break


def install_profile(state_dir):
    settings = Path(os.environ['LOCALAPPDATA']) / 'Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json'
    original = settings.read_text(encoding='utf-8-sig')
    config = json.loads(original)
    profiles = config['profiles']['list']
    profile = next((p for p in profiles if p.get('guid') == '{41cc0613-92f0-4c15-967d-9490faec2f94}'), None)
    if profile is None:
        profile = {'guid': '{41cc0613-92f0-4c15-967d-9490faec2f94}'}
        profiles.append(profile)
    # Cascadia Mono: Courier New has no box-drawing/braille/arrow glyphs and 'aliased'
    # rendering drops colour emoji, so Claude's UI came out as boxes (owner, 15 Oct).
    # 12pt so two side-by-side panes get usable width; dark Campbell scheme like the
    # user's consoles.
    profile.update(name=PROFILE, commandline='cmd.exe', hidden=False,
                   font={'face': 'Cascadia Mono', 'size': 12}, antialiasingMode='grayscale',
                   colorScheme='Campbell')
    (state_dir / 'terminal-settings-before.json').write_text(original, encoding='utf-8')
    settings.write_text(json.dumps(config, indent=4) + '\n', encoding='utf-8')


def layout_description(n):
    """Human wording of the split layout for n panes (launch prints it as requested_layout)."""
    if n <= 4:
        return f'1 row x {n} columns'
    top = math.ceil(n / 2)
    return f'2 rows: {top} columns over {n - top}'


def wt_command(name, state_dir, agents, mode_flag=None, brief_file=None, cwd=None, members=DEFAULT_LABELS,
               crew='default'):
    """wt.exe argv for one tab holding `members` (pane labels, in order).

    2 panes: side by side, focus left (unchanged since the first crew). 3-4: one row of equal columns,
    each new pane split off the last. 5-8: top row ceil(n/2) panes, bottom row the rest.
    """
    command = ['wt.exe', '-w', '-1', '--pos', '0,0', '--maximized']
    script = str(Path(__file__).resolve())

    def add(parts, role=None):
        if len(command) > 6:
            command.append(';')
        command.extend(parts)
        if role:
            if agents:
                command.extend(['--profile', PROFILE])
            # start mode shows short titles; the trial modes keep the shift name in the title.
            title = role if mode_flag == '--start' else 'Nightshift | ' + role + ' | ' + name
            command.extend(['--title', title, '--suppressApplicationTitle',
                            '-d', '%USERPROFILE%', sys.executable, script, '--pane', role, '--state-dir', str(state_dir)])
            if mode_flag:
                command.append(mode_flag)
            if brief_file:
                command.extend(['--brief-file', str(Path(brief_file).resolve())])
            if cwd:
                command.extend(['--cwd', cwd])
            if crew != 'default':
                command.extend(['--crew', crew])

    def columns(labels):
        # Each new pane takes (remaining-1)/remaining of the last one, so all end up equal.
        for i, label in enumerate(labels, 1):
            left = len(labels) + 1 - i
            add(['split-pane', '-V', '-s', f'{left / (left + 1):.4g}'], label)

    n = len(members)
    add(['new-tab'], members[0])
    if n == 2:
        add(['split-pane', '-V', '-s', '0.5'], members[1])
        add(['move-focus', 'left'])
        return command
    if n <= 4:
        columns(members[1:])
    else:
        top = math.ceil(n / 2)
        add(['split-pane', '-H', '-s', '0.5'], members[top])
        add(['move-focus', 'up'])
        columns(members[1:top])
        add(['move-focus', 'down'])
        columns(members[top + 1:])
    add(['focus-pane', '-t', '0'])
    return command


def terminal_windows():
    """Visible Windows Terminal top-level windows as {hwnd: title}."""
    user32 = ctypes.windll.user32
    found = {}

    def inspect(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == 'CASCADIA_HOSTING_WINDOW_CLASS':
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            found[int(hwnd)] = title.value
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(inspect), 0)
    return found


def restore_foreground(hwnd):
    """Give focus back to the window the human was using before a crew window appeared.

    A new Terminal window takes the foreground, so anything the human types or dictates in the next
    seconds lands in Foreman's prompt (it happened, 15 Oct). AttachThreadInput lets a background
    process hand focus back.
    """
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    if not hwnd or not user32.IsWindow(hwnd) or user32.GetForegroundWindow() == hwnd:
        return
    fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    me = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(fg_thread, me, True)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(fg_thread, me, False)


def close_terminal_windows(title_prefix):
    """WM_CLOSE every Terminal window whose title starts with title_prefix; returns how many."""
    hwnds = [h for h, title in terminal_windows().items() if title.startswith(title_prefix)]
    for hwnd in hwnds:
        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)
    return len(hwnds)


def launch(command, name, state_dir, extra, labels=DEFAULT_LABELS):
    before = set(terminal_windows())
    human_window = ctypes.windll.user32.GetForegroundWindow()
    (state_dir / 'launch.json').write_text(json.dumps({'argv': command, 'launcher_pid': os.getpid()}), encoding='utf-8')
    subprocess.run(command, check=True)
    for _ in range(20):
        new = [h for h in terminal_windows() if h not in before]
        if new:
            # Remembered so `down` can close exactly this window (wt.exe hosts every Terminal window).
            launch_record = json.loads((state_dir / 'launch.json').read_text(encoding='utf-8'))
            launch_record['hwnd'] = new[0]
            (state_dir / 'launch.json').write_text(json.dumps(launch_record), encoding='utf-8')
            time.sleep(1)
            restore_foreground(human_window)
            break
        time.sleep(0.25)
    # Each pane writes <role>.json once it is running; pane titles are too short to identify the window.
    pending = list(labels)
    for _ in range(60):
        pending = [label for label in labels if not (state_dir / (label + '.json')).exists()]
        if not pending:
            break
        time.sleep(0.5)
    print(json.dumps({'window_name': name, 'state_dir': str(state_dir), 'requested_layout': layout_description(len(labels)),
                      **extra, 'panes_missing': pending}))
    if pending:
        raise SystemExit(f'WT activation returned success, but these panes never reported: {", ".join(pending)}.')


TICKET_RE = re.compile(r'[A-Z][A-Z0-9]+-\d+')


def ticket_prompt(ticket_id):
    """Turn a tasks-cli ticket into the Foreman task; returns (id, prompt, codebase folder or None)."""
    result = subprocess.run(['tasks-cli', 'get', ticket_id], capture_output=True, timeout=30)
    if result.returncode != 0:
        raise SystemExit(f'tasks-cli get {ticket_id} failed: {result.stderr.decode("utf-8", "replace").strip()}')
    task = json.loads(result.stdout.decode('utf-8', 'replace'))
    body = task.get('body', '')
    codebase = task.get('frontmatter', {}).get('codebase')
    if not codebase:
        match = re.search(r'^codebase:\s*(.+?)\s*$', body, re.MULTILINE)
        codebase = match.group(1) if match else None
    prompt = (f'Ticket {task["task_id"]}: {task["title"]}\n\n{body.strip()}\n\n'
              f'Record progress with `tasks-cli note {task["task_id"]} --note "..."`; never edit task files directly, '
              'and leave the ticket open for the owner to review.')
    return task['task_id'], prompt, codebase


def lock_path(cwd):
    """LOCKS_DIR file for a working folder: C:\\scripts -> c__scripts.json."""
    return LOCKS_DIR / (re.sub(r'[:\\/]', '_', str(Path(cwd).resolve()).lower()) + '.json')


def read_locks():
    """[(path, lock dict)] for every readable lock file; [] when LOCKS_DIR is missing."""
    found = []
    for path in sorted(LOCKS_DIR.glob('*.json')) if LOCKS_DIR.exists() else []:
        try:
            lock = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(lock, dict):
            found.append((path, lock))
    return found


class FolderLocked(SystemExit):
    """The working folder is held by a live shift; exits with code 3."""
    def __init__(self, code, message):
        super().__init__(code)
        self.message = message


def take_lock(cwd, name, state_dir):
    """Claim cwd for shift `name`; FolderLocked if a live shift holds it. A stale or unreadable lock is replaced."""
    path = lock_path(cwd)
    try:
        held = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        held = None
    holder = SHIFTS_DIR / str(held['shift']) if isinstance(held, dict) and held.get('shift') else None
    if holder and holder.is_dir() and shift_record(holder)['alive']:
        message = (f"folder {cwd} is held by live shift {held['shift']} (state dir {holder}); "
                   f"stop it with `nightshift down {held['shift'].removeprefix('Nightshift-shift-')}` first")
        print(message, file=sys.stderr)
        raise FolderLocked(3, message)
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'shift': name, 'cwd': cwd, 'state_dir': str(state_dir), 'launcher_pid': os.getpid(),
                                'started': time.strftime('%Y-%m-%d %H:%M:%S')}), encoding='utf-8')
    return path


def release_lock(shift_name):
    """Remove every lock held by shift_name; returns the folders released."""
    released = []
    for path, lock in read_locks():
        if lock.get('shift') == shift_name:
            try:
                path.unlink()
            except OSError:
                continue
            released.append(lock.get('cwd'))
    return released


def start(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift start',
        description='Start a Nightshift crew: Claude Foreman and Coder side by side in one Windows Terminal tab '
                    '(the default crew; --crew NAME uses crews/NAME.yaml for other roles and pane counts). '
                    'The task is saved to <state_dir>/prompt.txt and given to Foreman only; Coder sends READY '
                    'and waits for Foreman to delegate work. Panes run in the current directory (no worktree is '
                    'created) in Claude auto permission mode. Each start is a new shift with its own '
                    '<shift>-foreman / <shift>-coder session names; there is no restart or resume, and a closed '
                    'pane is not relaunched. A folder holds one live shift at a time: start exits with code 3 '
                    'while another live shift holds the working folder (nightshift down releases it).',
        epilog='Example: nightshift start "Add a --verbose flag to report.py"  |  '
               'nightshift start --dry-run --prompt-file task.txt')
    parser.add_argument('prompt', nargs='?', help='the task for Foreman (quote it; it is treated as plain text), '
                                                  'or a ticket ID such as NGH-005: the ticket body becomes the task '
                                                  'and its codebase: field the working folder')
    parser.add_argument('--prompt-file', help='read the task from this UTF-8 file instead (safest for text with quotes or & | < > ^ %%)')
    parser.add_argument('--cwd', help='folder the crew works in (default: the ticket codebase, else the current directory)')
    parser.add_argument('--continue-from', metavar='SHIFT_DIR',
                        help='an earlier shift folder for the same task: its handover.md is appended so the crew carries on')
    parser.add_argument('--crew', default='default',
                        help='crew manifest crews/CREW.yaml: roles, briefs, colours, counts, model '
                             '(default: default = one Foreman, one Coder)')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the intended command, roles and prompt as JSON; launch and write nothing')
    args = parser.parse_args(argv)
    crew = load_crew(args.crew)
    if args.prompt is not None and args.prompt_file:
        parser.error('give the task either as an argument or with --prompt-file, not both')
    prompt = Path(args.prompt_file).read_text(encoding='utf-8') if args.prompt_file else args.prompt
    ticket = None
    ticket_cwd = None
    if prompt and TICKET_RE.fullmatch(prompt.strip()):
        ticket, prompt, ticket_cwd = ticket_prompt(prompt.strip())
    if prompt is None or not prompt.strip():
        parser.error('the task prompt is empty; give the work for Foreman, e.g. nightshift start "Fix the failing test"')
    required = parse_requires(prompt)   # before the handover is appended: only the task itself sets the bar
    if args.continue_from:
        handover = Path(args.continue_from) / 'handover.md'
        if handover.exists():
            prompt += ('\n\nAn earlier crew was interrupted on this task. Its handover follows; check what it already '
                       'committed before redoing anything.\n\n' + handover.read_text(encoding='utf-8')[-6000:])
    name = 'Nightshift-shift-' + uuid.uuid4().hex[:8]
    state_dir = SHIFTS_DIR / name
    cwd = args.cwd or ticket_cwd or os.getcwd()
    if not Path(cwd).is_dir():
        parser.error(f'working folder does not exist: {cwd}')
    cwd = str(Path(cwd).resolve())
    if ';' in cwd:
        parser.error(f'the working folder contains ";", which Windows Terminal treats as a command separator: {cwd}')
    members = crew_members(crew, name)
    labels = [m['label'] for m in members]
    command = wt_command(name, state_dir, agents=True, mode_flag='--start', cwd=cwd, members=labels, crew=crew['name'])
    if args.dry_run:
        plan = {
            'dry_run': True, 'shift': name, 'state_dir': str(state_dir), 'cwd': cwd, 'ticket': ticket,
            'prompt_file': str(state_dir / 'prompt.txt'), 'model': crew['model'], 'profile': PROFILE,
            'permission_mode': crew['permission_mode'], 'lock': str(lock_path(cwd)),
            'roles': {m['label']: {'session_name': m['identity'], 'pane_title': m['label'], 'peer': m['peer'],
                                   'receives_prompt': m['human_contact'], 'first_message': opening_message(m)}
                      for m in members},
            'wt_argv': command, 'prompt': prompt}
        if crew['name'] != 'default':
            plan['crew'] = crew['name']   # the default crew's plan stays exactly as it was before manifests
        if ticket:
            plan['requires'] = required   # free-text plans stay as they were
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return
    take_lock(cwd, name, state_dir)
    state_dir.mkdir(parents=True)
    (state_dir / 'prompt.txt').write_text(prompt, encoding='utf-8')
    (state_dir / 'requires.txt').write_text(required, encoding='utf-8')
    (state_dir / 'crew.json').write_text(json.dumps({'crew': crew['name'], 'members': members}, indent=2),
                                         encoding='utf-8')
    if ticket:
        (state_dir / 'ticket.txt').write_text(ticket, encoding='utf-8')
    install_profile(state_dir)
    launch(command, name, state_dir, {'mode': 'start', 'prompt_file': str(state_dir / 'prompt.txt')}, labels)
    return state_dir


def process_start_epoch(pid):
    """Creation time of a process as epoch seconds (local clock), or None if it does not exist."""
    out = subprocess.run(['wmic', 'process', 'where', f'processid={pid}', 'get', 'creationdate'],
                         capture_output=True, timeout=15).stdout.decode('utf-8', 'replace').replace('\x00', '')
    match = re.search(r'(\d{14})\.\d+', out)
    if not match:
        return None
    return time.mktime(time.strptime(match.group(1), '%Y%m%d%H%M%S'))


def pid_alive(pid, started=None):
    """True if this PID is a Claude process started when the crew member was (CSV PID field compared whole).

    Windows reuses PIDs, so a dead crew member's PID can turn up on another process - even another
    Claude session - and make a finished shift look alive; the start time settles it.
    """
    out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                         capture_output=True, timeout=15).stdout.decode('utf-8', 'replace')
    for line in out.splitlines():
        if line.startswith('"'):
            fields = line.strip('"').split('","')
            if fields[1:2] == [str(pid)] and fields[0].lower() in ('claude.exe', 'node.exe'):
                if started is None:
                    return True
                created = process_start_epoch(pid)
                return created is None or abs(created - started) <= 300
    return False


def shift_record(state_dir):
    """What `status` knows about one shift directory: roles, pids, liveness, mesh registration, task."""
    record = {'shift': state_dir.name, 'state_dir': str(state_dir), 'roles': {}}
    prompt = state_dir / 'prompt.txt'
    record['task'] = prompt.read_text(encoding='utf-8').strip().splitlines()[0][:120] if prompt.exists() else ''
    sessions = Path.home() / '.claude' / 'sessions'
    try:
        labels = [m['label'] for m in json.loads((state_dir / 'crew.json').read_text(encoding='utf-8'))['members']]
    except (OSError, ValueError, KeyError, TypeError):
        labels = DEFAULT_LABELS   # shifts started before crew manifests
    for role in labels:
        info_path = state_dir / f'{role}.json'
        if not info_path.exists():
            continue
        info = json.loads(info_path.read_text(encoding='utf-8'))
        pid = info['pid']
        record['roles'][role] = {
            'pid': pid, 'wrapper_pid': info.get('wrapper_pid'), 'name': info.get('name'), 'cwd': info.get('cwd'),
            'alive': pid_alive(pid, info['started']) if 'started' in info else pid_alive(pid),
            'registered': (sessions / f'{pid}.json').exists(),
        }
    record['alive'] = any(r['alive'] for r in record['roles'].values())
    result_path = state_dir / 'result.json'
    record['stage'] = None
    try:
        result = json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else None
        record['result'] = result.get('status', 'invalid') if result is not None else 'none'
        if result is not None:
            record['stage'] = result_stage(result)
    except (ValueError, AttributeError):
        record['result'] = 'invalid'
    record['requires'] = shift_requires(state_dir)
    pending = unacked_messages(state_dir)
    record['messages'] = {'unsent': sum(m['state'] == 'stored' for m in pending), 'unacked': len(pending)}
    # Only shifts started with the ladder (requires.txt) can be short; older done shifts stay done.
    if record['result'] == 'done' and (state_dir / 'requires.txt').exists() \
            and not meets_rung(record['stage'], record['requires']):
        record['result'] = 'short'   # done, but below the rung the ticket asked for
    return record


def resolve_shift(ref):
    """`latest`, a full shift name or a unique suffix -> state dir."""
    dirs = sorted((d for d in SHIFTS_DIR.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    if not dirs:
        raise SystemExit('no shifts found under ' + str(SHIFTS_DIR))
    if ref == 'latest':
        return dirs[-1]
    hits = [d for d in dirs if d.name == ref or d.name.endswith(ref)]
    if len(hits) != 1:
        raise SystemExit(f'shift {ref!r} matches {len(hits)} shifts; give the full name')
    return hits[0]


def status(argv):
    parser = argparse.ArgumentParser(prog='nightshift status', description='List shifts and whether their crews are alive.')
    parser.add_argument('--all', action='store_true', help='include shifts whose crew is gone')
    args = parser.parse_args(argv)
    records = [shift_record(d) for d in sorted(SHIFTS_DIR.iterdir(), key=lambda d: d.stat().st_mtime) if d.is_dir()] \
        if SHIFTS_DIR.exists() else []
    holds = {lock.get('shift'): lock.get('cwd') for _, lock in read_locks()}
    for record in records:
        record['holds'] = holds.get(record['shift'])
    if not args.all:
        records = [r for r in records if r['alive']]
    print(json.dumps(records, indent=2))


def pane_tail(pid, lines=40):
    """Last visible lines of a pane, read-only, via agents-cli; '' when unreadable."""
    try:
        peek = subprocess.run(['agents-cli', '_attach', 'peek', str(pid), '0'], capture_output=True, timeout=15)
        screen = json.loads((peek.stdout or b'{}').decode('utf-8', 'replace'))
        return [line for line in screen.get('lines', []) if line.strip()][-lines:]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def load_denials(state_dir):
    """Denial lines recorded for a shift (<state_dir>/denials.json); [] when missing or invalid."""
    try:
        denials = json.loads((state_dir / 'denials.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return denials if isinstance(denials, list) else []


def record_denials(state_dir, tails):
    """Add pane lines mentioning 'denied' (auto-mode classifier, Permission denied) to denials.json.

    tails is {role: [lines]}; each (role, line) is kept once, with the time it was first seen.
    """
    denials = load_denials(state_dir)
    seen = {(d.get('role'), d.get('line')) for d in denials if isinstance(d, dict)}
    added = False
    for role, lines in tails.items():
        for line in lines:
            if 'denied' in line.lower() and (role, line.strip()) not in seen:
                seen.add((role, line.strip()))
                denials.append({'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'role': role, 'line': line.strip()})
                added = True
    if added:
        (state_dir / 'denials.json').write_text(json.dumps(denials, indent=2), encoding='utf-8')
    return denials


def shift_commits(cwd, since):
    """One-line git log of cwd since the epoch time `since`; [] when git fails."""
    try:
        log = subprocess.run(['git', '-C', cwd, 'log',
                              f'--since={time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))}', '--oneline'],
                             capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if log.returncode != 0:
        return []
    return [line for line in (log.stdout or b'').decode('utf-8', 'replace').splitlines() if line.strip()]


def suite_result():
    """Run the launcher's own test suite: ('passed'|'failed', last output line) or ('not run: <err>', '')."""
    try:
        run = subprocess.run([SUITE_PYTHON,'-m', 'pytest', SUITE_TESTS, '-q'],
                             capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as err:
        return f'not run: {err}', ''
    lines = [line for line in (run.stdout or b'').decode('utf-8', 'replace').splitlines() if line.strip()]
    return ('passed' if run.returncode == 0 else 'failed'), (lines[-1].strip() if lines else '')


def handover_report(state_dir, record):
    """Result, commits-since-start and test-suite sections for handover.md."""
    lines = ['## Result', '']
    try:
        result = json.loads((state_dir / 'result.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        result = None
    if isinstance(result, dict):
        lines += [f"Status: {result.get('status', '')}",
                  f"Stage: {result_stage(result)} (required: {shift_requires(state_dir)})"]
        evidence = [e for e in result.get('evidence') or [] if isinstance(e, dict)]
        lines += ['Evidence:', *[f"- {e.get('stage')}: {e.get('what')} — {e.get('outcome')}" for e in evidence]] \
            if evidence else ['Evidence: (none)']
        lines.append(f"Summary: {result.get('summary', '')}")
        changed = result.get('changed') or []
        lines += ['Changed:', *[f'- {c}' for c in changed]] if changed else ['Changed: (none)']
    else:
        lines.append('(no result.json)')
    lines += ['', '## Auto-mode denials', '']
    denials = load_denials(state_dir)
    lines += [f"- {d.get('time')} {d.get('role')}: {d.get('line')}" for d in denials] if denials else ['(none)']
    lines += ['', '## Outstanding claims', '']
    claims = load_claims(state_dir)
    lines += [f"- {p} — {c.get('role')} (since {c.get('time')})" for p, c in claims.items()] if claims else ['(none)']
    lines += ['', '## Unacknowledged messages', '']
    pending = unacked_messages(state_dir)
    lines += [f"- msg:{m['id']} {m['kind']} {m['asg'] or '-'} {m['sender']} -> {m['recipient']} [{m['state']}]"
              for m in pending] if pending else ['(none)']
    lines += ['', '## Commits since shift start', '']
    launch_path = state_dir / 'launch.json'
    roles = record['roles']
    cwd = (roles.get('Foreman') or {}).get('cwd') or next((r['cwd'] for r in roles.values() if r.get('cwd')), None)
    if not launch_path.exists():
        lines.append('(no launch.json)')
    elif not cwd:
        lines.append('(no crew cwd recorded)')
    else:
        commits = shift_commits(cwd, launch_path.stat().st_mtime)
        lines += ['```', *commits, '```'] if commits else ['(none)']
    outcome, last = suite_result()
    lines += ['', '## Test suite at teardown', '', f'{Path(SUITE_TESTS).name}: {outcome} — {last}', '']
    return lines


def down(argv):
    parser = argparse.ArgumentParser(prog='nightshift down',
                                     description='Stop a crew: capture each pane, write handover.md, end the sessions, close the window.')
    parser.add_argument('shift', nargs='?', default='latest', help='shift name, unique suffix, or "latest" (default)')
    args = parser.parse_args(argv)
    state_dir = resolve_shift(args.shift)
    record = shift_record(state_dir)
    tails = {role: pane_tail(info['pid']) for role, info in record['roles'].items() if info['alive']}
    record_denials(state_dir, tails)
    handover = [f"# Handover: {record['shift']}", '', f"Task: {record['task']}", f"Stopped: {time.strftime('%Y-%m-%d %H:%M')}", '']
    handover += handover_report(state_dir, record)
    for role, info in record['roles'].items():
        handover += [f"## {role} ({info['name']}, pid {info['pid']}, {'alive' if info['alive'] else 'gone'})", '',
                     '```', *(tails.get(role, []) or ['(pane not readable)']), '```', '']
    (state_dir / 'handover.md').write_text('\n'.join(handover), encoding='utf-8')
    for role, info in record['roles'].items():
        for pid in (info['pid'], info.get('wrapper_pid')):
            if pid:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True, timeout=30)
    released = release_lock(record['shift'])
    launch_record = json.loads((state_dir / 'launch.json').read_text(encoding='utf-8')) if (state_dir / 'launch.json').exists() else {}
    hwnd = launch_record.get('hwnd')
    windows = terminal_windows()
    if not hwnd:
        # Shift started before the hwnd was recorded: fall back to the one window titled by a crew pane.
        candidates = [h for h, title in windows.items() if title in ('Foreman', 'Coder')]
        hwnd = candidates[0] if len(candidates) == 1 else None
    window_open = bool(hwnd and hwnd in windows)
    if window_open:
        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)   # WM_CLOSE on that window only
    print(json.dumps({'shift': record['shift'], 'handover': str(state_dir / 'handover.md'),
                      'stopped': [r for r, i in record['roles'].items() if i['alive']],
                      'window_closed': window_open, 'lock_released': released}))


class FileClaimed(FolderLocked):
    """A file is claimed by another role; `claim` exits with code 4 and changes nothing."""


def normalise_path(p):
    """Claims key for a file: absolute and case-folded, so C:/scripts/x.py and C:\\Scripts\\x.py are one key."""
    return os.path.normcase(str(Path(p).resolve()))


def load_claims(state_dir):
    """{path: {"role", "time"}} from <state_dir>/claims.json; {} when missing or invalid."""
    try:
        claims = json.loads((Path(state_dir) / 'claims.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def save_claims(state_dir, claims):
    """Write claims.json through a temp file + os.replace, so a reader never sees half a file."""
    tmp = Path(state_dir) / f'claims.{os.getpid()}.tmp'
    tmp.write_text(json.dumps(claims, indent=2), encoding='utf-8')
    os.replace(tmp, Path(state_dir) / 'claims.json')


@contextlib.contextmanager
def claims_locked(state_dir, wait=5.0, stale=30.0):
    """Hold <state_dir>/claims.lock (O_EXCL) around a read-modify-write of claims.json.

    Two Coders claiming at once would otherwise both read the old file and one claim would be lost.
    A lock older than `stale` seconds is left over from a crashed writer and removed.
    """
    lock = Path(state_dir) / 'claims.lock'
    deadline = time.time() + wait
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                is_stale = time.time() - lock.stat().st_mtime > stale
            except OSError:
                continue   # released between open and stat: try again
            if is_stale:
                with contextlib.suppress(OSError):
                    lock.unlink()
                continue
            if time.time() > deadline:
                raise SystemExit(f'{lock} is still held after {wait:.0f}s; try again')
            time.sleep(0.1)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()


def same_role(claim, role):
    return str(claim.get('role', '')).lower() == role.lower()


def claim(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift claim',
        description='Claim files for a role before editing them. All or nothing: if any path is held by another '
                    'role, nothing is claimed and the exit code is 4. Re-claiming your own path refreshes its time.')
    parser.add_argument('shift', help='shift name, unique suffix, or "latest"')
    parser.add_argument('role', help='who holds the claim, e.g. the Coder session name')
    parser.add_argument('paths', nargs='+')
    args = parser.parse_args(argv)
    state_dir = resolve_shift(args.shift)
    paths = list(dict.fromkeys(normalise_path(p) for p in args.paths))
    with claims_locked(state_dir):
        claims = load_claims(state_dir)
        conflicts = [p for p in paths if p in claims and not same_role(claims[p], args.role)]
        for p in conflicts:
            print(f"{p} is already claimed by {claims[p].get('role')} since {claims[p].get('time')}", file=sys.stderr)
        if conflicts:
            raise FileClaimed(4, f'{len(conflicts)} of {len(paths)} paths are claimed by another role')
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        claims.update({p: {'role': args.role, 'time': now} for p in paths})
        save_claims(state_dir, claims)
    print(json.dumps({'shift': state_dir.name, 'role': args.role, 'claimed': paths}))


def release(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift release',
        description="Release a role's claims: the given paths, or all of them when none are given. "
                    'Paths held by another role are left alone.')
    parser.add_argument('shift', help='shift name, unique suffix, or "latest"')
    parser.add_argument('role')
    parser.add_argument('paths', nargs='*')
    args = parser.parse_args(argv)
    state_dir = resolve_shift(args.shift)
    wanted = {normalise_path(p) for p in args.paths}
    with claims_locked(state_dir):
        claims = load_claims(state_dir)
        released = [p for p, c in claims.items() if same_role(c, args.role) and (not wanted or p in wanted)]
        for p in released:
            del claims[p]
        if released:
            save_claims(state_dir, claims)
    print(json.dumps({'released': released}))


def claims_command(argv):
    parser = argparse.ArgumentParser(prog='nightshift claims', description="Print a shift's file claims as JSON.")
    parser.add_argument('shift', help='shift name, unique suffix, or "latest"')
    args = parser.parse_args(argv)
    print(json.dumps(load_claims(resolve_shift(args.shift)), indent=2))


class ProtocolViolation(FolderLocked):
    """An ack that does not match the mailbox (unknown id, wrong recipient, stale incarnation); exits with code 5."""


MSG_KINDS = ('assignment', 'result', 'note', 'ack')
MAILBOX_SCHEMA = '''CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, sender TEXT NOT NULL, sender_incarnation REAL,
    recipient TEXT NOT NULL, recipient_incarnation REAL, asg TEXT, body TEXT NOT NULL, created TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'stored')'''


@contextlib.contextmanager
def mailbox(state_dir, create=True):
    """Open <state_dir>/mailbox.sqlite3 (WAL, busy timeout); yields None when create=False and it does not exist.

    The connection is always closed, so nothing keeps the file open (Windows cannot delete an open file).
    """
    path = Path(state_dir) / 'mailbox.sqlite3'
    if not create and not path.exists():
        yield None
        return
    with contextlib.closing(sqlite3.connect(path, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA journal_mode=WAL')
        if create:
            conn.execute(MAILBOX_SCHEMA)
            conn.commit()
        yield conn


def crew_roster(state_dir):
    """[{label, identity, human_contact}] from crew.json, else DEFAULT_LABELS with the <shift>-<role> names."""
    state_dir = Path(state_dir)
    try:
        members = json.loads((state_dir / 'crew.json').read_text(encoding='utf-8'))['members']
        return [{'label': m['label'], 'identity': m.get('identity'), 'human_contact': bool(m.get('human_contact'))}
                for m in members]
    except (OSError, ValueError, KeyError, TypeError):
        return [{'label': label, 'identity': f'{state_dir.name}-{label.lower()}', 'human_contact': label == 'Foreman'}
                for label in DEFAULT_LABELS]


def member_info(state_dir, label):
    """<state_dir>/<label>.json as a dict, {} when missing or unreadable."""
    try:
        info = json.loads((Path(state_dir) / f'{label}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def resolve_role(state_dir, role):
    """Canonical crew label for a label (any case) or a member's mesh identity; SystemExit when not in the crew."""
    roster = crew_roster(state_dir)
    wanted = role.lower()
    for m in roster:
        names = {m['label'], m['identity'], member_info(state_dir, m['label']).get('name')}
        if wanted in {str(n).lower() for n in names if n}:
            return m['label']
    raise SystemExit(f"{role!r} is not a member of shift {Path(state_dir).name}; "
                     f"use one of: {', '.join(m['label'] for m in roster)}")


def incarnation(state_dir, label):
    """The member's current incarnation: 'started' from <label>.json, None when that file is absent."""
    return member_info(state_dir, label).get('started')


def mesh_name(state_dir, label):
    """The member's session name: <label>.json name, else its crew.json identity, else None."""
    name = member_info(state_dir, label).get('name')
    return name or next((m['identity'] for m in crew_roster(state_dir) if m['label'] == label), None)


def human_contact_label(state_dir):
    return next((m['label'] for m in crew_roster(state_dir) if m['human_contact']), DEFAULT_LABELS[0])


def msg_header(row):
    return f"msg:{row['id']} kind:{row['kind']} asg:{row['asg'] or '-'}"


def insert_message(conn, state_dir, kind, sender, recipient, body, asg=None):
    """Store a message with both incarnations as they are now; returns its id."""
    with conn:
        cur = conn.execute(
            'INSERT INTO messages (kind, sender, sender_incarnation, recipient, recipient_incarnation, asg, body, '
            'created, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (kind, sender, incarnation(state_dir, sender), recipient, incarnation(state_dir, recipient), asg, body,
             time.strftime('%Y-%m-%d %H:%M:%S'), 'stored'))
    return cur.lastrowid


def get_message(conn, msg_id):
    row = conn.execute('SELECT * FROM messages WHERE id = ?', (msg_id,)).fetchone()
    return dict(row) if row else None


def msg(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift msg',
        description='Record crew messages in the shift mailbox before sending them with SendMessage.')
    sub = parser.add_subparsers(dest='action', required=True)
    send = sub.add_parser('send', help='store a message; prints its header line for the top of the SendMessage')
    send.add_argument('shift', help='shift name, unique suffix, or "latest"')
    send.add_argument('sender', metavar='FROM', help='your crew label (Foreman, Coder) or session name')
    send.add_argument('recipient', metavar='TO', help="the peer's crew label or session name")
    send.add_argument('--kind', required=True, choices=MSG_KINDS)
    send.add_argument('--body', required=True, help='the message text the recipient acts on')
    send.add_argument('--asg', help='assignment id this message belongs to')
    sent = sub.add_parser('sent', help='mark a stored message as delivered with SendMessage')
    sent.add_argument('shift', help='shift name, unique suffix, or "latest"')
    sent.add_argument('id', type=int)
    args = parser.parse_args(argv)
    state_dir = resolve_shift(args.shift)
    if args.action == 'send':
        sender, recipient = resolve_role(state_dir, args.sender), resolve_role(state_dir, args.recipient)
        with mailbox(state_dir) as conn:
            msg_id = insert_message(conn, state_dir, args.kind, sender, recipient, args.body, args.asg)
            print(msg_header(get_message(conn, msg_id)))
        return
    with mailbox(state_dir) as conn:
        with conn:
            conn.execute("UPDATE messages SET state = 'sent' WHERE id = ? AND state = 'stored'", (args.id,))
        row = get_message(conn, args.id)
    if row is None:
        raise SystemExit(f'no message msg:{args.id} in shift {state_dir.name}')
    print(json.dumps(row))


def inbox_view(state_dir, label):
    """What a member has to deal with: unacked inbound, its own unsent outbound, open assignments."""
    view = {'inbound_unacked': [], 'outbound_unsent': [], 'active_assignments': []}
    with mailbox(state_dir, create=False) as conn:
        if conn is None:
            return view
        view['inbound_unacked'] = [dict(r) for r in conn.execute(
            "SELECT * FROM messages WHERE recipient = ? AND state IN ('stored', 'sent') ORDER BY id", (label,))]
        for r in conn.execute("SELECT * FROM messages WHERE sender = ? AND state = 'stored' ORDER BY id", (label,)):
            view['outbound_unsent'].append({**dict(r), 'header': msg_header(r), 'to': mesh_name(state_dir, r['recipient'])})
        for r in conn.execute(
                "SELECT * FROM messages a WHERE kind = 'assignment' AND (sender = ? OR recipient = ?) AND NOT EXISTS "
                "(SELECT 1 FROM messages b WHERE b.kind = 'result' AND b.asg = a.asg) ORDER BY id", (label, label)):
            view['active_assignments'].append(
                {**dict(r), 'stale': r['recipient_incarnation'] != incarnation(state_dir, r['recipient'])})
    return view


def inbox(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift inbox',
        description="A member's mailbox as JSON: inbound_unacked, outbound_unsent (send each, then `msg sent`), "
                    'active_assignments (stale = sent to an earlier incarnation of the recipient).')
    parser.add_argument('shift', help='shift name, unique suffix, or "latest"')
    parser.add_argument('role', help='your crew label (Foreman, Coder) or session name')
    args = parser.parse_args(argv)
    state_dir = resolve_shift(args.shift)
    print(json.dumps(inbox_view(state_dir, resolve_role(state_dir, args.role)), indent=2))


def ack(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift ack',
        description='Acknowledge a received message and print its stored body, the only text to act on. '
                    'Unknown id, wrong recipient or a message for an earlier incarnation: a protocol-violation '
                    'note goes to the human contact and the exit code is 5 (do not act).')
    parser.add_argument('shift', help='shift name, unique suffix, or "latest"')
    parser.add_argument('role', help='your crew label (Foreman, Coder) or session name')
    parser.add_argument('id', type=int)
    args = parser.parse_args(argv)
    state_dir = resolve_shift(args.shift)
    label = resolve_role(state_dir, args.role)
    current = incarnation(state_dir, label)
    with mailbox(state_dir) as conn:
        row = get_message(conn, args.id)
        if row is None:
            reason = f'no message msg:{args.id}'
        elif row['recipient'] != label:
            reason = f"msg:{args.id} is addressed to {row['recipient']}, not {label}"
        elif row['recipient_incarnation'] != current:
            reason = (f"msg:{args.id} was sent to an earlier {label} incarnation "
                      f"({row['recipient_incarnation']}; current {current})")
        else:
            reason = None
        if reason:
            insert_message(conn, state_dir, 'note', label, human_contact_label(state_dir),
                           f'protocol violation: {reason} (ack of msg:{args.id} by {label})')
            print(f'protocol violation: {reason}', file=sys.stderr)
            raise ProtocolViolation(5, reason)
        outcome = 'already acknowledged' if row['state'] == 'acknowledged' else 'acknowledged'
        with conn:
            conn.execute("UPDATE messages SET state = 'acknowledged' WHERE id = ?", (args.id,))
        row = get_message(conn, args.id)
    print(json.dumps({'ack': outcome, 'row': row}))


def unacked_messages(state_dir):
    """Stored or sent rows, oldest first; [] when the shift has no mailbox."""
    with mailbox(state_dir, create=False) as conn:
        if conn is None:
            return []
        return [dict(r) for r in conn.execute("SELECT * FROM messages WHERE state IN ('stored', 'sent') ORDER BY id")]


def session_idle_since(pid):
    """Seconds the registered Claude session has been idle, or None if busy/unknown."""
    try:
        info = json.loads((Path.home() / '.claude' / 'sessions' / f'{pid}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if info.get('status') != 'idle':
        return None
    return max(0.0, time.time() - info.get('statusUpdatedAt', 0) / 1000)


def wait_for_result(state_dir, stall_minutes, poll_seconds=30, clock=time.time, sleep=time.sleep):
    """Block until result.json appears, the crew dies, or both sessions sit idle for stall_minutes.

    Returns the result dict ({"status": "done"|"blocked"|"stalled"|"died", ...}).
    """
    result_path = state_dir / 'result.json'
    while True:
        if result_path.exists():
            try:
                return json.loads(result_path.read_text(encoding='utf-8'))
            except ValueError:
                return {'status': 'blocked', 'summary': 'result.json is not valid JSON'}
        record = shift_record(state_dir)
        if not record['alive']:
            return {'status': 'died', 'summary': 'crew processes ended without writing result.json'}
        record_denials(state_dir, {role: pane_tail(info['pid'])
                                   for role, info in record['roles'].items() if info['alive']})
        idle = [session_idle_since(info['pid']) for info in record['roles'].values()]
        if idle and all(i is not None and i >= stall_minutes * 60 for i in idle):
            return {'status': 'stalled', 'summary': f'both sessions idle for {stall_minutes} minutes with no result.json'}
        sleep(poll_seconds)


def project_tickets(prefix):
    """Backlog ticket IDs for a project prefix, lowest number first, skipping any tagged needs-human."""
    result = subprocess.run(['tasks-cli', 'search', '--prefix', prefix, '--status', 'backlog', '--all'],
                            capture_output=True, timeout=60)
    if result.returncode != 0:
        raise SystemExit(f'tasks-cli search --prefix {prefix} failed: {result.stderr.decode("utf-8", "replace").strip()}')
    data = json.loads(result.stdout.decode('utf-8', 'replace'))
    tasks = [t for t in data['results'] if 'needs-human' not in (t.get('tags') or [])]
    return [t['task_id'] for t in sorted(tasks, key=lambda t: t['number'])]


def notify_owner(subject, body):
    """Email the owner through Mailkeep: prepare -> queue -> send, stopping at the first failure.

    Never raises - a mail failure must not crash the loop. Returns {'sent', 'draft_id', 'error'}.
    """
    outcome = {'sent': False, 'draft_id': None, 'error': None}

    def run(argv):
        result = subprocess.run(argv, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f'mailkeep {argv[1]} exited {result.returncode}: '
                               f'{result.stderr.decode("utf-8", "replace").strip()}')
        try:
            return json.loads(result.stdout.decode('utf-8', 'replace'))
        except ValueError:
            raise RuntimeError(f'mailkeep {argv[1]} printed no JSON') from None

    try:
        with tempfile.TemporaryDirectory() as tmp:
            body_file = Path(tmp) / 'body.txt'
            body_file.write_text(body, encoding='utf-8')
            prepared = run([MAILKEEP, 'prepare', '--identity', NOTIFY_IDENTITY, '--to', NOTIFY_TO,
                            '--subject', subject, '--body-file', str(body_file), '--execute'])
        draft_id = prepared.get('id') if isinstance(prepared, dict) else None
        if not draft_id:
            raise RuntimeError('mailkeep prepare returned no id')
        outcome['draft_id'] = draft_id
        queued = run([MAILKEEP, 'queue', draft_id, '--execute'])
        if isinstance(queued, dict) and queued.get('ok') is False:
            raise RuntimeError(f'mailkeep queue {draft_id} reported ok:false')
        sent = run([MAILKEEP, 'send', draft_id, '--execute'])
        confirmed = isinstance(sent, dict) and (sent.get('ok') is True or any(
            isinstance(r, dict) and r.get('id') == draft_id and r.get('ok') is True
            for r in sent.get('results') or []))
        if not confirmed:
            raise RuntimeError(f'mailkeep send did not confirm {draft_id}')
        outcome['sent'] = True
    except Exception as err:   # noqa: BLE001 - reported, never raised
        outcome['error'] = str(err)
    return outcome


def notify_body(status, records):
    """Plain-text owner email: overall status, then each ticket's result, handover and denials."""
    lines = [f'Status: {status}', '']
    for r in records:
        lines += [f"Ticket: {r['ticket']}", f"Result: {r['status']}: {r['summary']}",
                  f"Handover: {r['handover']}", 'Denials:']
        lines += [f"  {d.get('role')}: {d.get('line')}" for d in r['denials']] or ['  (none)']
        lines.append('')
    return '\n'.join(lines)


def loop(argv, continue_from=None):
    """continue_from: optional {ticket: shift_dir}; that ticket's first attempt starts with --continue-from it."""
    parser = argparse.ArgumentParser(
        prog='nightshift loop',
        description='Run tickets one per shift: start a crew, wait for result.json (or a stall), tear the shift down, '
                    'note the ticket, continue. Stops at the first blocked/stalled/died shift so a human can look. '
                    'With --project PREFIX [--max N], works that project\'s backlog tickets (lowest number first, '
                    'needs-human skipped) and moves each to in-progress when its shift starts.')
    parser.add_argument('tickets', nargs='*', help='ticket IDs, worked in order')
    parser.add_argument('--project', metavar='PREFIX', help='work this project\'s backlog instead of listed tickets')
    parser.add_argument('--max', type=int, metavar='N', help='work at most N tickets')
    parser.add_argument('--cwd', help='working folder for every shift (default: each ticket codebase)')
    parser.add_argument('--stall-minutes', type=int, default=20)
    parser.add_argument('--retries', type=int, default=2,
                        help='rerun a ticket this many times after its crew dies or stalls (default 2)')
    parser.add_argument('--no-notify', action='store_true',
                        help='do not email the owner when the loop halts or finishes')
    args = parser.parse_args(argv)

    def notify(subject, status, records):
        if args.no_notify:
            return
        # stderr, so stdout stays the shift event stream that ends with 'stopped' or 'finished'.
        print(json.dumps({'event': 'notified', 'subject': subject, **notify_owner(subject, notify_body(status, records))}),
              file=sys.stderr, flush=True)
    records = []
    if bool(args.tickets) == bool(args.project):
        parser.error('give either ticket IDs or --project PREFIX, not both')
    tickets = args.tickets or project_tickets(args.project)
    if args.max is not None:
        tickets = tickets[:args.max]
    if args.project:
        print(json.dumps({'event': 'selected', 'project': args.project, 'tickets': tickets}), flush=True)
    for ticket in tickets:
        previous = (continue_from or {}).get(ticket)
        for attempt in range(1, args.retries + 2):
            extra = (['--cwd', args.cwd] if args.cwd else []) + (['--continue-from', str(previous)] if previous else [])
            try:
                state_dir = start([ticket] + extra)
            except SystemExit as err:
                if err.code != 3:
                    raise
                # Another live shift holds the folder: a human has to look, so no down() and no retry.
                result = {'status': 'blocked', 'summary': getattr(err, 'message', str(err))}
                state_dir = None
                subprocess.run(['tasks-cli', 'note', ticket, '--heading', 'Nightshift loop', '--note',
                                f"Nightshift loop: attempt {attempt} could not start: {result['summary']}"],
                               capture_output=True, timeout=60)
                print(json.dumps({'event': 'ended', 'ticket': ticket, 'attempt': attempt, **result}), flush=True)
                break
            if args.project and attempt == 1:
                moved = subprocess.run(['tasks-cli', 'move', ticket, 'in-progress'], capture_output=True, timeout=60)
                if moved.returncode != 0:
                    print(json.dumps({'event': 'move_failed', 'ticket': ticket,
                                      'error': moved.stderr.decode('utf-8', 'replace').strip()}), flush=True)
            print(json.dumps({'event': 'started', 'ticket': ticket, 'shift': state_dir.name, 'attempt': attempt}), flush=True)
            result = wait_for_result(state_dir, args.stall_minutes)
            if result.get('status') == 'done':
                stage, rung = result_stage(result), shift_requires(state_dir)
                if not meets_rung(stage, rung):
                    # Done below the ticket's rung is not done: it stops the loop for a human, like blocked.
                    result = {**result, 'status': 'short', 'stage': stage, 'requires': rung,
                              'summary': f'stage {stage} below required {rung}: ' + str(result.get('summary', ''))}
            down([state_dir.name])
            note = (f"Nightshift loop: shift {state_dir.name} (attempt {attempt}) ended with status "
                    f"{result.get('status')}: {result.get('summary', '')}")
            if result.get('stage'):
                note += f" Stage: {result['stage']}."
            denials = load_denials(state_dir)
            if result.get('status') in ('blocked', 'short') and denials:
                note += ' Denials: ' + '; '.join(f"{d.get('role')}: {d.get('line')}" for d in denials)
            subprocess.run(['tasks-cli', 'note', ticket, '--heading', 'Nightshift loop', '--note', note],
                           capture_output=True, timeout=60)
            print(json.dumps({'event': 'ended', 'ticket': ticket, 'shift': state_dir.name, 'attempt': attempt, **result}),
                  flush=True)
            # An interrupted shift (crew died, or sat idle) is rerun with a fresh crew that gets the
            # handover; "blocked" is the crew asking for a human, so that stops the loop.
            if result.get('status') in ('died', 'stalled') and attempt <= args.retries:
                previous = state_dir
                print(json.dumps({'event': 'retrying', 'ticket': ticket, 'after': result.get('status')}), flush=True)
                continue
            break
        records.append({'ticket': ticket, 'status': result.get('status'), 'summary': result.get('summary', ''),
                        'handover': str(state_dir / 'handover.md') if state_dir else 'no shift started',
                        'denials': load_denials(state_dir) if state_dir else []})
        if result.get('status') != 'done':
            print(json.dumps({'event': 'stopped', 'reason': result.get('status'), 'ticket': ticket}), flush=True)
            notify(f"Nightshift: {result.get('status')} {ticket}", result.get('status'), records[-1:])
            raise SystemExit(2)
    print(json.dumps({'event': 'finished', 'tickets': tickets}), flush=True)
    notify(f"Nightshift: finished {tickets[-1] if tickets else 'batch'}", 'finished', records)
    # A finished loop closes its own Terminal window; a stopped one stays open so the human can read why.
    # Never under pytest: a crew running the suite closed the live loop's window and killed it (15 Oct).
    if 'PYTEST_CURRENT_TEST' not in os.environ:
        close_terminal_windows('Nightshift loop')


def stale_locks():
    """[(path, lock)] for locks whose shift folder is missing or whose crew is no longer alive."""
    stale = []
    for path, lock in read_locks():
        holder = SHIFTS_DIR / str(lock['shift']) if lock.get('shift') else None
        if holder is None or not holder.is_dir() or not shift_record(holder)['alive']:
            stale.append((path, lock))
    return stale


def ticket_sort_key(ticket):
    prefix, _, number = ticket.rpartition('-')
    return (prefix, int(number)) if number.isdigit() else (ticket, -1)


def resume_plan(project=None):
    """Which tickets' latest shift never reached done: {'resume': [...], 'skipped': [...], 'stale_locks': [...]}.

    The latest shift of a ticket is the one whose ticket.txt is newest (written once at start; the folder's
    own mtime moves when down writes the handover).
    """
    latest = {}
    for d in (d for d in SHIFTS_DIR.iterdir() if d.is_dir()) if SHIFTS_DIR.exists() else []:
        ticket_file = d / 'ticket.txt'
        if not ticket_file.is_file():
            continue
        ticket = ticket_file.read_text(encoding='utf-8').strip()
        if not ticket or (project and ticket.split('-')[0] != project):
            continue
        mtime = ticket_file.stat().st_mtime
        if ticket not in latest or mtime > latest[ticket][0]:
            latest[ticket] = (mtime, d)
    plan = {'resume': [], 'skipped': []}
    for ticket in sorted(latest, key=ticket_sort_key):
        d = latest[ticket][1]
        record = shift_record(d)
        if record['result'] == 'done':
            plan['skipped'].append({'ticket': ticket, 'last_shift': d.name, 'reason': 'done'})
            continue
        if record['alive']:
            plan['skipped'].append({'ticket': ticket, 'last_shift': d.name, 'reason': 'running'})
            continue
        if (d / 'handover.md').exists():
            reason = f"handover without done (result {record['result']})"
        elif record['result'] == 'none':
            reason = 'no handover'
        else:
            reason = f"result {record['result']} without handover"
        plan['resume'].append({'ticket': ticket, 'continue_from': str(d), 'last_shift': d.name,
                               'last_result': record['result'], 'reason': reason})
    plan['stale_locks'] = [{'lock': str(path), 'shift': lock.get('shift'), 'cwd': lock.get('cwd')}
                           for path, lock in stale_locks()]
    return plan


def resume(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift resume',
        description='Rerun every ticket whose latest shift never reached done (crew died, stalled, blocked or was '
                    'stopped), each continuing from that shift\'s handover. Done and still-running tickets are '
                    'skipped. Locks held by dead shifts are cleared first. Prints the plan as JSON.')
    parser.add_argument('--project', metavar='PREFIX', help='only tickets of this project, e.g. NGH')
    parser.add_argument('--dry-run', action='store_true', help='print the plan; clear no locks and start nothing')
    parser.add_argument('--stall-minutes', type=int, default=20)
    parser.add_argument('--no-notify', action='store_true', help='do not email the owner when the loop halts or finishes')
    args = parser.parse_args(argv)
    plan = resume_plan(args.project)
    print(json.dumps({'dry_run': args.dry_run, **plan}, indent=2), flush=True)
    if args.dry_run:
        return
    for entry in plan['stale_locks']:
        with contextlib.suppress(OSError):
            Path(entry['lock']).unlink()
    if plan['resume']:
        tickets = [r['ticket'] for r in plan['resume']]
        flags = ['--stall-minutes', str(args.stall_minutes)] + (['--no-notify'] if args.no_notify else [])
        loop(tickets + flags, continue_from={r['ticket']: r['continue_from'] for r in plan['resume']})


def main():
    commands = {'start': start, 'status': status, 'down': down, 'loop': loop, 'resume': resume,
                'claim': claim, 'release': release, 'claims': claims_command, 'msg': msg, 'inbox': inbox, 'ack': ack}
    if len(sys.argv) > 1 and sys.argv[1] in commands:
        commands[sys.argv[1]](sys.argv[2:])
        return
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Commands:\n  start "task"|TICKET [--dry-run] [--cwd D] [--crew NAME]\n'
               '                                              start a crew (manifest crews/NAME.yaml)\n'
               '  status [--all]                              shifts and whether their crews are alive\n'
               '  down [SHIFT|latest]                         capture panes, write handover.md, stop the crew, close its window\n'
               '  loop TICKET [TICKET...] [--stall-minutes N] one shift per ticket: start, wait for result.json, down, next\n'
               '  loop --project PREFIX [--max N]             same over a project\'s backlog, lowest number first\n'
               '  resume [--project PREFIX] [--dry-run]       rerun tickets whose latest shift never reached done (--continue-from it)\n'
               '  claim SHIFT ROLE PATH...                    claim files before editing; exit 4 if another role holds one\n'
               '  release SHIFT ROLE [PATH...]                drop those claims, or all of ROLE\'s when no paths given\n'
               '  claims SHIFT                                print the shift\'s claims as JSON\n'
               '  msg send SHIFT FROM TO --kind K --body TEXT [--asg ID]\n'
               '                                              store a crew message; prints its header line\n'
               '  msg sent SHIFT ID                           mark a stored message as sent with SendMessage\n'
               '  inbox SHIFT ROLE                            unacked inbound, unsent outbound, active assignments\n'
               '  ack SHIFT ROLE ID                           acknowledge and print the stored body; exit 5 = do not act')
    parser.add_argument('--pane', help=argparse.SUPPRESS)
    parser.add_argument('--state-dir', help=argparse.SUPPRESS)
    parser.add_argument('--start', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--cwd', default='C:/git/nightshift', help=argparse.SUPPRESS)
    parser.add_argument('--crew', help=argparse.SUPPRESS)
    parser.add_argument('--live', action='store_true', help='diagnostic ping/pong communication trial with live agents')
    parser.add_argument('--brief-file', help='with --live: replace the trial brief with this assignment file')
    args = parser.parse_args()
    if args.pane:
        pane(args.pane, args.state_dir, args.live, args.brief_file, args.start, args.cwd, args.crew)
        return
    name = 'Nightshift-trial-' + uuid.uuid4().hex[:8]
    state_dir = TRIALS_DIR / name
    state_dir.mkdir(parents=True)
    if args.live:
        install_profile(state_dir)
    command = wt_command(name, state_dir, args.live, '--live' if args.live else None, args.brief_file)
    launch(command, name, state_dir, {'live_requested': args.live})


if __name__ == '__main__':
    main()

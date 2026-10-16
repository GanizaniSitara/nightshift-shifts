"""Nightshift launcher: one Windows Terminal tab, Foreman, Advisor, Coder and Tester side by side.

`start "task"` (via C:/scripts/nightshift.cmd) persists the task to
<state_dir>/prompt.txt and starts interactive Claude Foreman, Advisor, Coder and Tester (the default crew;
Opus 5.5, the Advisor Fable 5.1) in the "Nightshift Crew" Terminal profile (Cascadia Mono 12, dark). Only Foreman
receives the task; Coder and Tester get their role and peer identity, send READY and await work; the Advisor
(NGH-027) stays dormant until Foreman consults it, and the mailbox lets only Foreman address it. The task
reaches the panes by file path only, never as argv text. Panes run in the
caller's current directory (no worktree is created) in Claude's auto permission
mode (--permission-mode auto). Each `start` is a new shift: mesh names are
<shift>-foreman / <shift>-advisor / <shift>-coder / <shift>-tester, pane titles are '<ticket or shift> <Role>' (e.g. 'NGH-019 Foreman'), a loop
window is titled 'Nightshift <first ticket>..<last ticket>', and
<state_dir>/<Role>.json records pid, name, peer and model. There is no restart or
resume; a closed pane is not relaunched. `start --dry-run` prints the plan as
JSON without launching or writing anything.
Container (NGH-025): a crew with layout 2x4 (8 slots, 2 rows x 4 columns) or 1x4 (4 equal columns) puts the crew
in the first slots and plain cmd shells in the rest (each records <state_dir>/slots/<N>.json); the launcher exits once every pane and slot has
reported. `spawn SHIFT --role ROLE [--cli claude|gemini|codex|copilot] [--crew NAME] [--slot N]` starts a session in a free
slot of that window (Foreman's only way to add a crew member; --crew takes the role from crews/NAME.yaml), and Foreman ends its own shift with `down` (--hold, the
default, keeps the tab; --close closes the window, as `loop` does). A working folder holds one live shift at a time:
start takes a lock (C:/Data/nightshift/locks), refuses with exit code 3 while a live shift holds it,
and `down` releases it.
Coders claim files before editing them (`claim`/`release`/`claims`, kept in <state_dir>/claims.json); a file
held by another role makes `claim` exit 4 and change nothing.
Crew messages go through a durable mailbox (<state_dir>/mailbox.sqlite3): `msg send`/`msg sent`, `inbox`, `ack` (exit 5 = protocol violation).
Every mailbox row and the start/spawn/result/down events are appended to <state_dir>/bus.jsonl (the shift bus, a
plain file with no process); `log SHIFT [--follow] [--since N] [--json]` prints it in order.
`build` runs heavy builds one at a time per folder (exit 6 = waited past --timeout) and records <state_dir>/builds/<n>.json.
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
CAMPBELL_BACKGROUND = '#0C0C0C'
ROLE_BACKGROUNDS = {'red': '#291719', 'purple': '#241A30', 'green': '#15291E',
                    'blue': '#172339', 'yellow': '#2B2614'}
CAMPBELL_SCHEME = {
    'background': CAMPBELL_BACKGROUND, 'foreground': '#CCCCCC', 'cursorColor': '#FFFFFF',
    'selectionBackground': '#FFFFFF', 'black': '#0C0C0C', 'red': '#C50F1F', 'green': '#13A10E',
    'yellow': '#C19C00', 'blue': '#0037DA', 'purple': '#881798', 'cyan': '#3A96DD', 'white': '#CCCCCC',
    'brightBlack': '#767676', 'brightRed': '#E74856', 'brightGreen': '#16C60C', 'brightYellow': '#F9F1A5',
    'brightBlue': '#3B78FF', 'brightPurple': '#B4009E', 'brightCyan': '#61D6D6', 'brightWhite': '#F2F2F2',
}
SHIFTS_DIR = Path('C:/Data/nightshift/shifts')
LOCKS_DIR = Path('C:/Data/nightshift/locks')
TRIALS_DIR = Path('C:/Data/nightshift/layout-trials')
SUITE_PYTHON = 'C:/miniconda3/envs/python312/python.exe'
REPO_DIR = Path(__file__).resolve().parent                 # C:/git/nightshift-shifts
SUITE_TESTS = str(REPO_DIR / 'test_nightshift_shifts.py')  # run by `down` for the handover
BRIEFS_DIR = REPO_DIR / 'briefs'                           # common.md, foreman.md, coder.md, tester.md
PERSONAS_DIR = REPO_DIR / 'personas'                       # <role>.yaml: identity, good_looks_like, how_you_work, never
CREWS_DIR = REPO_DIR / 'crews'                             # <crew>.yaml manifests; default.yaml
START_PERMISSION_MODE = 'auto'  # owner decision 2026-10-15: auto (classifier-guarded), not manual or bypass
START_MESSAGES = {'Foreman': 'Begin the assignment: the task text is at the end of your role instructions, after '
                             '"The human task for this shift follows, verbatim:".',
                  'Coder': 'Begin your Coder role: send READY to Foreman and await assignments.',
                  'Tester': 'Begin your Tester role: send READY to Foreman and await verification assignments.',
                  'Apprentice': 'Begin your Apprentice role: send READY to Foreman and await bounded assignments.',
                  'Advisor': 'You are the Advisor and dormant: do not send READY, do not poll, read or run anything; '
                             'wait silently until Foreman messages you.'}
ADVISOR = 'Advisor'   # the crew label only the human contact may message, and that answers only it (NGH-027)
MAILKEEP = 'C:/Tools/mailkeep/Mailkeep.Cli.exe'
NOTIFY_IDENTITY = 'owner_id'                # Mailkeep identity for owner@example.com
NOTIFY_TO = 'owner@example.com'        # `loop` emails the owner here when it halts or finishes
TOOLS = ('claude', 'agy', 'codex', 'copilot')   # the CLI a crew role runs (manifest `tool:`); claude is the default
# Antigravity's agy (Gemini) as installed by winget; it has no --name or system-prompt flag (NGH-012).
AGY_EXE = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'WinGet' / 'Links' / 'agy.exe'
CODEX_EXE = (Path(os.environ.get('APPDATA', '')) / 'npm' / 'node_modules' / '@openai' / 'codex' / 'node_modules'
             / '@openai' / 'codex-win32-x64' / 'vendor' / 'x86_64-pc-windows-msvc' / 'bin' / 'codex.exe')
COPILOT_EXE = (Path(os.environ.get('APPDATA', '')) / 'npm' / 'node_modules' / '@github' / 'copilot' / 'node_modules'
               / '@github' / 'copilot-win32-x64' / 'copilot.exe')
TOOL_EXES = {'agy': AGY_EXE, 'codex': CODEX_EXE, 'copilot': COPILOT_EXE}
DEFAULT_LABELS = ('Foreman', 'Coder')   # panes of the trial modes and of shifts started before crew.json existed
STAGES = ('implemented', 'tested', 'deployed', 'validated')   # the result ladder, lowest rung first
DEFAULT_REQUIRES = 'tested'
# Crew layouts and their slot counts: 'split' sizes the tab to the crew (no slots); '1x4' (4 slots) and '2x4'
# (8 slots, 2 rows x 4 columns) are containers (NGH-025): the crew fills the first slots and plain cmd shells fill the rest.
LAYOUT_SLOTS = {'split': None, '1x4': 4, '2x4': 8}
SPAWN_CLIS = {'claude': 'claude', 'gemini': 'agy', 'codex': 'codex', 'copilot': 'copilot', 'opencode': None}
AGY_MODEL = 'gemini-3.8-flash-high'      # model for a spawned agy role when no crew member names one
CODEX_MODEL = 'gpt-6.1-sol'              # default model for a spawned codex role
COPILOT_MODEL = 'gemini-3.8-flash-high'  # default model for a spawned copilot role
DEFAULT_TOOL_MODELS = {'agy': AGY_MODEL, 'codex': CODEX_MODEL, 'copilot': COPILOT_MODEL}
REASONING_LEVELS = ('low', 'medium', 'high', 'xhigh')
PERMISSIONS = ('skip', 'auto', 'default')   # a manifest role's `permissions:`; permission_flags maps them per tool
APPROVE_ALL_FLAGS = {'agy': ('--dangerously-skip-permissions',),   # as C:/scripts/launch.ps1 starts agy
                     'codex': ('--dangerously-bypass-approvals-and-sandbox',),
                     'copilot': ('--allow-all',)}


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
        # persona: names a file in personas/; without the key a role gets personas/<role>.yaml when it exists
        persona = str(entry['persona']) if entry.get('persona') else default_persona(role)
        if entry.get('persona'):
            try:
                persona_data = yaml.safe_load((PERSONAS_DIR / persona).read_text(encoding='utf-8'))
            except (OSError, yaml.YAMLError):
                persona_data = None
            if not isinstance(persona_data, dict):
                raise SystemExit(f'crew {name}: persona {persona} for {role} is not a YAML mapping in {PERSONAS_DIR}')
        count = entry.get('count', 1)
        if type(count) is not int or count < 1:
            raise SystemExit(f'crew {name}: count for {role} must be a whole number >= 1, not {count!r}')
        human = entry.get('human_contact') is True
        tool = str(entry.get('tool') or 'claude')
        if tool not in TOOLS:
            raise SystemExit(f'crew {name}: tool {tool!r} for {role} is not supported; use one of: {", ".join(TOOLS)}')
        role_dict = {'role': role, 'brief': brief, 'persona': persona, 'count': count, 'human_contact': human,
                     'colour': str(entry.get('colour') or ('red' if human else 'green')),
                     'tool': tool, 'model': str(entry.get('model') or '')}
        if entry.get('reasoning') is not None:
            reasoning = str(entry['reasoning'])
            if reasoning not in REASONING_LEVELS:
                raise SystemExit(f"crew {name}: reasoning {reasoning!r} for {role} must be one of: "
                                 f"{', '.join(REASONING_LEVELS)}")
            role_dict['reasoning'] = reasoning
        if entry.get('account') is not None:
            if entry['account'] not in (1, 2):
                raise SystemExit(f"crew {name}: account for {role} must be 1 or 2, not {entry['account']!r}")
            role_dict['account'] = entry['account']
        if entry.get('permissions') is not None:
            permissions = str(entry['permissions'])
            if permissions not in PERMISSIONS:
                raise SystemExit(f"crew {name}: permissions {permissions!r} for {role} must be one of: "
                                 f"{', '.join(PERMISSIONS)}")
            role_dict['permissions'] = permissions
        checked.append(role_dict)
    humans = [r['role'] for r in checked if r['human_contact']]
    if len(humans) != 1:
        raise SystemExit(f'crew {name}: exactly one role needs human_contact: true, found {len(humans)}')
    if next(r for r in checked if r['human_contact'])['count'] != 1:
        raise SystemExit(f'crew {name}: the human_contact role {humans[0]} must have count 1')
    panes = sum(r['count'] for r in checked)
    if not 2 <= panes <= 8:
        raise SystemExit(f'crew {name}: {panes} panes; a crew has 2 to 8')
    layout = data.get('layout', 'split')
    if layout not in LAYOUT_SLOTS:
        raise SystemExit(f"crew {name}: layout {layout!r} is not supported; use one of: {', '.join(LAYOUT_SLOTS)}")
    slots = LAYOUT_SLOTS[layout]
    if slots is not None and panes > slots:
        raise SystemExit(f'crew {name}: {panes} panes do not fit layout {layout} ({slots} slots)')
    model = str(data.get('model') or MODEL)
    for r in checked:
        r['model'] = r['model'] or model   # a role without its own model: runs the crew model
    return {'name': name, 'model': model,
            'permission_mode': str(data.get('permission_mode') or START_PERMISSION_MODE),
            'contract': str(data.get('contract') or 'result.json'), 'layout': layout, 'slots': slots, 'roles': checked}


def crew_members(crew, shift):
    """Panes in launch order: [{role, label, identity, brief, colour, human_contact, tool, model, peer}].

    A role with count 1 is labelled Role and named <shift>-role; count N gives Role-1..N / <shift>-role-1..N.
    The human-contact member's peer is every other member (comma-separated); everyone else's is it.
    """
    members = []
    for r in crew['roles']:
        for n in range(1, r['count'] + 1):
            suffix = f'-{n}' if r['count'] > 1 else ''
            member = {'role': r['role'], 'label': r['role'] + suffix,
                      'identity': f"{shift}-{r['role'].lower()}{suffix}", 'brief': r['brief'],
                      'persona': r.get('persona'),
                      'colour': r['colour'], 'human_contact': r['human_contact'],
                      'tool': r['tool'], 'model': r['model']}
            for opt in ('reasoning', 'account', 'permissions'):
                if opt in r:
                    member[opt] = r[opt]
            members.append(member)
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


def default_persona(role):
    """personas/<role lower>.yaml when that file exists, else None (a custom role without one gets no persona)."""
    name = f'{role.lower()}.yaml'
    return name if (PERSONAS_DIR / name).is_file() else None


def persona_text(role, persona):
    """The persona file PERSONAS_DIR/<persona> as markdown: a heading naming the role, the identity paragraph,
    then What good looks like / How you work / What you never do as bullet lists, closed by a Protocol heading."""
    data = yaml.safe_load((PERSONAS_DIR / persona).read_text(encoding='utf-8')) or {}
    text = f'# {role}\n\n' + str(data.get('identity') or '').strip() + '\n\n'
    for key, heading in (('good_looks_like', 'What good looks like'), ('how_you_work', 'How you work'),
                         ('never', 'What you never do')):
        items = data.get(key) or []
        if items:
            text += f'## {heading}\n\n' + ''.join(f'- {str(i).strip()}\n' for i in items) + '\n'
    return text + '---\n\n# Protocol\n\n'


def role_brief(role, identity, peer_name, state_dir='', brief=None, persona=None):
    """Crew instructions: the role's persona (PERSONAS_DIR/<persona>, default <role>.yaml when it exists; none
    otherwise) rendered first, then BRIEFS_DIR/common.md + the role's brief file (default <role>.md), with
    {role}/{identity}/{peer_name}/{state_dir}/{shift} filled in ({shift} is the state folder's name, '' without one).
    Every CLI (claude, agy, codex, copilot) gets its brief from here.

    Edit those files to change how a crew behaves; nothing here needs touching.
    """
    persona = persona or default_persona(role)
    text = persona_text(role, persona) if persona else ''
    for name in ('common.md', brief or role.lower() + '.md'):
        text += (BRIEFS_DIR / name).read_text(encoding='utf-8').strip() + '\n\n'
    shift = Path(state_dir).name if state_dir else ''
    # Plain replace, not str.format: the briefs contain literal JSON braces.
    # {nightshift} is the exact command: the crews' shell is Git Bash, where the nightshift.cmd shim
    # does not resolve by bare name, and forward slashes because Git Bash strips unquoted backslashes.
    nightshift_cmd = f'{SUITE_PYTHON} {(REPO_DIR / "nightshift_shifts.py").as_posix()}'
    state_posix = Path(state_dir).as_posix() if state_dir else ''
    for key, value in (('{role}', role), ('{identity}', identity), ('{peer_name}', peer_name),
                       ('{state_dir}', state_posix), ('{shift}', shift), ('{nightshift}', nightshift_cmd)):
        text = text.replace(key, value)
    return text


def crew_env(environ, tool='claude', account=None):
    """Environment for a crew member's CLI process.

    Drops inherited Claude Code markers: when start is run from inside a Claude session the crew
    would otherwise become child sessions of it (name inherited, transcript saving off, no mesh
    registration). CLAUDE_AFK_* are the user's own settings (AFK auto-answer disabled) and stay.
    Colour is forced on because a parent Codex shell can carry NO_COLOR=1 / TERM=dumb (NGH-002).
    """
    env = {k: v for k, v in environ.items()
           if not k.upper().startswith('CLAUDE') or k.upper().startswith('CLAUDE_AFK_')}
    env.pop('NO_COLOR', None)
    env.update(TERM='xterm-256color', COLORTERM='truecolor', FORCE_COLOR='3')
    if tool == 'codex':
        env.pop('TERM', None)   # native Windows cmd.exe: TERM can make Codex stop for confirmation
        if account == 2:
            env['CODEX_HOME'] = str(Path.home() / '.codex-second')
    return env


def permission_flags(tool, permissions):
    """The CLI flags for a role's `permissions:` (skip | auto | default) on `tool` - the only place they are set.

    Claude: skip = --dangerously-skip-permissions, default = nothing, anything else = --permission-mode <it>
    (auto, or a crew-level permission_mode such as plan). The other CLIs have no classifier mode, so auto
    means approve-all like skip: agy --dangerously-skip-permissions, codex --dangerously-bypass-approvals-and-sandbox,
    copilot --allow-all; default or any other value adds nothing.
    """
    permissions = permissions or START_PERMISSION_MODE
    if tool == 'claude':
        if permissions == 'skip':
            return ['--dangerously-skip-permissions']
        return [] if permissions == 'default' else ['--permission-mode', permissions]
    return list(APPROVE_ALL_FLAGS[tool]) if permissions in ('skip', 'auto') else []


def agy_command(model, brief, first_message, permissions=None):
    """argv for an agy (Gemini) crew member: agy has no --name or system-prompt flag, so the brief is the prompt."""
    return [str(AGY_EXE), '--model', model, *permission_flags('agy', permissions), '-i', brief + '\n\n' + first_message]


def codex_command(model, brief, first_message, reasoning=None, permissions=None):
    """argv for a Codex crew member: approvals per `permissions`, no daemon, static terminal title, optional reasoning."""
    cmd = [str(CODEX_EXE), *permission_flags('codex', permissions), '--no-daemon',
           '-c', 'tui.terminal_title=[]',
           '-c', 'tui.status_line=["thread-title","model-with-reasoning","context-remaining"]', '--model', model]
    if reasoning:
        cmd += ['-c', f'model_reasoning_effort={reasoning}']
    return cmd + [brief + '\n\n' + first_message]


def copilot_command(model, brief, first_message, permissions=None):
    """argv for a Copilot CLI crew member: tools per `permissions`, no auto-update, interactive initial prompt."""
    return [str(COPILOT_EXE), *permission_flags('copilot', permissions), '--no-auto-update', '--model', model,
            '-i', brief + '\n\n' + first_message]


def register_name(pid, identity):
    """Name a non-Claude pane in agents-cli so Foreman can send/wait/tail it by identity; failures are ignored."""
    try:
        subprocess.run(['agents-cli', 'name', str(pid), identity], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


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


def peek_lines(pid, timeout=10):
    """Visible pane lines via agents-cli, [] when unreadable."""
    try:
        peek = subprocess.run(['agents-cli', '_attach', 'peek', str(pid), '0'], capture_output=True, timeout=timeout)
        if peek.returncode:
            return []
        result = json.loads((peek.stdout or b'{}').decode('utf-8', 'replace'))
        return result.get('lines', []) if isinstance(result, dict) and not result.get('error') else []
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


def codex_prompt_is_empty(lines):
    """Recognize the current bottom composer, including while a Codex turn is running.

    Unknown hints fail closed. Transcript prompts, wrapped drafts and dialogs are not input readiness.
    The thread-title footer may be arbitrary text, clipped before the model/context fields.
    """
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return False
    lines = [line.rstrip() for line in lines]
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) < 3:
        return False
    # Codex indents its bottom status row. Do not constrain the title's content: before /rename
    # it may be a UUID or an automatically generated title, with no room left for other fields.
    if not lines[-1].startswith('  '):
        return False
    prompts = [i for i, line in enumerate(lines) if line.lstrip().startswith('›')]
    if not prompts:
        return False
    prompt = prompts[-1]
    if prompt < len(lines) - 6 or prompt >= len(lines) - 2:
        return False
    if lines[prompt].strip() not in ('›', '› Ask Codex to do anything'):
        return False
    if any(line.strip() for line in lines[prompt + 1:-1]):
        return False
    # Busy status is allowed: /rename is handled locally without interrupting the active turn.
    tail = '\n'.join(lines[max(0, prompt - 4):])
    return not re.search(r'(?i)(?:\(y/n\)|\[y/n\]|press enter to confirm|do you trust|'
                         r'would you like to|allow once|approve|select an option|^\s*[›❯]?\s*\d+\. )',
                         tail, re.M)


def set_codex_role_name(child, role, identity, tries=30):
    """Send /rename once after two empty-composer reads, bounded to 30 seconds including I/O."""
    deadline = time.monotonic() + 30
    for _ in range(tries):
        if child.poll() is not None or time.monotonic() >= deadline:
            return False
        try:
            lines = peek_lines(child.pid, timeout=min(10, deadline - time.monotonic()))
            if codex_prompt_is_empty(lines):
                if child.poll() is not None or time.monotonic() >= deadline:
                    return False
                lines = peek_lines(child.pid, timeout=min(10, deadline - time.monotonic()))
                if codex_prompt_is_empty(lines):
                    if child.poll() is not None or time.monotonic() >= deadline:
                        return False
                    sent = subprocess.run(
                        ['agents-cli', '_attach', 'inject', str(child.pid), '1', f'/rename {role} ({identity})'],
                        capture_output=True, timeout=min(10, deadline - time.monotonic()))
                    return sent.returncode == 0  # never retry an uncertain submission
        except (OSError, subprocess.TimeoutExpired):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.5, remaining))
    return False


def set_pane_background(background):
    """Set this Windows Terminal pane's default background, including a reused slot."""
    if os.name != 'nt' or not sys.stdout.isatty():
        return False
    try:
        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.restype = ctypes.c_void_p
        kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if not kernel.SetConsoleMode(handle, mode.value | 4):
            return False
        print(f'\033]11;{background}\033\\', end='', flush=True)
        return True
    except OSError:
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


def read_crew_json(state_dir):
    """<state_dir>/crew.json as a dict with a members list, None when missing or unreadable."""
    try:
        saved = json.loads(Path(state_dir, 'crew.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) and isinstance(saved.get('members'), list) else None


def write_slot(state_dir, slot, **fields):
    """Record <state_dir>/slots/<slot>.json: {slot, started, role: None, ...fields} (fields win)."""
    slots_dir = Path(state_dir) / 'slots'
    slots_dir.mkdir(parents=True, exist_ok=True)
    write_json(slots_dir / f'{slot}.json', {'started': time.time(), 'role': None, **fields, 'slot': slot})


def read_slot(state_dir, slot):
    """<state_dir>/slots/<slot>.json as a dict, None when missing or unreadable."""
    try:
        record = json.loads((Path(state_dir) / 'slots' / f'{slot}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def slot_shell(slot, state_dir):
    """The --slot step of a slot shell: record the cmd.exe that runs it (our parent) and exit to its prompt.

    A role already recorded in the slot (a crew pane whose member died) is kept, so `spawn` can put that
    role back into its own slot and `status` shows it as dead rather than empty."""
    kept = {k: v for k, v in (read_slot(state_dir, slot) or {}).items() if k in ('role', 'identity', 'spawned')}
    write_slot(state_dir, slot, **kept, cmd_pid=os.getppid())


def slot_state(state_dir, slot):
    """What slot N holds: 'empty' (a cmd shell, no role), 'dead' (a cmd shell whose role's member has exited),
    'live' (its member runs), 'starting' (spawned, the wrapper has not written <role>.json yet) or 'gone' (no
    record, or no shell left to type into). Only empty and dead slots can be spawned into."""
    record = read_slot(state_dir, slot)
    if record is None:
        return 'gone'
    shell = bool(record.get('cmd_pid')) and process_running(record['cmd_pid'])
    role = record.get('role')
    if role is None:
        return 'empty' if shell else 'gone'
    info = member_info(state_dir, role)
    # spawn stamps 'spawned'; a <role>.json older than that belongs to the previous incarnation
    if not info.get('pid') or info.get('started', 0) < record.get('spawned', 0):
        return 'starting'
    if pid_alive(info['pid'], info.get('started')):
        return 'live'
    return 'dead' if shell else 'gone'


def foreman_claude_settings(cwd):
    """Deny built-in repository edits, leaving external shift state and shell commands available."""
    repo = Path(cwd).resolve().as_posix().rstrip('/')
    # Claude normalizes C:/repo to /c/repo; // anchors a permission rule at the filesystem root.
    repo = re.sub(r'^([A-Za-z]):', lambda match: '/' + match[1].lower(), repo)
    repo = re.sub(r'([*?\[\]])', r'\\\1', repo)   # literal directory names, not gitignore globs
    # Edit(path) covers Write, NotebookEdit and legacy MultiEdit too. Their own path rules are ignored.
    # This is not a sandbox: arbitrary subprocesses can still write files (git commit must remain usable).
    return {'permissions': {'deny': [f'Edit(/{repo}/**)']}}


def pane(role, state_dir, live=False, brief_file=None, start=False, cwd='C:/git/nightshift', crew=None, slot=None):
    if live or start:
        model, permission_mode, tool = MODEL, START_PERMISSION_MODE, 'claude'
        reasoning, account = None, None
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
            # `role` is the pane label (Foreman, Coder-2, ...); everything else comes from <state_dir>/crew.json
            # (which also holds members added by `spawn`), else from the crew manifest.
            saved = read_crew_json(state_dir)
            if saved:
                members = saved['members']
                crew_name, permission_mode = saved.get('crew', 'default'), saved.get('permission_mode')
            else:
                crew_def = load_crew(crew or 'default')
                members = crew_members(crew_def, shift)
                crew_name, permission_mode = crew_def['name'], crew_def['permission_mode']
            member = next((m for m in members if m['label'] == role), None)
            if member is None:
                raise SystemExit(f'pane {role} is not a member of crew {crew_name}')
            identity, peer_name, colour = member['identity'], member['peer'], member['colour']
            model, tool = member['model'], member['tool']
            reasoning, account = member.get('reasoning'), member.get('account')
            # a role's own permissions: (skip | auto | default) wins over the crew-level permission_mode
            permission_mode = member.get('permissions') or permission_mode or START_PERMISSION_MODE
            first_message = opening_message(member)
            brief = role_brief(member['role'], identity, peer_name, state_dir, member['brief'], member.get('persona'))
            if member['human_contact']:
                brief += Path(state_dir, 'prompt.txt').read_text(encoding='utf-8')
        if tool == 'agy':
            command = agy_command(model, brief, first_message, permissions=permission_mode)
        elif tool == 'codex':
            command = codex_command(model, brief, first_message, reasoning=reasoning, permissions=permission_mode)
        elif tool == 'copilot':
            command = copilot_command(model, brief, first_message, permissions=permission_mode)
        else:
            exe = Path('C:/nvm4w/nodejs/node_modules/@anthropic-ai/claude-code/bin/claude.exe')
            command = [str(exe), '--name', identity, '--model', model,
                       *permission_flags('claude', permission_mode), '--append-system-prompt', brief]
            if start and member['role'].lower() == 'foreman':
                command += ['--settings', json.dumps(foreman_claude_settings(cwd))]
            if reasoning:
                command += ['--effort', reasoning]
            command.append(first_message)
        env = crew_env(os.environ) if tool == 'claude' and not account else crew_env(os.environ, tool=tool, account=account)
        set_pane_background(ROLE_BACKGROUNDS.get(colour, CAMPBELL_BACKGROUND)
                            if tool == 'codex' else CAMPBELL_BACKGROUND)
        try:
            child = subprocess.Popen(command, cwd=cwd, env=env)
            record = {'pid': child.pid, 'wrapper_pid': os.getpid(), 'role': role, 'started': time.time(),
                      'name': identity, 'peer': peer_name, 'model': model, 'cwd': cwd}
            if start:
                record['tool'] = tool
            previous = member_info(state_dir, role)
            if previous:
                # earlier incarnations of this role; 'started' stays the current one, so mailbox fencing is unchanged
                record['history'] = previous.get('history', []) + [{k: v for k, v in previous.items() if k != 'history'}]
            Path(state_dir, role + '.json').write_text(json.dumps(record), encoding='utf-8')
            if slot:
                # keep what is already there (a spawned slot's cmd_pid) and add who runs in it
                kept = {k: v for k, v in (read_slot(state_dir, slot) or {}).items() if k != 'slot'}
                write_slot(state_dir, slot, **{**kept, 'role': role, 'identity': identity, 'wrapper_pid': os.getpid()})
            if tool != 'claude':
                register_name(child.pid, identity)   # the trust dialog and /color are Claude-only
                if tool == 'codex':
                    set_codex_role_name(child, role, identity)
            else:
                accept_trust_prompt(child)
                set_role_colour(child, colour, cwd)
            code = child.wait()
        finally:
            if tool == 'codex':
                set_pane_background(CAMPBELL_BACKGROUND)
        print(f'{role} exited with code {code}.')
        if start and slot:
            # A container slot must leave a cmd prompt behind for `spawn` to type into. A spawned wrapper was
            # typed into a slot shell: returning lands at its prompt. A crew pane wt launched directly has none,
            # so it becomes one (slot_shell keeps the dead role in the slot record).
            cmd_pid = (read_slot(state_dir, slot) or {}).get('cmd_pid')
            if not (cmd_pid and process_running(cmd_pid)):
                subprocess.run(['cmd.exe', '/k', sys.executable, str(Path(__file__).resolve()),
                                '--slot', str(slot), '--state-dir', str(state_dir)])
            return
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
    schemes = config.setdefault('schemes', [])
    campbell = {**CAMPBELL_SCHEME, **next((s for s in schemes if s.get('name') == 'Campbell'), {})}
    for colour, background in ROLE_BACKGROUNDS.items():
        desired = {**campbell, 'name': 'Nightshift ' + colour, 'background': background}
        existing = next((s for s in schemes if s.get('name') == desired['name']), None)
        if existing is None:
            schemes.append(desired)
        else:
            existing.clear()
            existing.update(desired)
    (state_dir / 'terminal-settings-before.json').write_text(original, encoding='utf-8')
    settings.write_text(json.dumps(config, indent=4) + '\n', encoding='utf-8')


def layout_description(n):
    """Human wording of the split layout for n panes (launch prints it as requested_layout)."""
    if n <= 4:
        return f'1 row x {n} columns'
    top = math.ceil(n / 2)
    return f'2 rows: {top} columns over {n - top}'


def pane_title(label):
    """Start-mode pane title, the same for every pane and slot of the container: '<ticket or shift> shift', e.g.
    'NGH-025 shift' (owner, 16 Oct; NGH-028). Shifts before that titled panes '<ticket or shift> <Role>'."""
    return f'{label} shift'


def loop_title(tickets):
    """The loop's console title: 'Nightshift NGH-019..NGH-020', or 'Nightshift NGH-019' for one ticket."""
    if not tickets:
        return 'Nightshift loop'
    return 'Nightshift ' + tickets[0] + (f'..{tickets[-1]}' if len(tickets) > 1 else '')


def wt_command(name, state_dir, agents, mode_flag=None, brief_file=None, cwd=None, members=DEFAULT_LABELS,
               crew='default', ticket=None, slots=None, codex_colours=None):
    """wt.exe argv for one tab holding `members` (pane labels, in order).

    2 panes: side by side, focus left (unchanged since the first crew). 3-4: one row of equal columns,
    each new pane split off the last. 5-8: top row ceil(n/2) panes, bottom row the rest.
    With `slots` (a 1x4 crew) the tab has that many cells: members fill slots 1..k (each wrapper gets
    --slot N) and slots k+1.. run `cmd.exe /k <python> <script> --slot N`, an empty shell `spawn` can fill.
    """
    command = ['wt.exe', '-w', '-1', '--pos', '0,0', '--maximized']
    script = str(Path(__file__).resolve())

    def add_empty(parts, slot):
        if len(command) > 6:
            command.append(';')
        command.extend(parts)
        if agents:
            command.extend(['--profile', PROFILE])
        command.extend(['--title', pane_title(ticket or name), '--suppressApplicationTitle',
                        '-d', '%USERPROFILE%', 'cmd.exe', '/k', sys.executable, script,
                        '--slot', str(slot), '--state-dir', str(state_dir)])

    def place(parts, i):
        """Cell i (0-based): a crew member, or an empty slot past the last member."""
        if i < len(members):
            add(parts, members[i])
            if slots:
                command.extend(['--slot', str(i + 1)])
        else:
            add_empty(parts, i + 1)

    def add(parts, role=None):
        if len(command) > 6:
            command.append(';')
        command.extend(parts)
        if role:
            if agents:
                command.extend(['--profile', PROFILE])
                colour = (codex_colours or {}).get(role)
                if mode_flag == '--start' and colour in ROLE_BACKGROUNDS:
                    command.extend(['--colorScheme', 'Nightshift ' + colour])
            # start mode shows '<ticket or shift> shift'; the trial modes keep the shift name in the title.
            title = pane_title(ticket or name) if mode_flag == '--start' else 'Nightshift | ' + role + ' | ' + name
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

    def columns(cells):
        # Each new pane takes (remaining-1)/remaining of the last one, so all end up equal.
        for i, cell in enumerate(cells, 1):
            left = len(cells) + 1 - i
            place(['split-pane', '-V', '-s', f'{left / (left + 1):.4g}'], cell)

    n = slots or len(members)
    place(['new-tab'], 0)
    if n == 2:
        place(['split-pane', '-V', '-s', '0.5'], 1)
        add(['move-focus', 'left'])
        return command
    if n <= 4:
        columns(range(1, n))
    else:
        top = math.ceil(n / 2)
        place(['split-pane', '-H', '-s', '0.5'], top)
        add(['move-focus', 'up'])
        columns(range(1, top))
        add(['move-focus', 'down'])
        columns(range(top + 1, n))
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


def launch(command, name, state_dir, extra, labels=DEFAULT_LABELS, slots=None):
    """Run wt.exe, remember the new window, wait for every pane to report, print, return. The launcher then
    exits: nothing polls the shift afterwards (NGH-025); with `slots`, every slots/<N>.json must report too."""
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
    expected = [(label, state_dir / (label + '.json')) for label in labels]
    expected += [(f'slot {n}', state_dir / 'slots' / f'{n}.json') for n in range(1, (slots or 0) + 1)]
    pending = [label for label, _ in expected]
    for _ in range(60):
        pending = [label for label, path in expected if not path.exists()]
        if not pending:
            break
        time.sleep(0.5)
    print(json.dumps({'window_name': name, 'state_dir': str(state_dir),
                      'requested_layout': layout_description(slots or len(labels)),
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
        description='Start a Nightshift crew: Claude Foreman, Advisor, Coder and Tester side by side in one Windows Terminal tab '
                    '(the default crew; --crew NAME uses crews/NAME.yaml for other roles and pane counts). '
                    'The task is saved to <state_dir>/prompt.txt and given to Foreman only; Coder and Tester send '
                    'READY and wait for Foreman to delegate work; the Advisor stays dormant until Foreman consults it. Panes run in the current directory (no worktree is '
                    'created) in Claude auto permission mode. Each start is a new shift with its own '
                    '<shift>-foreman / <shift>-advisor / <shift>-coder / <shift>-tester session names; there is no restart or resume, and a closed '
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
                             '(default: default = one Foreman, one Advisor, one Coder, one Tester)')
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
    if ticket is None:
        header = re.match(r'Ticket (' + TICKET_RE.pattern + r'):', prompt.splitlines()[0])
        if header:
            ticket = header.group(1)
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
    command = wt_command(name, state_dir, agents=True, mode_flag='--start', cwd=cwd, members=labels, crew=crew['name'],
                         ticket=ticket, slots=crew['slots'],
                         codex_colours={m['label']: m['colour'] for m in members if m['tool'] == 'codex'})
    if args.dry_run:
        plan = {
            'dry_run': True, 'shift': name, 'state_dir': str(state_dir), 'cwd': cwd, 'ticket': ticket,
            'prompt_file': str(state_dir / 'prompt.txt'), 'model': crew['model'], 'profile': PROFILE,
            'permission_mode': crew['permission_mode'], 'lock': str(lock_path(cwd)),
            'roles': {m['label']: {'session_name': m['identity'], 'pane_title': pane_title(ticket or name),
                                   'peer': m['peer'],
                                   'receives_prompt': m['human_contact'], 'first_message': opening_message(m),
                                   # Claude roles on the crew model keep the pre-manifest shape; a Claude role on
                                   # another model (the Fable Advisor) shows it; other tools show what will run
                                   **({'tool': m['tool'], 'model': m['model'], 'exe': str(TOOL_EXES[m['tool']])}
                                      if m['tool'] != 'claude' else
                                      {'model': m['model']} if m['model'] != crew['model'] else {}),
                                   **({'reasoning': m['reasoning']} if 'reasoning' in m else {}),
                                   **({'account': m['account']} if 'account' in m else {}),
                                   'permission_flags': permission_flags(
                                       m['tool'], m.get('permissions') or crew['permission_mode'])}
                      for m in members}}
        if crew['slots']:
            # The container (NGH-025): which slot holds which crew pane, and the empty ones `spawn` can fill.
            cells = labels + [None] * (crew['slots'] - len(labels))
            plan['slots'] = [{'slot': n, 'role': role, 'pane_title': pane_title(ticket or name)}
                             for n, role in enumerate(cells, 1)]
        plan.update(wt_argv=command, prompt=prompt)
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
    (state_dir / 'crew.json').write_text(json.dumps({'crew': crew['name'], 'members': members, 'slots': crew['slots'],
                                                     'permission_mode': crew['permission_mode'], 'model': crew['model'],
                                                     'cwd': cwd}, indent=2), encoding='utf-8')
    if ticket:
        (state_dir / 'ticket.txt').write_text(ticket, encoding='utf-8')
    bus_append(state_dir, 'start', shift=name, ticket=ticket, cwd=cwd, crew=crew['name'], members=labels)
    install_profile(state_dir)
    launch(command, name, state_dir, {'mode': 'start', 'prompt_file': str(state_dir / 'prompt.txt')}, labels,
           crew['slots'])
    return state_dir


def process_start_epoch(pid):
    """Creation time of a process as epoch seconds (local clock), or None if it does not exist.

    None also when wmic does not answer in time: the loop died on 16 Oct from an uncaught
    TimeoutExpired here, and an unknown start time must mean "cannot verify", not "crash".
    """
    try:
        out = subprocess.run(['wmic', 'process', 'where', f'processid={pid}', 'get', 'creationdate'],
                             capture_output=True, timeout=15).stdout.decode('utf-8', 'replace').replace('\x00', '')
    except subprocess.TimeoutExpired:
        return None
    match = re.search(r'(\d{14})\.\d+', out)
    if not match:
        return None
    return time.mktime(time.strptime(match.group(1), '%Y%m%d%H%M%S'))


def pid_alive(pid, started=None):
    """True if this PID is a crew CLI process (claude.exe, node.exe, agy.exe, codex.exe or copilot.exe)
    started when the crew member was (CSV PID field compared whole).

    Windows reuses PIDs, so a dead crew member's PID can turn up on another process - even another
    Claude session - and make a finished shift look alive; the start time settles it.
    """
    out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                         capture_output=True, timeout=15).stdout.decode('utf-8', 'replace')
    for line in out.splitlines():
        if line.startswith('"'):
            fields = line.strip('"').split('","')
            if fields[1:2] == [str(pid)] and fields[0].lower() in ('claude.exe', 'node.exe', 'agy.exe',
                                                                   'codex.exe', 'copilot.exe'):
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
            'tool': info.get('tool', 'claude'),
            'alive': pid_alive(pid, info['started']) if 'started' in info else pid_alive(pid),
            'registered': (sessions / f'{pid}.json').exists(),
        }
    record['alive'] = any(r['alive'] for r in record['roles'].values())
    slots = (read_crew_json(state_dir) or {}).get('slots')
    if slots:
        # the container's cells; empty and dead slots are the ones `spawn` can fill
        record['slots'] = []
        for n in range(1, slots + 1):
            state = slot_state(state_dir, n)
            record['slots'].append({'slot': n, 'role': (read_slot(state_dir, n) or {}).get('role'), 'state': state,
                                    'reusable': state in ('empty', 'dead')})
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
    lines += ['', '## Builds', '']
    builds = load_builds(state_dir)
    lines += [f"- #{b.get('n')} {b.get('status')} exit={b.get('exit')} head={(b.get('head') or '-')[:8]} "
              f"dirty={b.get('dirty')} {b.get('cmd')}" for b in builds] if builds else ['(none)']
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


def parent_pids():
    """{pid: parent pid} for every process, from one Toolhelp snapshot; {} when the snapshot fails."""
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', ctypes.c_ulong), ('cntUsage', ctypes.c_ulong), ('th32ProcessID', ctypes.c_ulong),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', ctypes.c_ulong),
                    ('cntThreads', ctypes.c_ulong), ('th32ParentProcessID', ctypes.c_ulong),
                    ('pcPriClassBase', ctypes.c_long), ('dwFlags', ctypes.c_ulong), ('szExeFile', ctypes.c_wchar * 260)]
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return {}
    parents = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        ok = kernel32.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
        while ok:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel32.Process32NextW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snapshot))
    return parents


def own_ancestors():
    """PIDs of this process and every process above it (parent, grandparent, ...)."""
    parents, seen, pid = parent_pids(), set(), os.getpid()
    while pid and pid not in seen:
        seen.add(pid)
        pid = parents.get(pid)
    return seen


def close_target(state_dir):
    """The shift's open Terminal window (hwnd) for `down --close`, or None."""
    launch_record = json.loads((state_dir / 'launch.json').read_text(encoding='utf-8')) if (state_dir / 'launch.json').exists() else {}
    hwnd = launch_record.get('hwnd')
    windows = terminal_windows()
    if not hwnd:
        # Shift started before the hwnd was recorded: fall back to the one window titled by this shift's panes
        # ('<ticket or shift> shift'; older shifts '<ticket or shift> <Role>', or a bare 'Foreman'/'Coder').
        ticket_file = state_dir / 'ticket.txt'
        label = (ticket_file.read_text(encoding='utf-8').strip() if ticket_file.is_file() else '') or state_dir.name
        titles = {pane_title(label)} | {f"{label} {m['label']}" for m in crew_roster(state_dir)} | {'Foreman', 'Coder'}
        candidates = [h for h, title in windows.items() if title in titles]
        hwnd = candidates[0] if len(candidates) == 1 else None
    return hwnd if hwnd and hwnd in windows else None


def down(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift down',
        description='Stop a crew: capture each pane, write handover.md, end the sessions and release the folder lock. '
                    '--hold (default) leaves the tab open; --close also closes the window. When run from inside the '
                    'crew (Foreman ending its own shift), its own process tree is killed last, after the output.')
    parser.add_argument('shift', nargs='?', default='latest', help='shift name, unique suffix, or "latest" (default)')
    keep = parser.add_mutually_exclusive_group()
    keep.add_argument('--hold', dest='close', action='store_false', help='leave the Terminal tab open (default)')
    keep.add_argument('--close', dest='close', action='store_true', help="also close the shift's Terminal window")
    parser.set_defaults(close=False)   # without it the shared dest takes --hold's store_false default (True)
    args = parser.parse_args(argv)
    state_dir = resolve_shift(args.shift)
    record = shift_record(state_dir)
    tails = {role: pane_tail(info['pid']) for role, info in record['roles'].items() if info['alive']}
    record_denials(state_dir, tails)
    # When Foreman already ran `down` (--hold) and a caller later runs `down --close`, the crew is already gone:
    # keep the handover captured while the panes were live rather than overwriting it with '(pane not readable)'.
    if record['alive'] or not (state_dir / 'handover.md').exists():
        handover = [f"# Handover: {record['shift']}", '', f"Task: {record['task']}",
                    f"Stopped: {time.strftime('%Y-%m-%d %H:%M')}", '']
        handover += handover_report(state_dir, record)
        for role, info in record['roles'].items():
            handover += [f"## {role} ({info['name']}, pid {info['pid']}, {'alive' if info['alive'] else 'gone'})", '',
                         '```', *(tails.get(role, []) or ['(pane not readable)']), '```', '']
        (state_dir / 'handover.md').write_text('\n'.join(handover), encoding='utf-8')
    # The member whose process tree runs this `down` (Foreman ending its own shift) is killed last, after the
    # lock is released and the output printed; killing it first would end `down` half-way.
    mine = own_ancestors()
    kills, last = [], []
    for role, info in record['roles'].items():
        pids = [pid for pid in (info['pid'], info.get('wrapper_pid')) if pid]
        (last if any(pid in mine for pid in pids) else kills).extend(pids)
    for pid in kills:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True, timeout=30)
    released = release_lock(record['shift'])
    stopped = [r for r, i in record['roles'].items() if i['alive']]
    # The bus's last words, before the self-kill below: the crew's result.json as it stands, then 'down'.
    result_file = state_dir / 'result.json'
    if result_file.exists():
        try:
            bus_append(state_dir, 'result', result=json.loads(result_file.read_text(encoding='utf-8')))
        except (OSError, ValueError) as err:
            bus_append(state_dir, 'result', result=None, error=f'result.json unreadable: {err}')
    bus_append(state_dir, 'down', stopped=stopped, lock_released=released)
    hwnd = close_target(state_dir) if args.close else None
    print(json.dumps({'shift': record['shift'], 'handover': str(state_dir / 'handover.md'),
                      'stopped': stopped,
                      'window_closed': bool(hwnd), 'held': not args.close, 'lock_released': released}), flush=True)
    if hwnd:
        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)   # WM_CLOSE on that window only
    for pid in last:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True, timeout=30)


def spawn(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift spawn',
        description="Start a crew session in a free slot of a live shift's own window (2x4/1x4 container, NGH-025): "
                    'adds the role to crew.json and types the pane wrapper into that slot\'s cmd shell. Refused when '
                    'the window is gone, no slot is free, or the role is already live. Foreman only.')
    parser.add_argument('shift', help='shift name, unique suffix, or "latest"')
    parser.add_argument('--role', required=True, help='role label, e.g. Advisor or Coder2 (letters, digits, _)')
    parser.add_argument('--cli', choices=list(SPAWN_CLIS),
                        help='the CLI to run: claude (default), gemini (agy), codex or copilot; opencode is not wired '
                             'yet. Overrides the tool --crew would give')
    parser.add_argument('--crew', help="take the role's brief, colour, tool, model, reasoning and account from "
                                       "crews/NAME.yaml instead of the shift's own manifest (e.g. apprentice-codex)")
    parser.add_argument('--slot', type=int, help='slot number (default: the first free one)')
    args = parser.parse_args(argv)
    tool = SPAWN_CLIS[args.cli or 'claude']
    if tool is None:
        raise SystemExit(f'--cli {args.cli} is not yet wired (NGH-022)')
    role = args.role
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', role):
        raise SystemExit(f'role name {role!r} must be letters, digits or _ (it names files and panes)')
    crew_role = None   # --crew NAME: the role as manifest NAME defines it
    if args.crew:
        crew_role = next((r for r in load_crew(args.crew)['roles'] if r['role'].lower() == role.lower()), None)
        if crew_role is None:
            raise SystemExit(f'crew {args.crew} has no role {role}')
        if args.cli is None:
            tool = crew_role['tool']
    state_dir = resolve_shift(args.shift)
    saved = read_crew_json(state_dir)
    slots = (saved or {}).get('slots')
    if not slots:
        raise SystemExit(f'shift {state_dir.name} has no slot grid (its crew layout is split); nothing to spawn into')
    try:
        hwnd = json.loads((state_dir / 'launch.json').read_text(encoding='utf-8')).get('hwnd')
    except (OSError, ValueError):
        hwnd = None
    if not hwnd or hwnd not in terminal_windows():
        raise SystemExit(f'window is gone: shift {state_dir.name} has no open Terminal window to spawn into')
    members = saved['members']
    existing = next((m for m in members if m['label'].lower() == role.lower()), None)
    if existing:
        info = member_info(state_dir, existing['label'])
        if info.get('pid') and pid_alive(info['pid'], info.get('started')):
            raise SystemExit(f"role {existing['label']} is already live in shift {state_dir.name} (pid {info['pid']})")
    if args.slot and not 1 <= args.slot <= slots:
        raise SystemExit(f'no free slot: slot {args.slot} is not in 1..{slots}')
    states = {n: slot_state(state_dir, n) for n in range(1, slots + 1)}
    mine = []   # the slots recorded for this role
    if existing:
        mine = [n for n in states if (read_slot(state_dir, n) or {}).get('role') == existing['label']]
        starting = [n for n in mine if states[n] == 'starting']
        if starting:
            # spawned into a slot but its wrapper has not written <label>.json yet: still starting, count it live
            raise SystemExit(f"role {existing['label']} is already live in shift {state_dir.name} "
                             f'(starting in slot {starting[0]})')
    free = [n for n, state in states.items() if state in ('empty', 'dead')]
    if args.slot:
        slot = args.slot if args.slot in free else None
    else:
        # a dead member goes back into its own slot; otherwise the lowest free slot, dead or empty alike
        own = [n for n in free if n in mine]
        slot = (own or free or [None])[0]
    if slot is None:
        raise SystemExit(f'no free slot: slot {args.slot} is taken' if args.slot else
                         f'no free slot in shift {state_dir.name} ({slots} slots, all taken)')
    shift = state_dir.name
    lead = next((m for m in members if m.get('human_contact')), members[0])
    if existing and not crew_role and args.cli is None and existing.get('tool') in TOOLS:
        tool = existing['tool']
    same_role = next((m for m in members if m['role'].lower() == role.lower()), None)
    same_tool = next((m for m in members if m.get('tool') == tool), None)
    if same_tool:
        model = same_tool['model']
    else:
        model = (saved.get('model') or MODEL) if tool == 'claude' else DEFAULT_TOOL_MODELS[tool]
    member = {'role': same_role['role'] if same_role else role, 'label': role, 'identity': f'{shift}-{role.lower()}',
              'brief': f'{role.lower()}.md' if (BRIEFS_DIR / f'{role.lower()}.md').is_file() else 'coder.md',
              # like the brief: the role's own persona, else the Coder's (a new label such as Coder2 is a coder)
              'persona': default_persona(same_role['role'] if same_role else role) or default_persona('Coder'),
              'colour': same_role['colour'] if same_role else 'yellow', 'human_contact': False, 'tool': tool,
              'model': model, 'peer': lead['identity']}
    if not existing:
        # a role the shift never had but its crew manifest names (an Advisor added to an older 3-pane shift):
        # the manifest gives brief and colour, and the model when the manifest role runs the spawned tool
        try:
            manifest_roles = load_crew(saved.get('crew', 'default'))['roles']
        except SystemExit:
            manifest_roles = []   # the shift's manifest is gone or unusable: spawn as before
        manifest_role = next((r for r in manifest_roles if r['role'].lower() == role.lower()), None)
        if manifest_role:
            member.update(role=manifest_role['role'], label=manifest_role['role'], brief=manifest_role['brief'],
                          persona=manifest_role.get('persona'), colour=manifest_role['colour'])
            if manifest_role['tool'] == tool:
                member['model'] = manifest_role['model']
                for opt in ('reasoning', 'account', 'permissions'):
                    if opt in manifest_role:
                        member[opt] = manifest_role[opt]
    if existing:
        # a gone member is restarted with its own role, label, identity, brief, colour and (on the same tool) model
        member.update(role=existing.get('role', existing['label']), label=existing['label'],
                      identity=existing.get('identity', f"{shift}-{existing['label'].lower()}"),
                      brief=existing.get('brief', member['brief']), persona=existing.get('persona', member['persona']),
                      colour=existing.get('colour', member['colour']))
        if tool == existing.get('tool', 'claude') and existing.get('model'):
            member['model'] = existing['model']
            for opt in ('reasoning', 'account', 'permissions'):
                if opt in existing:
                    member[opt] = existing[opt]
    if crew_role:
        # --crew NAME wins over the shift's manifest and the gone member's record; the model, reasoning and account
        # only when the spawned tool is the one NAME gives the role (an explicit --cli falls back as above)
        member.update(role=crew_role['role'], brief=crew_role['brief'], persona=crew_role.get('persona'),
                      colour=crew_role['colour'])
        if not existing:
            member['label'] = crew_role['role']
        for opt in ('reasoning', 'account', 'permissions'):
            member.pop(opt, None)
        if crew_role['tool'] == tool:
            member['model'] = crew_role['model']
            for opt in ('reasoning', 'account', 'permissions'):
                if opt in crew_role:
                    member[opt] = crew_role[opt]
    updated = [m for m in members if m is not existing] + [member]
    cwd = saved.get('cwd') or member_info(state_dir, lead['label']).get('cwd') or os.getcwd()
    crew_file = state_dir / 'crew.json'
    before = crew_file.read_text(encoding='utf-8')
    write_json(crew_file, {**saved, 'members': updated})
    # Typed at a cmd prompt: every path double-quoted so a folder with spaces survives cmd's parsing.
    line = (f'"{sys.executable}" "{Path(__file__).resolve()}" --pane {member["label"]} --state-dir "{state_dir}" '
            f'--start --cwd "{cwd}" --slot {slot}'
            + (f" --crew {saved['crew']}" if saved.get('crew', 'default') != 'default' else ''))
    record = read_slot(state_dir, slot)
    spawned = time.time()   # before typing: the wrapper's <role>.json 'started' comes after it
    try:
        typed = subprocess.run(['agents-cli', '_attach', 'inject', str(record['cmd_pid']), '1', line],
                               capture_output=True, timeout=30)
        failed = None if typed.returncode == 0 else \
            (typed.stderr or b'').decode('utf-8', 'replace').strip() or f'exit {typed.returncode}'
    except (OSError, subprocess.TimeoutExpired) as err:
        failed = str(err)
    if failed:
        crew_file.write_text(before, encoding='utf-8')   # nothing was started: leave the crew as it was
        raise SystemExit(f'could not type into slot {slot} (cmd pid {record["cmd_pid"]}): {failed}')
    # re-read: the wrapper may already have added its wrapper_pid
    kept = {k: v for k, v in (read_slot(state_dir, slot) or record).items() if k != 'slot'}
    write_slot(state_dir, slot, **{**kept, 'role': member['label'], 'identity': member['identity'],
                                   'spawned': spawned})
    bus_append(state_dir, 'spawn', role=member['label'], slot=slot, tool=tool, identity=member['identity'])
    print(json.dumps({'shift': shift, 'slot': slot, 'role': member['label'], 'identity': member['identity'],
                      'tool': tool}))


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
    """
    lock = Path(state_dir) / 'claims.lock'
    try:
        take_file_lock(lock, wait, stale)
    except TimeoutError:
        raise SystemExit(f'{lock} is still held after {wait:.0f}s; try again')
    try:
        yield
    finally:
        release_file_lock(lock)


def take_file_lock(lock, wait, stale):
    """Create `lock` with O_EXCL, retrying for `wait` seconds (then TimeoutError); the caller releases it with
    release_file_lock.

    A lock older than `stale` seconds is left over from a crashed holder and removed. On Windows a contender's
    create can fail with PermissionError instead of FileExistsError (the lock is delete-pending after its holder's
    unlink, or another contender's stat has it open): that is contention too, retried until `wait` runs out.
    """
    deadline = time.time() + wait
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return
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
                raise TimeoutError(f'{lock} is still held after {wait:.0f}s')
            time.sleep(0.1)
        except PermissionError:
            if time.time() > deadline:
                raise
            time.sleep(0.01)


def release_file_lock(lock, wait=2.0):
    """Unlink `lock`, retrying while Windows refuses with PermissionError (a contender's stat or create has it
    open for a moment). Giving up silently would leave the lock until it goes stale and time out every waiter."""
    deadline = time.time() + wait
    while True:
        try:
            lock.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.time() > deadline:
                return
            time.sleep(0.005)
        except OSError:
            return


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
        for other_dir in SHIFTS_DIR.iterdir() if SHIFTS_DIR.exists() else []:
            if not other_dir.is_dir() or other_dir == state_dir or not shift_record(other_dir)['alive']:
                continue
            other_claims = load_claims(other_dir)
            for p in paths:
                if p in other_claims:
                    held = other_claims[p]
                    print(f"{p} is already claimed by {held.get('role')} (shift {other_dir.name}) "
                          f"since {held.get('time')}", file=sys.stderr)
                    if p not in conflicts:
                        conflicts.append(p)
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


BUS_LOCK_WAIT = 5.0     # seconds a bus append waits for bus.lock before giving up (with a warning)
BUS_LOCK_STALE = 10.0   # a bus.lock older than this is left over from a crashed writer
LOG_POLL_SECONDS = 1.0  # how often `log --follow` re-reads bus.jsonl


def bus_append(state_dir, event, **fields):
    """Append one event to <state_dir>/bus.jsonl: {seq, ts, event, **fields}; seq is its 1-based line number.

    The shift bus (NGH-026) is a plain file, no process: every mailbox row and launcher event lands here, in order.
    bus.lock serialises writers so seq is unique and increasing; the line goes out in one write call. A bus
    failure only warns on stderr - it never breaks the command that appended.
    """
    path = Path(state_dir) / 'bus.jsonl'
    lock = Path(state_dir) / 'bus.lock'
    try:
        take_file_lock(lock, BUS_LOCK_WAIT, BUS_LOCK_STALE)
        try:
            data = path.read_bytes() if path.exists() else b''
            # a torn last line (writer killed mid-write) is closed off so the new event starts on its own line
            prefix = b'\n' if data and not data.endswith(b'\n') else b''
            seq = data.count(b'\n') + len(prefix) + 1
            record = {'seq': seq, 'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'event': event, **fields}
            line = prefix + (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_BINARY', 0))
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            release_file_lock(lock)
    except OSError as err:   # TimeoutError included
        print(f'warning: shift bus not written ({event}): {err}', file=sys.stderr)


def bus_events(path, offset=0):
    """([(text, event)], new_offset): the complete lines of bus.jsonl from byte `offset` on, with each parsed
    (event None when a line is not a JSON object); a trailing line without its newline yet is left for the next read."""
    with open(path, 'rb') as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b'\n') + 1
    lines = []
    for raw in data[:end].splitlines():
        text = raw.decode('utf-8', 'replace').rstrip('\r')
        if not text.strip():
            continue
        try:
            event = json.loads(text)
        except ValueError:
            event = None
        lines.append((text, event if isinstance(event, dict) else None))
    return lines, offset + end


def one_line(text):
    return ' '.join(str(text).split())


def format_event(e):
    """One human line per bus event, e.g. '#12 08:53:49 msg Foreman->Coder assignment asg:a1 msg:3  <body>'."""
    head = f"#{e.get('seq', '?')} {str(e.get('ts', ''))[11:19]} {e.get('event', '?')}"
    kind = e.get('event')
    if kind == 'msg':
        return (f"{head} {e.get('sender')}->{e.get('recipient')} {e.get('kind')} asg:{e.get('asg') or '-'} "
                f"msg:{e.get('id')}  {one_line(e.get('body', ''))}")
    if kind == 'sent':
        return f"{head} msg:{e.get('id')}"
    if kind == 'ack':
        return f"{head} msg:{e.get('id')} by {e.get('by')}: {e.get('outcome')}" + \
            (f" ({e['reason']})" if e.get('reason') else '')
    if kind == 'start':
        return (f"{head} ticket:{e.get('ticket') or '-'} crew:{e.get('crew')} "
                f"members:{','.join(e.get('members') or [])} cwd:{e.get('cwd')}")
    if kind == 'spawn':
        return f"{head} {e.get('role')} slot:{e.get('slot')} tool:{e.get('tool')} {e.get('identity')}"
    if kind == 'down':
        return (f"{head} stopped:{','.join(e.get('stopped') or []) or '-'} "
                f"lock_released:{','.join(e.get('lock_released') or []) or '-'}")
    rest = {k: v for k, v in e.items() if k not in ('seq', 'ts', 'event')}
    return f"{head} {one_line(json.dumps(rest, ensure_ascii=False))}"


def log_command(argv, sleep=time.sleep):
    parser = argparse.ArgumentParser(
        prog='nightshift log',
        description="Print a shift's bus (<state_dir>/bus.jsonl) in order: every mailbox message, sent and ack, and "
                    'the start, spawn, result and down events. Reads the file only; there is no bus process. '
                    "--follow keeps watching until Ctrl+C, or until the shift's down event has been printed.")
    parser.add_argument('shift', help='shift name, unique suffix, or "latest"')
    parser.add_argument('--follow', '-f', action='store_true', help='keep printing new events as they are appended')
    parser.add_argument('--since', type=int, default=0, metavar='N', help='only events with seq greater than N')
    parser.add_argument('--json', action='store_true', help='print the raw JSON lines')
    args = parser.parse_args(argv)
    state_dir = resolve_shift(args.shift)
    path = state_dir / 'bus.jsonl'
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors='replace')   # a cp1252 console must not crash on a message body
    offset, warned = 0, False
    try:
        while True:
            if path.exists():
                lines, offset = bus_events(path, offset)
                done = False
                for text, e in lines:
                    seq = (e or {}).get('seq')
                    done = done or (e or {}).get('event') == 'down'   # the shift is over: --follow stops here
                    if isinstance(seq, int) and seq <= args.since:
                        continue
                    print(text if args.json else format_event(e) if e else f'? {text}', flush=True)
                if done or not args.follow:
                    return
            else:
                if not warned:
                    print(f'no bus yet: {path}', file=sys.stderr, flush=True)
                    warned = True
                if not args.follow:
                    return
            sleep(LOG_POLL_SECONDS)
    except KeyboardInterrupt:
        return


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
    """Store a message with both incarnations as they are now and put it on the shift bus; returns its id.

    Every mailbox row is written here, so every one lands on the bus as a 'msg' event.
    """
    with conn:
        cur = conn.execute(
            'INSERT INTO messages (kind, sender, sender_incarnation, recipient, recipient_incarnation, asg, body, '
            'created, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (kind, sender, incarnation(state_dir, sender), recipient, incarnation(state_dir, recipient), asg, body,
             time.strftime('%Y-%m-%d %H:%M:%S'), 'stored'))
    bus_append(state_dir, 'msg', id=cur.lastrowid, kind=kind, sender=sender, recipient=recipient, asg=asg, body=body)
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
        lead = human_contact_label(state_dir)
        if recipient == ADVISOR and sender != lead:
            raise SystemExit(f'only {lead} may address the {ADVISOR}; nothing stored')
        if sender == ADVISOR and recipient != lead:
            raise SystemExit(f'the {ADVISOR} answers only {lead}; nothing stored')
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
    bus_append(state_dir, 'sent', id=args.id)
    print(json.dumps(row))


def inbox_view(state_dir, label):
    """What a member has to deal with: unacked inbound, its own unsent outbound, open assignments."""
    view = {'inbound_unacked': [], 'outbound_unsent': [], 'active_assignments': []}
    with mailbox(state_dir, create=False) as conn:
        if conn is None:
            return view
        current = incarnation(state_dir, label)
        # Only rows addressed to the current incarnation can be acked by it (earlier ones are stale assignments).
        view['inbound_unacked'] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM messages WHERE recipient = ? AND state IN ('stored', 'sent') ORDER BY id", (label,))
            if r['recipient_incarnation'] == current]
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
            bus_append(state_dir, 'ack', id=args.id, by=label, outcome='protocol violation', reason=reason)
            print(f'protocol violation: {reason}', file=sys.stderr)
            raise ProtocolViolation(5, reason)
        outcome = 'already acknowledged' if row['state'] == 'acknowledged' else 'acknowledged'
        with conn:
            conn.execute("UPDATE messages SET state = 'acknowledged' WHERE id = ?", (args.id,))
        row = get_message(conn, args.id)
    bus_append(state_dir, 'ack', id=args.id, by=label, outcome=outcome)
    print(json.dumps({'ack': outcome, 'row': row}))


def unacked_messages(state_dir):
    """Stored or sent rows, oldest first; [] when the shift has no mailbox."""
    with mailbox(state_dir, create=False) as conn:
        if conn is None:
            return []
        return [dict(r) for r in conn.execute("SELECT * FROM messages WHERE state IN ('stored', 'sent') ORDER BY id")]


BUILD_POLL_SECONDS = 5           # how often a waiting build re-checks its folder's build lock
BUILD_TIMEOUT_MINUTES = 60       # default --timeout: how long a build waits for the folder before exit 6
BUILD_UNREADABLE_GRACE = 5       # seconds an unreadable build lock may be (a holder mid-write) before it is stale


class BuildWaitTimeout(FolderLocked):
    """Another live build held the folder for the whole --timeout; exits with code 6."""


def build_lock_path(cwd):
    """LOCKS_DIR/builds/<lock_path key>: a subfolder, so read_locks()/stale_locks()/resume never see build locks."""
    return LOCKS_DIR / 'builds' / lock_path(cwd).name


def process_running(pid, started_epoch=None):
    """True if any process has this PID (any image name) and, given started_epoch, was created then (within 2s)."""
    if not pid:
        return False
    out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                         capture_output=True, timeout=15).stdout.decode('utf-8', 'replace')
    if not any(line.startswith('"') and line.strip('"').split('","')[1:2] == [str(pid)] for line in out.splitlines()):
        return False
    if started_epoch is None:
        return True
    created = process_start_epoch(pid)
    return created is None or abs(created - started_epoch) <= 2


def write_json(path, data):
    """Write JSON through a temp file + os.replace, so a reader never sees half a file."""
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def take_build_lock(cwd, shift, n, timeout_minutes):
    """Hold the folder's build lock (O_EXCL); waits while a live build holds it, BuildWaitTimeout after the timeout.

    A holder whose pid is gone, or whose process start time differs (PID reused), is stale and replaced.
    """
    path = build_lock_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        started_epoch = process_start_epoch(os.getpid())
    except (OSError, subprocess.TimeoutExpired):
        started_epoch = None
    mine = {'shift': shift, 'n': n, 'cwd': cwd, 'pid': os.getpid(), 'started_epoch': started_epoch,
            'started': time.strftime('%Y-%m-%d %H:%M:%S')}
    deadline = time.time() + timeout_minutes * 60
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                raw = path.read_text(encoding='utf-8')
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue   # released between open and read: try again
            try:
                held = json.loads(raw)
            except ValueError:
                held = None
            if not isinstance(held, dict):
                stale = age > BUILD_UNREADABLE_GRACE   # a fresh empty file is a holder still writing it
            else:
                stale = not process_running(held.get('pid'), held.get('started_epoch'))
            if stale:
                with contextlib.suppress(OSError):
                    if path.read_text(encoding='utf-8') == raw:   # not already replaced by another waiter
                        path.unlink()
                continue
            if time.time() >= deadline:
                holder = (f"build #{held.get('n')} of shift {held.get('shift')} (pid {held.get('pid')}, "
                          f"since {held.get('started')})") if isinstance(held, dict) else 'a build still writing its lock'
                message = f'folder {cwd} is still held by {holder} after {timeout_minutes:g} minutes; lock {path}'
                print(message, file=sys.stderr)
                raise BuildWaitTimeout(6, message)
            time.sleep(BUILD_POLL_SECONDS)
            continue
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(mine, f)
        return path


def release_build_lock(path):
    """Remove the build lock if this process still holds it."""
    with contextlib.suppress(OSError, ValueError):
        if json.loads(path.read_text(encoding='utf-8')).get('pid') == os.getpid():
            path.unlink()


def reserve_build(builds_dir):
    """Next build number in builds_dir (1, 2, ...), reserved by creating <n>.json with O_EXCL."""
    builds_dir.mkdir(parents=True, exist_ok=True)
    n = 1 + max((int(p.stem) for p in builds_dir.glob('*.json') if p.stem.isdigit()), default=0)
    while True:
        try:
            os.close(os.open(builds_dir / f'{n}.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return n
        except FileExistsError:
            n += 1


def git_state(cwd):
    """(head, dirty, porcelain lines) of cwd; (None, None, []) when it is not a git work tree or git fails."""
    try:
        head = subprocess.run(['git', '-C', cwd, 'rev-parse', 'HEAD'], capture_output=True, timeout=30)
        if head.returncode != 0:
            return None, None, []
        status = subprocess.run(['git', '-C', cwd, 'status', '--porcelain'], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None, None, []
    sha = (head.stdout or b'').decode('utf-8', 'replace').strip() or None
    if status.returncode != 0:
        return sha, None, []
    porcelain = [line for line in (status.stdout or b'').decode('utf-8', 'replace').splitlines() if line.strip()]
    return sha, bool(porcelain), porcelain


def load_builds(state_dir):
    """Every readable <state_dir>/builds/<n>.json, lowest n first."""
    builds_dir = Path(state_dir) / 'builds'
    found = []
    for p in sorted((p for p in builds_dir.glob('*.json') if p.stem.isdigit()), key=lambda p: int(p.stem)) \
            if builds_dir.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found.append(data)
    return found


def cancel_build(state_dir, n):
    """Mark build n cancelled; a running build's process tree is killed by PID. Finished builds are left alone."""
    path = Path(state_dir) / 'builds' / f'{n}.json'
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise SystemExit(f'no build #{n} in shift {Path(state_dir).name}')
    if record.get('status') not in ('waiting', 'running'):
        print(json.dumps({'build': n, 'cancelled': False,
                          'message': f"build #{n} already finished with status {record.get('status')}"}))
        return
    (Path(state_dir) / 'builds' / f'{n}.cancel').write_text(time.strftime('%Y-%m-%d %H:%M:%S'), encoding='utf-8')
    killed = False
    if record.get('status') == 'running' and record.get('pid'):
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(record['pid'])], capture_output=True, timeout=30)
        killed = True
    print(json.dumps({'build': n, 'cancelled': True, 'killed_pid': record.get('pid') if killed else None}))


def build(argv):
    parser = argparse.ArgumentParser(
        prog='nightshift build',
        description='Run a heavy build or test suite in a folder, one at a time across shifts: waits while another '
                    'live build holds the folder (exit 6 after --timeout), then runs COMMAND and records '
                    '<state_dir>/builds/<n>.json (head, dirty tree, exit, log) as evidence; exit code = COMMAND\'s. '
                    '--cancel SHIFT N kills build N\'s process tree and marks it cancelled.')
    parser.add_argument('shift', help='shift name, unique suffix, or "latest"')
    parser.add_argument('n', nargs='?', type=int, help='with --cancel: the build number')
    parser.add_argument('--cwd', help='folder to build in (its build lock serialises builds there)')
    parser.add_argument('--cmd', help='the command line to run (through the shell)')
    parser.add_argument('--timeout', type=float, default=BUILD_TIMEOUT_MINUTES,
                        help=f'minutes to wait for the folder before giving up with exit 6 (default {BUILD_TIMEOUT_MINUTES})')
    parser.add_argument('--cancel', action='store_true', help='cancel build N of SHIFT')
    args = parser.parse_args(argv)
    state_dir = resolve_shift(args.shift)
    if args.cancel:
        if args.n is None:
            parser.error('--cancel needs the build number: nightshift build --cancel SHIFT N')
        cancel_build(state_dir, args.n)
        return
    if not args.cwd or not args.cmd:
        parser.error('give both --cwd DIR and --cmd COMMAND')
    if not Path(args.cwd).is_dir():
        parser.error(f'build folder does not exist: {args.cwd}')
    cwd = str(Path(args.cwd).resolve())
    builds_dir = state_dir / 'builds'
    n = reserve_build(builds_dir)
    path, cancel_marker = builds_dir / f'{n}.json', builds_dir / f'{n}.cancel'
    log = builds_dir / f'{n}.log'
    record = {'n': n, 'cmd': args.cmd, 'cwd': cwd, 'head': None, 'dirty': None, 'porcelain': [], 'started': None,
              'ended': None, 'exit': None, 'log': str(log), 'pid': None, 'status': 'waiting'}
    write_json(path, record)
    lock = None
    try:
        lock = take_build_lock(cwd, state_dir.name, n, args.timeout)
        record['head'], record['dirty'], record['porcelain'] = git_state(cwd)
        record['started'] = time.strftime('%Y-%m-%d %H:%M:%S')
        if cancel_marker.exists():
            record.update(status='cancelled', ended=record['started'])
        else:
            with open(log, 'wb') as out:
                child = subprocess.Popen(args.cmd, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
                record.update(status='running', pid=child.pid)
                write_json(path, record)
                code = child.wait()
            record.update(exit=code, ended=time.strftime('%Y-%m-%d %H:%M:%S'),
                          status='cancelled' if cancel_marker.exists() else 'passed' if code == 0 else 'failed')
    except BuildWaitTimeout:
        record.update(status='timeout', ended=time.strftime('%Y-%m-%d %H:%M:%S'))
        raise
    finally:
        if record['status'] in ('waiting', 'running'):
            record.update(status='failed', ended=time.strftime('%Y-%m-%d %H:%M:%S'))   # interrupted
        write_json(path, record)
        if lock:
            release_build_lock(lock)
    print(json.dumps(record, indent=2))
    if record['exit']:
        raise SystemExit(record['exit'])


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
    parser.add_argument('--notify', dest='notify', action='store_true', default=False,
                        help='email the owner via Mailkeep when the loop halts or finishes')
    parser.add_argument('--no-notify', dest='notify', action='store_false',
                        help='do not email the owner (default)')
    args = parser.parse_args(argv)

    def notify(subject, status, records):
        if not args.notify:
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
    if 'PYTEST_CURRENT_TEST' not in os.environ:
        ctypes.windll.kernel32.SetConsoleTitleW(loop_title(tickets))   # the name the finished-loop close looks for
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
            down([state_dir.name, '--close'])
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
        close_terminal_windows(loop_title(tickets))


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
    parser.add_argument('--notify', dest='notify', action='store_true', default=False,
                        help='email the owner via Mailkeep when the loop halts or finishes')
    parser.add_argument('--no-notify', dest='notify', action='store_false',
                        help='do not email the owner (default)')
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
        flags = ['--stall-minutes', str(args.stall_minutes)] + (['--notify'] if args.notify else [])
        loop(tickets + flags, continue_from={r['ticket']: r['continue_from'] for r in plan['resume']})


def main():
    commands = {'start': start, 'status': status, 'down': down, 'spawn': spawn, 'loop': loop, 'resume': resume,
                'claim': claim, 'release': release, 'claims': claims_command, 'msg': msg, 'inbox': inbox, 'ack': ack,
                'build': build, 'log': log_command}
    if len(sys.argv) > 1 and sys.argv[1] in commands:
        commands[sys.argv[1]](sys.argv[2:])
        return
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Commands:\n  start "task"|TICKET [--dry-run] [--cwd D] [--crew NAME]\n'
               '                                              start a crew (manifest crews/NAME.yaml)\n'
               '  status [--all]                              shifts and whether their crews are alive\n'
               '  down [SHIFT|latest] [--hold|--close]        capture panes, write handover.md, stop the crew; --hold (default)\n'
               '                                              keeps the tab open, --close closes its window\n'
               '  spawn SHIFT --role ROLE [--cli claude|gemini|codex|copilot] [--crew NAME] [--slot N]\n'
               "                                              start a session in a free slot of the shift's 1x4 window\n"
               '  loop TICKET [TICKET...] [--stall-minutes N] optional batch runner: start, wait for result.json, down --close, next\n'
               '  loop --project PREFIX [--max N]             same over a project\'s backlog, lowest number first\n'
               '  resume [--project PREFIX] [--dry-run]       rerun tickets whose latest shift never reached done (--continue-from it)\n'
               '  claim SHIFT ROLE PATH...                    claim files before editing; exit 4 if another role holds one\n'
               '  release SHIFT ROLE [PATH...]                drop those claims, or all of ROLE\'s when no paths given\n'
               '  claims SHIFT                                print the shift\'s claims as JSON\n'
               '  msg send SHIFT FROM TO --kind K --body TEXT [--asg ID]\n'
               '                                              store a crew message; prints its header line\n'
               '  msg sent SHIFT ID                           mark a stored message as sent with SendMessage\n'
               '  inbox SHIFT ROLE                            unacked inbound, unsent outbound, active assignments\n'
               '  ack SHIFT ROLE ID                           acknowledge and print the stored body; exit 5 = do not act\n'
               "  build SHIFT --cwd DIR --cmd 'COMMAND' [--timeout MIN]\n"
               '                                              run a build one at a time per folder; evidence in builds/<n>.json\n'
               '  build --cancel SHIFT N                      kill build N and mark it cancelled\n'
               '  log SHIFT [--follow] [--since N] [--json]   print the shift bus (bus.jsonl): messages, acks, start..down')
    parser.add_argument('--pane', help=argparse.SUPPRESS)
    parser.add_argument('--state-dir', help=argparse.SUPPRESS)
    parser.add_argument('--start', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--cwd', default='C:/git/nightshift', help=argparse.SUPPRESS)
    parser.add_argument('--crew', help=argparse.SUPPRESS)
    parser.add_argument('--slot', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--live', action='store_true', help='diagnostic ping/pong communication trial with live agents')
    parser.add_argument('--brief-file', help='with --live: replace the trial brief with this assignment file')
    args = parser.parse_args()
    if args.pane:
        pane(args.pane, args.state_dir, args.live, args.brief_file, args.start, args.cwd, args.crew, args.slot)
        return
    if args.slot:
        slot_shell(args.slot, args.state_dir)   # an empty container slot: record it, leave a plain cmd prompt
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

# Nightshift shifts — architecture (as built, 16 Oct 2026)

Nightshift runs a **shift**: a small crew of interactive coding-agent sessions, launched together
in one Windows Terminal window, given one ticket, and torn down when the ticket is done. A
**loop** runs shifts back to back. Nothing resident, no service, no installer: a Python launcher,
plain files, and the CLIs already on the machine.

The design this grew from is in `portable-shifts-design.md` and its three reviews
(`*-fable-critique.md`, `*-mistral-critique.md`, `*-argon-gaps.md`). This file describes what
exists; where the two differ, this file wins.

## Container, not loop (owner decision, 16 Oct 2026; NGH-025)

The owner has reset the shape of the thing. Nightshift is **not a persistent process**. The
executable launches a container and exits: one Windows Terminal tab holding a grid of slots
(1x4 now, 2x4 later), the first slots filled with the crew from the manifest, the rest empty
shells. From then on **Foreman owns the container**: it may fill an empty slot through
`nightshift spawn` (any CLI; the Nightshift CLI is the one component that knows how to put a
session into a tab slot, agents-cli only drives consoles), and when the ticket is tested and
deployed it writes the records and runs `nightshift down` itself. If Foreman dies, the shift is
dead until a human looks; there is no watcher of watchers. The `loop` below is kept as an
optional batch runner and is no longer the default path.

As built (NGH-025):

- **Layout.** A manifest says `layout: 1x4` (the default and gemini-coder crews) or `layout: split`
  (the tab sized to the crew, 2-8 panes, unchanged). `load_crew` exposes `slots` (4 for 1x4, None
  for split) and refuses a crew that does not fit. `wt_command` lays out four equal columns: crew
  panes in slots 1..k (their wrappers get `--slot N`), and slots k+1..4 run
  `cmd.exe /k <python> nightshift_shifts.py --slot N --state-dir <dir>`, titled `<ticket or shift> slot N`.
- **slots/.** Every slot records `<state_dir>/slots/<N>.json`: an empty slot writes
  `{slot, cmd_pid, started, role: null}` (cmd_pid = the cmd.exe that ran it) and exits to a plain
  prompt; a crew pane writes `{slot, role, identity, wrapper_pid}`. `start --dry-run` shows
  `slots: [{slot, role|null, pane_title}]`.
- **Launcher exits.** `launch` waits for every `<Role>.json` and every slot record, prints, and
  returns. Nothing polls the shift after that.
- **spawn.** `nightshift spawn SHIFT --role ROLE [--cli claude|gemini] [--slot N]` is the only way a
  crew session starts after launch, and only Foreman runs it. It refuses (non-zero, plain message) when
  the shift's window (`launch.json` hwnd) is gone, when no slot is free or the requested one is taken,
  or when the role is already live (or still starting in a slot). `--cli gemini` runs agy; codex,
  copilot and opencode are refused as not yet wired (NGH-022). It appends the member to `crew.json`
  (identity `<shift>-<role>`, brief `<role>.md` if it exists else `coder.md`, the colour of a member
  with the same role else yellow, peer = the human contact; Foreman's own peer list is not rewritten),
  types the pane wrapper line (paths double-quoted, `--slot N`) into that slot's cmd with
  `agents-cli _attach inject`, and marks the slot with the role. A start-mode pane reads its member
  from `crew.json` (falling back to the manifest), so spawned roles run like launched ones; `status`,
  `down` and the mailbox pick them up from `crew.json`.
- **down --hold / --close.** Both write the handover, kill the crew and release the folder lock.
  `--hold` (default) leaves the tab open; `--close` also closes the window (the loop uses it). The
  member whose process tree is running `down` (Foreman ending its own shift) is killed last, after the
  lock is released and the JSON (with `held`) is printed. Foreman's brief makes `down` its last act
  after writing `result.json`.

## The four identities (owner, 16 Oct 2026)

The 1x4 container is meant for four fixed identities, one per column: **Foreman** (runs the
ticket's goal statement, sole human contact), **Advisor**, **Coder**, **Tester** (NGH-024; built,
see "The default crew" below). The
Advisor is a dormant session on a frontier model (Fable, Astra, Mistral Large 4 or Gemini's top
model, whichever the manifest names): it gets no instructions beyond its identity and does
nothing until Foreman is stuck on something it cannot resolve. Then, instead of asking the human,
Foreman asks the Advisor, and the Advisor is expected to take a sensible decision. Rules that
keep it bounded: Foreman asks only after it has tried itself; the question and the answer go
through the mailbox so the human can audit every decision; the Advisor never edits files and
never talks to Coder or Tester; the human is still asked when the ticket itself is wrong or a
decision cannot be undone. This is the reviewed design's "bounded consultant" (not a council).

## Components

| Piece | Role | Where |
|---|---|---|
| `nightshift_shifts.py` | the launcher: `start`, `status`, `down` (`--hold`/`--close`), `spawn` (NGH-025), the optional `loop`, `resume`, `claim/release/claims`, `msg/inbox/ack` (NGH-019), `build` (NGH-020) | this repo |
| `crews/*.yaml` | crew manifests: roles, counts, model, permission mode, colours, which brief each role gets, each role's `tool` (claude or agy) and `model`, pane layout | this repo |
| `briefs/*.md` | the instructions a role is launched with (`common.md` + `<role>.md`: `foreman.md`, `coder.md`, `tester.md`; placeholders filled in); the task text is appended for Foreman | this repo |
| shift folder | `C:\Data\nightshift\shifts\<shift>\` — the source of truth for one shift (below) | data, not code |
| tasks-cli | the work queue: the loop takes ticket IDs, reads `codebase:` for the working folder, crews write notes, the loop moves status | `C:\Tools\tasks-cli.exe` |
| agents-cli | universal console driver for any agent TUI: peek a pane, inject a line, press a key, name/wait on a session | `C:\Tools\agents-cli` |
| Claude Code mesh | `ListAgents` / `SendMessage` between Claude sessions over named pipes (`~/.claude/sessions/<pid>.json`) | Claude Code |
| Windows Terminal | one window per shift, one pane per role; a "Nightshift Crew" profile (Cascadia Mono 12, Campbell) | `wt.exe` |
| `C:\scripts\nightshift.cmd` | shim on PATH that calls the repo script | C:\scripts |

## A shift, step by step

1. `nightshift start <TICKET|"task"> [--crew NAME] [--cwd DIR] [--continue-from SHIFT]`
   reads the ticket through tasks-cli, resolves the working folder (`--cwd` > ticket `codebase:` >
   current directory), takes the **folder lock** (`C:\Data\nightshift\locks\`, exit 3 if another
   live shift holds it), writes `prompt.txt`, and runs `wt.exe` with one `new-tab`/`split-pane`
   per role. Each pane runs this same script as a **wrapper** (`--pane ROLE`), which launches the
   role's CLI, records `<Role>.json` (pid, name, start time, cwd), answers Claude's trust dialog
   if it appears, sets the role colour, and waits for the CLI to exit. `<Role>.json` also records
   the role's `tool` (claude or agy), which `status` reports per role.
2. Each Claude member starts with `--name <shift>-<role>`, `--permission-mode auto` (owner
   decision: classifier-guarded, never `manual`, never bypass), and
   `--append-system-prompt` = its brief. The wrapper scrubs inherited `CLAUDE*` env so a crew
   launched from inside a Claude session is not a child session of it. The human's foreground
   window is restored once the crew window exists.
3. The crew works. Foreman is the only member that talks to the human; Coder and Tester act only
   on Foreman's assignments (handoff below). Rules the briefs enforce: never launch sessions/windows/agents
   yourself; the only way a session starts is Foreman's `nightshift spawn` into a free slot (anything else:
   write the command for the human); claim files before editing (`claims.json`, exit 4 on
   conflict); record messages in the mailbox before sending them (NGH-019).
4. Foreman ends the shift by writing `result.json`: `status` done|blocked, `stage`
   implemented|tested|deployed|validated, `evidence` per rung, `summary`, `changed`. A ticket may
   say `requires: <rung>` (default tested); done below the bar is recorded as `short`.
5. Foreman then runs `nightshift down` itself: it captures the last lines of each pane, writes
   `handover.md` (result, commits since start, test outcome, denials, claims, pane tails), kills the
   crew processes (Foreman's own tree last), releases the lock, and keeps the tab open (`--hold`,
   default) or closes only that shift's Terminal window (`--close`).

## The default crew: Foreman, Coder, Tester (NGH-024)

- `crews/default.yaml` is Foreman (red), Coder (green), Tester (blue, `tester.md`) in slots 1-3 of
  the 1x4 container; slot 4 is an empty shell for `spawn`. Foreman's peers are `<shift>-coder, <shift>-tester`; Coder's and
  Tester's peer is `<shift>-foreman`. Other crews (`--crew NAME`) are unaffected.
- Handoff is Coder -> Foreman -> Tester. Coder reports an assignment done; Foreman reviews it and
  assigns verification to Tester with its own `--asg` id, saying what changed and what was asked.
  Coder and Tester never talk to each other.
- Tester runs the suite through `nightshift build` so the evidence is revision-bound, reviews the
  diff against the assignment and writes missing tests. It edits test files only (claims disjoint
  from Coder's); a needed product fix is reported, not made.
- Its report starts `TESTED-PASS` or `TESTED-FAIL` and cites `builds/<n>.json` (head, dirty, exit),
  test counts, findings and the test files it changed. A TESTED-PASS on a clean tree is the
  tested-rung evidence Foreman puts in `result.json`; with a Tester present Foreman does not run the
  suite itself. A TESTED-FAIL goes back to Coder as a new assignment. Without a Tester, Foreman
  produces the tested evidence itself as before.

## Mixed crews: per-role tool and model (NGH-012)

- A manifest role may say `tool: claude` (default) or `tool: agy`; anything else is refused naming
  the allowed tools. `model:` on a role overrides the crew `model:` for that role only.
- An agy pane runs `%LOCALAPPDATA%\Microsoft\WinGet\Links\agy.exe --model <model> --mode auto -i
  "<brief>\n\n<opening message>"` (`agy_command`). agy has no `--name` or system-prompt flag, so the brief
  is the prompt text. The wrapper scrubs the env the same way (`crew_env`), then names the pane with
  `agents-cli name <pid> <identity>` (`register_name`, failures ignored) so it can be addressed by its
  shift identity. The trust-dialog answer and `/color` are Claude-only and skipped.
- `pid_alive` counts `agy.exe` as a crew process alongside `claude.exe`/`node.exe`.
- `start --dry-run` adds `tool`, `model` and `exe` to each non-Claude role; Claude roles keep the
  original shape (the default three-pane crew's plan is golden-tested byte for byte).
- `crews/gemini-coder.yaml` is the first mixed crew: Claude Foreman (`foreman-agy-coder.md`), agy
  Coder on `gemini-3.8-flash-high` (`coder-agy.md`). The agy Coder cannot use ListAgents/SendMessage:
  it writes reports in its own window, each ending with `CODER-DONE` or `CODER-BLOCKED`. Foreman
  assigns with mailbox `msg send` → `agents-cli send <coder> "<header> <text>"` → `msg sent`,
  waits with `agents-cli wait <coder> --match 'CODER-DONE|CODER-BLOCKED' --timeout 30m`, reads
  `agents-cli tail`, and checks the marker ends the new report, not an old one. Its proactive check
  is `agents-cli ls`/`tail` instead of ListAgents.

## Window and pane titles (NGH-023)

- Each `start` pane is titled `<ticket or shift> <Role>`, one space, no dash: `NGH-019 Foreman`,
  `NGH-019 Coder-2`; a free-text shift uses its shift name (`Nightshift-shift-1a2b3c4d Coder`).
  `--suppressApplicationTitle` keeps the CLI from overwriting it, and the window shows the
  focused pane's title. One helper (`pane_title`) builds it for `wt_command`, the dry-run plan
  and `down`.
- Empty container slots are titled `<ticket or shift> slot N` (NGH-025).
- The legacy trial modes keep `Nightshift | <Role> | <trial>`.
- `loop` sets its own console title to `Nightshift <first>..<last>` (`Nightshift NGH-019` for one
  ticket; `loop_title`), and a finished loop closes the Terminal window with that title.
- `down` closes the hwnd recorded in `launch.json`; without one it falls back to the single window
  titled by one of this shift's panes, or a bare `Foreman`/`Coder` for shifts from before NGH-023.

## The shift folder is the source of truth

```
C:\Data\nightshift\shifts\<shift>\
  prompt.txt       the task as given to Foreman        ticket.txt   ticket id (when started from one)
  launch.json      wt argv, launcher pid, window hwnd   Foreman.json / Coder.json / Tester.json   pid, name, start time, cwd
  crew.json        members (incl. spawned), slots, cwd  slots/<N>.json  what each container slot holds (NGH-025)
  result.json      the shift contract (above)           handover.md  written by down
  claims.json      file claims per role                 denials.json auto-mode classifier refusals seen
  mailbox.sqlite3  messages: stored → sent → acknowledged, with sender/recipient incarnation (NGH-019)
```
Anything that needs to survive a dead pane, a restart or a different CLI lives here, not in a
process. `status` and `resume` are computed from it.

## The loop (optional batch runner)

Since NGH-025 a shift ends itself (Foreman runs `down`); the loop is an optional way to run
several tickets unattended. `nightshift loop T1 T2 …` (or `--project NGH [--max N]`) is deterministic
Python, no model: start a shift → poll for `result.json`, crew death, a 20-minute idle stall, and
classifier denials → `down --close` → note the ticket → next. A died/stalled shift is rerun with a fresh crew
that gets the previous handover (`--retries`, default 2); `blocked` stops the loop for a human.
It emails the owner through Mailkeep when it halts or finishes (NGH-016) and closes its own
window on success. `nightshift resume` reruns tickets whose last shift never reached done
(after a reboot). The loop is the "guardian"; a model-driven "master of masters" is a persona
decision not yet taken (NGH-014).

## Messaging: two transports, one record

- Claude ↔ Claude: native `SendMessage`, instant, Claude-only, no persistence.
- Anything else (Gemini via `agy`, Copilot): `agents-cli send/wait/tail` into the pane, with a
  `CODER-DONE` / `CODER-BLOCKED` convention (NGH-012, NGH-022).
- The **mailbox** (NGH-019) sits above both: every message is stored first, carries a
  `msg:<id>` header, is acked by id by the right recipient incarnation, and `inbox` re-delivers
  what was stored but never sent. Transports are notifications; the mailbox is what is true.

## Concurrency

Folder lock between shifts (NGH-010); file claims between coders inside a shift (NGH-011);
build slot (NGH-020) serialises expensive builds per folder and records
revision-bound evidence. No worktrees per shift (owner: not needed).

## Design vs built — reconciliation (keep current)

| Reviewed design said | Built | Status |
|---|---|---|
| No resident runtime; launch from the human's shell; Windows Terminal, two panes | same; the default crew is now three panes (NGH-024) | matches |
| Named Claude sessions on the native mesh; permission-mode parity | same (`--name`, `--permission-mode auto` for all) | matches |
| Mailbox with stored→sent→acked, incarnation fencing, `inbox`/`ack`, protocol-violation notes | NGH-019 | matches |
| Folder lock, worktree lease at `assign` time | folder lock (NGH-010); worktrees dropped by owner, file claims instead (NGH-011) | deliberate departure |
| Build slot, revision-bound evidence, cancellation (G4) | NGH-020 | in progress |
| Acceptance gates G0–G5 as the delivery sequence | replaced by ticketed increments run by the loop; G0–G3 and most of G5 are covered, G4 pending | departure in method, not in content |
| `down` with handover; `resume` re-dispatches acked-but-unfinished work | `down`/handover, loop retries with handover carry-over, `resume` after reboot | matches |
| Advisors as bounded consultants; later Tester/Reviewer/DevOps/Controller | Tester built into the default crew (NGH-024); Advisor and the rest not built, NGH-014 holds the design | in progress |
| Second coder, heterogeneous CLIs later | claims ready; per-role `tool:` with an agy Gemini Coder crew (NGH-012, not yet run live); Copilot (NGH-022) | in progress |
| "Portable package", CLI language open | Python in its own repo; binary decision NGH-021 | open |
| — (not in the design) | loop, result ladder, crew manifest, email on halt, denial capture | additions |
| — (owner, 16 Oct) | container with slot grid (1x4), `slots/`, `spawn`, Foreman-owned `down --hold/--close`, launcher exits; loop optional | NGH-025, built |

## Not decided / not built

- Personas beyond Foreman + Coder + Tester (Advisor, DevOps, Reviewer, validator council): design in
  NGH-014, to be agreed with the owner before any is added. Roadmap on NGH-001.
- Whether to converge all messaging on mailbox + agents-cli, or keep native mesh for Claude.
- Single binary (NGH-021): PyInstaller bundle vs Go port — owner's call.
- Parallel shifts on separate codebases (NGH-013) — the locks make it possible; not yet run.

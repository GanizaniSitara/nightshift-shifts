# Nightshift shifts — architecture (as built, 16 Oct 2026)

Nightshift runs a **shift**: a small crew of interactive coding-agent sessions, launched together
in one Windows Terminal window, given one ticket, and torn down when the ticket is done. A
**loop** runs shifts back to back. Nothing resident, no service, no installer: a Python launcher,
plain files, and the CLIs already on the machine.

The design this grew from is in `portable-shifts-design.md` and its three reviews
(`*-fable-critique.md`, `*-mistral-critique.md`, `*-argon-gaps.md`). This file describes what
exists; where the two differ, this file wins.

## Components

| Piece | Role | Where |
|---|---|---|
| `nightshift_shifts.py` | the launcher and loop: `start`, `status`, `down`, `loop`, `resume`, `claim/release/claims`, `msg/inbox/ack` (NGH-019), `build` (NGH-020) | this repo |
| `crews/*.yaml` | crew manifests: roles, counts, model, permission mode, colours, which brief each role gets, pane layout | this repo |
| `briefs/*.md` | the instructions a role is launched with (`common.md` + `<role>.md`, placeholders filled in); the task text is appended for Foreman | this repo |
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
   if it appears, sets the role colour, and waits for the CLI to exit.
2. Each Claude member starts with `--name <shift>-<role>`, `--permission-mode auto` (owner
   decision: classifier-guarded, never `manual`, never bypass), and
   `--append-system-prompt` = its brief. The wrapper scrubs inherited `CLAUDE*` env so a crew
   launched from inside a Claude session is not a child session of it. The human's foreground
   window is restored once the crew window exists.
3. The crew works. Foreman is the only member that talks to the human; Coder acts only on
   Foreman's assignments. Rules the briefs enforce: never launch sessions/windows/agents (write
   the command for the human instead); claim files before editing (`claims.json`, exit 4 on
   conflict); record messages in the mailbox before sending them (NGH-019).
4. Foreman ends the shift by writing `result.json`: `status` done|blocked, `stage`
   implemented|tested|deployed|validated, `evidence` per rung, `summary`, `changed`. A ticket may
   say `requires: <rung>` (default tested); done below the bar is recorded as `short`.
5. `nightshift down` captures the last lines of each pane, writes `handover.md` (result,
   commits since start, test outcome, denials, claims, pane tails), kills the crew processes,
   releases the lock and closes only that shift's Terminal window.

## Window and pane titles (NGH-023)

- Each `start` pane is titled `<ticket or shift> <Role>`, one space, no dash: `NGH-019 Foreman`,
  `NGH-019 Coder-2`; a free-text shift uses its shift name (`Nightshift-shift-1a2b3c4d Coder`).
  `--suppressApplicationTitle` keeps the CLI from overwriting it, and the window shows the
  focused pane's title. One helper (`pane_title`) builds it for `wt_command`, the dry-run plan
  and `down`.
- The legacy trial modes keep `Nightshift | <Role> | <trial>`.
- `loop` sets its own console title to `Nightshift <first>..<last>` (`Nightshift NGH-019` for one
  ticket; `loop_title`), and a finished loop closes the Terminal window with that title.
- `down` closes the hwnd recorded in `launch.json`; without one it falls back to the single window
  titled by one of this shift's panes, or a bare `Foreman`/`Coder` for shifts from before NGH-023.

## The shift folder is the source of truth

```
C:\Data\nightshift\shifts\<shift>\
  prompt.txt       the task as given to Foreman        ticket.txt   ticket id (when started from one)
  launch.json      wt argv, launcher pid, window hwnd   Foreman.json / Coder.json   pid, name, start time, cwd
  result.json      the shift contract (above)           handover.md  written by down
  claims.json      file claims per role                 denials.json auto-mode classifier refusals seen
  mailbox.sqlite3  messages: stored → sent → acknowledged, with sender/recipient incarnation (NGH-019)
```
Anything that needs to survive a dead pane, a restart or a different CLI lives here, not in a
process. `status` and `resume` are computed from it.

## The loop

`nightshift loop T1 T2 …` (or `--project NGH [--max N]`) is deterministic Python, no model:
start a shift → poll for `result.json`, crew death, a 20-minute idle stall, and classifier
denials → `down` → note the ticket → next. A died/stalled shift is rerun with a fresh crew
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
| No resident runtime; launch from the human's shell; Windows Terminal, two panes | same | matches |
| Named Claude sessions on the native mesh; permission-mode parity | same (`--name`, `--permission-mode auto` for all) | matches |
| Mailbox with stored→sent→acked, incarnation fencing, `inbox`/`ack`, protocol-violation notes | NGH-019 | matches |
| Folder lock, worktree lease at `assign` time | folder lock (NGH-010); worktrees dropped by owner, file claims instead (NGH-011) | deliberate departure |
| Build slot, revision-bound evidence, cancellation (G4) | NGH-020 | in progress |
| Acceptance gates G0–G5 as the delivery sequence | replaced by ticketed increments run by the loop; G0–G3 and most of G5 are covered, G4 pending | departure in method, not in content |
| `down` with handover; `resume` re-dispatches acked-but-unfinished work | `down`/handover, loop retries with handover carry-over, `resume` after reboot | matches |
| Advisors as bounded consultants; later Tester/Reviewer/DevOps/Controller | none built; NGH-014 holds the design for owner agreement | not started |
| Second coder, heterogeneous CLIs later | claims ready; Gemini Coder spec'd (NGH-012), Copilot (NGH-022) | not started |
| "Portable package", CLI language open | Python in its own repo; binary decision NGH-021 | open |
| — (not in the design) | loop, result ladder, crew manifest, email on halt, denial capture | additions |

## Not decided / not built

- Personas beyond Foreman + Coder (Tester, DevOps, Reviewer, validator council): design in
  NGH-014, to be agreed with the owner before any is added. Roadmap on NGH-001.
- Whether to converge all messaging on mailbox + agents-cli, or keep native mesh for Claude.
- Single binary (NGH-021): PyInstaller bundle vs Go port — owner's call.
- Parallel shifts on separate codebases (NGH-013) — the locks make it possible; not yet run.

# Nightshift shifts — architecture (as built, 16 Oct 2026)

Nightshift runs a **shift**: a small crew of interactive coding-agent sessions, launched together
in one Windows Terminal window, given one ticket, and torn down when the ticket is done. A
**loop** runs shifts back to back. Nothing resident, no service, no installer: a Python launcher,
plain files, and the CLIs already on the machine.

The design this grew from is in `portable-shifts-design.md` and its three reviews
(`*-fable-critique.md`, `*-mistral-critique.md`, `*-argon-gaps.md`). This file describes what
exists; where the two differ, this file wins. Sections explicitly marked **proposed / unbuilt**
are review designs, not descriptions of running behaviour.

## State roots and floor roles (NGH-035)

New shifts use `C:\nightshift\` as the visible machine state root. The launcher derives these
locations from `SOLID_DIR`:

- `C:\nightshift\shifts\<shift>\` — shift records, the mailbox and the bus.
- `C:\nightshift\locks\` — direct-checkout/non-git working-folder locks, the build lock tree, and short-lived repository landing locks.
- `C:\nightshift\builds\` — the defined machine-level `BUILDS_DIR` path; the current `build`
  command still writes revision-bound evidence to `<state_dir>/builds/<N>.json`, not centrally.
- `C:\nightshift\floor.json` — machine-level floor state for reaping, patrol and dispatch.
- `C:\nightshift\floor\bus.jsonl` — the machine-level floor bus; `floor/cursors/` stores durable
  per-reader cursors, `floor/bus.lock` serializes writers, and each cursor's sibling `.lock` serializes
  only that reader's read/print/cursor-advance batch.

An already-running legacy shift may keep its state under `C:\Data\nightshift\` until it is torn
down; the root change does not move or split that shift's mailbox, claims or bus. The launcher
keeps a read-through lookup for that legacy state while new state is written under `C:\nightshift\`.

The three out-of-shift commands operate on `floor.json` and emit JSON reports:

- `nightshift cleaner [--dry-run]` reaps only dead shifts, or shifts whose `result` is `done` or
  `halted`. It closes a still-open finished shift, releases its claims and stale locks, and records
  `{shift, task, result, reaped_at}` in `floor.json`. Its report has `reaped`, `locks_released`,
  `windows_closed` and `dry_run` fields. A dry run makes no cleanup changes.
- `nightshift warden [<shift>] [--dry-run]` patrols live shifts only and never tears one down. It
  delivers unsent bus messages, nudges a silent Foreman after `NUDGE_IDLE_S` (300 seconds), clears
  stale `claims.lock` and `bus.lock` files after `FILE_LOCK_STALE_S` (30 seconds), normalizes short
  live-role names in `claims.json`, and recovers crashed core slots in an unfinished (`result ==
  "none"`) shift. Recovery is limited to the manifest's Foreman, Advisor, Coder and Tester base
  roles; Apprentice and custom helpers stay under Foreman's control. It records `last_warden_at`
  and per-shift `health` in `floor.json`.
- `nightshift gaffer [--max-shifts N] [--crew CREW] [--ticket ID] [--repo PATH] [--tick]
  [--dry-run]` dispatches work while enforcing a live-shift cap (default `2`). `--tick` runs the
  cleaner and then the warden before evaluating dispatch. At capacity it reports
  `reason: "at_capacity"` without launching; under capacity it dispatches the explicit ticket or
  a queued floor ticket and records `{shift, ticket, crew, repo, dispatched_at}` in `dispatches`.

The machine-level floor bus is a separate, append-only communication channel:

- `nightshift floor say FROM TEXT [--to NAME]` appends one JSON object to
  `C:\nightshift\floor\bus.jsonl`. The default recipient is `all`; each event has exactly `ts`,
  `from`, `to`, `text` and a 1-based monotonic `id`. Writers hold `floor/bus.lock` with the same
  exclusive-file, stale-lock recovery pattern as the shift bus and complete the append before
  releasing that lock. `say` prints the appended event as JSON and fails if the append cannot
  complete.
- `nightshift floor hear ME [--since ID] [--follow]` prints one JSON event per line when `to` is
  `ME` or `all`. The reader cursor is an atomically replaced
  `floor/cursors/<sha256(reader)>.json`; it advances over every complete bus event read, including
  events for other recipients. An explicit `--since` is a non-mutating replay lower bound even when
  it is below the saved cursor. Each read/print/advance batch holds that reader's cursor lock and
  releases it before `--follow` sleeps. `--follow` polls for new
  complete lines, leaves a trailing partial line for the next read, and exits cleanly on Ctrl+C. A
  missing bus is an empty read.
- Supervisors use stable identities `claude-supervisor` and `agy-supervisor`: they hear at every
  patrol and before a crew start, then say directly when starting or landing a shift. The owner is
  not a relay; the full operating note is [docs/SUPERVISOR.md](SUPERVISOR.md). After a successful
  actual `start`, the launcher also appends `from: launcher`, `to: all` with the shift name and
  ticket (when present). Dry-run and failed starts append no automatic start event. Go parity is
  deferred.

Warden delivers only stored messages addressed to the recipient's current incarnation. A send
(`console.Send`, NGH-068) must report submitted. Not submitted remains unconfirmed, except on a copilot pane: Copilot can leave the pasted header
in its composer (TECH-225, seen live 16 Oct), so `send_header` presses `console.Key(pid, tool, "enter")` up to
`COPILOT_ENTER_TRIES` (3) times, each only while the tail shows the header still in the current
composer and no select/confirm menu footer, and confirms with the same tail check. The per-shift `warden-delivery.json` records each attempt
before injection; subsequent patrols check uncertain delivery through the tail without injecting
the same header again. If confirmation stays unavailable, the row remains stored for inspection.

Claude idle time comes from its session record. Other CLIs require `console.Tail` to report
`idle`, plus at least 300 seconds since the latest Foreman bus activity or process start. Busy or
unknown state is never nudged. `warden-nudge.json` throttles attempts, including uncertain sends.

Every nudge, from the warden or from `ready`/`assign`/`note`/`report`, is `console.Send(pid, tool, line, true)`
for every tool, Claude included (NGH-065, NGH-068): unlike agents-cli, which needed `--force` for that, the console
package has no Claude-to-Claude refusal. A nudge is never a SendMessage.

Re-nudge (NGH-065). Each patrol, a row in state `stored` or `sent` (not acknowledged) whose recipient pid is alive
for the row's recipient incarnation and who has no live waiter file (`waiting/<ROLE>.json`) is due a retry when
10 minutes (600 s) have passed since the latest of its last retry, the warden's last delivery attempt
(`warden-delivery.json`, even one that left the row stored) and `delivered_epoch` (else `created`)
(`nudgeRetryDue(now, lastAttempt, attempts)`, false once three retries were sent). A warden delivery that marks a
row sent also sets `delivered_epoch`, so one patrol never both delivers and re-nudges the same row. A retry types `msg:N kind:K asg:A  run: nightshift wait SHIFT ROLE`
again and appends bus `nudge-retry` {id, to, attempt}; at most three per row and incarnation, kept in
`<shift>/warden-retry.json` as `{"<msg id>:<incarnation>": {"attempts": n, "at": epoch}}`. The third retry raises
`member-stalled <Role> msg:<id> unacked after 3 nudges` (a mailbox event to the lead, deduped per role and message
id like the NGH-061 events) and writes the floor-bus line `member-stalled <Role> in <shift> (msg:<id>)` from
`warden` to `owner`. The patrol report lists the retries under `renudged` when there are any; `--dry-run` lists
them without sending or writing.

Mailbox reads use a disposable database/WAL copy outside shift state, so a dry-run cannot create
SQLite WAL/SHM sidecars beside the real mailbox. A named Windows mutex keyed by the canonical
mailbox path guards the copy and every central `mailbox(create=True)` connection through close
and checkpoint. SQLite opens only the scratch copy for reads and rebuilds its SHM there. This
snapshot consistency guarantee covers writers using that helper; direct external SQLite writers
do not participate. Missing mailboxes create no state, and mutex waits have an explicit timeout.

All three commands are wired through the main parser and `COMMANDS` mapping. They are maintenance
and dispatch operations, not additional crew sessions: cleaner and warden are safe to run repeatedly,
and `--dry-run` is the inspection mode for all three.

## Standalone Go view and in-shift writes (NGH-021, Increment 2)

The repository also builds a standalone `build\nightshift.exe` containing the complete set of CLI
commands: read-only views (`status`, `claims`, `inbox`, `log`), native in-shift writes (`claim`,
`release`, `msg`, `ack`, `build`), native lifecycle commands (`land`, `down`, `cleaner`), and full
launcher parity (`start`, `spawn`, `warden`, `gaffer`, `loop`, `resume`). It reads the same shift-folder
contract as the Python launcher and never delegates to Python or replaces the `nightshift.cmd` shim.
The state roots may be isolated for inspection with `NIGHTSHIFT_STATE_ROOT` (default `C:\nightshift`),
`NIGHTSHIFT_LEGACY_ROOT` (default `C:\Data\nightshift`), and `NIGHTSHIFT_HOME` (the home used
for Claude session-registration lookup).

Mailbox reads take the same named Windows mutex as the Python helper, copy `mailbox.sqlite3` and
an existing `-wal` to a disposable directory while holding it, and open only that copy. They do
not create `-shm` or other sidecars beside a live shift mailbox. The build requires Go
and the pinned module versions in `go.mod`; the build entrypoint is `scripts\build.cmd [OUTDIR]`,
which builds `./cmd/nightshift` with `go` from PATH (honouring `GOEXE` if set) using
`-mod=readonly -trimpath` into `OUTDIR\nightshift.exe` (defaulting to `C:\nightshift\bin\nightshift.exe`).
`verify.cmd` calls `"%~dp0scripts\build.cmd" "%~dp0build"` to produce `build\nightshift.exe` for tests.

Increment 2 adds native in-shift write commands to that executable. `claim SHIFT ROLE PATH...`
and `release SHIFT ROLE [PATH...]` use the same state roots and shift resolution as the read-only
views and return the same JSON receipts as the Python oracle:
`claim` returns `{"shift": ..., "role": ..., "claimed": [...]}` and `release` returns
`{"released": [...]}`. Claim paths are normalized to absolute, case-folded keys and duplicate
arguments are removed in first-seen order. The role is stored exactly as supplied; role comparison
for ownership is case-insensitive. A claim refreshes the timestamp when the same role reclaims it.

Both writes take `claims.lock` with the Python-compatible five-second wait and thirty-second stale
lock recovery, then save claims through a temporary file and atomic replacement. A claim checks all
requested paths before changing any of them. In direct-checkout/non-git mode it also checks claims in
other live shifts; a git worktree shift is isolated and does not conflict across shifts. Any conflict
prints the Python-compatible owner lines to stderr, leaves `claims.json` unchanged, and exits `4`.
`release` removes only claims owned by the requested role, all of them when no paths are supplied;
claims held by another role remain untouched. These commands mutate only the shift claim records.

NGH-068 shortens the shared file-lock retry interval on an existing lock from 100 ms to 5 ms,
matching the release retry cadence. This reduces opportunities for new writers to barge ahead of
sleeping waiters and starve them; timeout and stale-lock thresholds remain unchanged.

The same increment carries the mailbox and evidence writes: `msg send` inserts a stored row and
prints its header, `msg sent` advances that row to sent, and `ack` accepts only the addressed,
current-incarnation message and prints its stored body. Unknown, wrong-recipient, or stale-incarnation
acks create the protocol-violation note and exit `5` without handing a body to the caller. `build`
uses the per-folder build lock, reserves and updates `<state_dir>/builds/<N>.json`, records command
exit/head/dirty/log evidence, and preserves the Python wait-timeout (`6`) and command exit codes.
All of these writes use the same mailbox/build file contracts and bus records as the Python oracle.

Increment 3 adds native lifecycle operations for the existing worktree and teardown contracts. `land`
refuses dirty or untested revisions, fast-forwards an unchanged target, returns a rebase-needed result
when the target moved, and returns a conflict result without holding the repository landing lock. `down`
captures pane tails and classifier denials, writes `handover.md`, emits the final bus evidence, releases
runtime locks and stops the crew; landed worktrees are reclaimed while unlanded work is preserved or
held according to the recovery contract. `cleaner` retries pending worktree/branch cleanup for dead or
finished shifts and preserves unexpected files.

The lifecycle helpers are native Go: `handoverReport` renders the result, denial, claim, mailbox, build,
commit and test-evidence sections; `recordDenials` deduplicates pane lines and persists `denials.json`;
and `paneTail` reads visible pane lines through `console.Peek`. These lifecycle operations do not launch
sessions; `down --close` preserves the existing WM_CLOSE window-closing contract.

NGH-068 makes the pane wrapper join its telemetry watcher after child exit. The watcher makes a final
discovery-only refresh before returning, so a short-lived CLI cannot lose its session metadata when
the wrapper exits; usage harvesting remains the responsibility of `down`.

NGH-048 adds best-effort, Go-only session telemetry. A live pane discovers its own Claude or Codex
session/transcript and records `session_id` and `transcript` on its current `<Role>.json` only when a
real file exists; `down` takes the final transcript sample before it kills the panes and stores a
per-role token `usage` summary. The live watcher is fenced by PID and `started` incarnation, serializes
updates with teardown, and merges only absent telemetry fields, so it cannot overwrite a replacement
role record, its preserved history, or the final down harvest. The handover adds `## Token Usage` only
when at least one usable summary exists; absent telemetry leaves Python-oracle-compatible records and
handover bytes unchanged. The serialization uses a path-keyed Windows named mutex, so a crashed pane
cannot strand a stale on-disk telemetry lock and block its replacement.

Increment 4 (NGH-052) completes the native Go migration with full launcher parity: native `start`
(manifest load, ticket prompt via tasks-cli, worktree/lock, crew.json, the 2x4/1x4 Windows Terminal
container argv), `spawn`, the hidden `--pane`/`--slot` child entry points and the top-level
parser/layout trials, `warden`/`gaffer` (agents-cli header delivery), `loop` and `resume`. Standard
crews, briefs, and personas are embedded directly into the binary with `//go:embed` in root `assets.go`,
using the lookup order `NIGHTSHIFT_CREWS_DIR`/`NIGHTSHIFT_BRIEFS_DIR`/`NIGHTSHIFT_PERSONAS_DIR` >
`<exe dir>` > `<exe dir>/..` > embedded, while repository `.nightshift/` layers still take precedence.
The Go binary's children run the Go executable itself (`launcherExe()`), never Python. New commands
register through `cmd/nightshift/registry.go` (`extraCommands`, `startHook`, `spawnHook`). Go also
carries main's on-demand Advisor reservation, consult, floor bus and agy statusline.

## Container, not loop (owner decision, 16 Oct 2026; NGH-025)

The owner has reset the shape of the thing. Nightshift is **not a persistent process**. The
executable launches a container and exits: one Windows Terminal tab holding a grid of slots
(2x4 = 8 slots across two rows of four columns; 1x4 = 4 slots also supported), the first slots
filled with the crew from the manifest, the rest empty shells. From then on **Foreman owns the
container**: it may fill an empty slot through `nightshift spawn` (any CLI; the Nightshift CLI
is the one component that knows how to put a session into a tab slot; its console package drives
consoles), and when the ticket is tested and deployed it writes the records and runs
`nightshift down` itself. If a core member crashes while the shift is active, `nightshift warden`
can recover its pane; if recovery cannot proceed, the shift remains for human inspection. The
`loop` below is kept as an optional batch runner and is no longer the
default path.

As built (NGH-025):

- **Layout.** A manifest says `layout: 2x4` (the default and gemini-coder crews: 8 slots, two rows
  of four equal columns), `layout: 1x4` (4 slots, one row of four equal columns) or `layout: split`
  (the tab sized to the crew, 2-8 panes, unchanged). `load_crew` exposes `slots` (8 for 2x4, 4 for
  1x4, None for split) and refuses a crew that does not fit. `wt_command` lays out the grid: crew
  panes in slots 1..k (their wrappers get `--slot N`), and slots k+1..slots run
  `cmd.exe /k <python> nightshift_shifts.py --slot N --state-dir <dir>`. Every pane is titled
  `<ticket or shift> shift` (NGH-028).
- **slots/.** Every slot records `<state_dir>/slots/<N>.json`: an empty slot writes
  `{slot, cmd_pid, started, role: null}` (cmd_pid = the cmd.exe that ran it) and exits to a plain
  prompt; a crew pane writes `{slot, role, identity, wrapper_pid}`. `start --dry-run` shows
  `slots: [{slot, role|null, pane_title}]`.
- **Slot states and reuse (NGH-028).** `slot_state` reads a slot as `empty` (a running cmd shell, no
  role), `dead` (a running cmd shell whose role's member has exited: `<role>.json` exists and
  `pid_alive` is false), `live`, `starting` (role recorded but no `<role>.json`, or one older than the
  slot's `spawned` stamp, i.e. the previous incarnation's) or `gone` (no record, or no shell to type
  into). Empty and dead slots are free except for an unconsulted Advisor's reserved slot; a live member's slot is never taken. When a container pane's
  member exits, the pane leaves a cmd prompt: a spawned wrapper just returns to the slot shell it was
  typed into; a crew pane wt launched directly runs `cmd.exe /k ... --slot N` and becomes a slot
  shell, and `slot_shell` keeps the dead role in the slot record (so `status` shows it dead and the
  role can go back into its own slot). `status` adds `slots: [{slot, role, state, reusable}]` for a
  container shift.
- **Incarnation history.** When a pane writes `<role>.json` over an earlier one, the earlier record
  (minus its own history) is appended to `history`; `started` stays the current incarnation, so
  mailbox fencing is unchanged. `inbox` filters `inbound_unacked` to `recipient_incarnation == current`
  so a respawned member never tries to ack a dead incarnation's messages. Any discovered
  `session_id`, `transcript`, and token `usage` remain in that preserved earlier record.
- **Launcher exits.** `launch` waits for every `<Role>.json` and every slot record, prints, and
  returns. Nothing polls the shift after that.
- **spawn.** `nightshift spawn SHIFT --role ROLE [--cli claude|gemini|codex|copilot] [--crew NAME] [--slot N]` is the only way a
  crew session starts after launch, and only Foreman runs it. It refuses (non-zero, plain message) when
  the shift's window (`launch.json` hwnd) is gone, when no slot is free or the requested one is taken,
  or when the role is already live (or still starting in a slot). Slot choice: a dead role being
  respawned goes back into its own slot; otherwise the lowest free slot, dead or empty alike (a dead
  slot 2 beats an empty slot 4); `--slot N` accepts a dead slot too. `--cli gemini` runs agy, `--cli codex`
  runs Codex (`codex.exe`), and `--cli copilot` runs GitHub Copilot CLI (`copilot.exe`); `opencode` is
  refused as not yet wired (NGH-022). It appends the member to `crew.json`
  (identity `<shift>-<role>`, brief `<role>.md` if it exists else `coder.md`, the colour of a member
  with the same role else yellow, peer = the human contact; Foreman's own peer list is not rewritten),
  preserving a dead member's `role`, `label`, `identity`, `brief`, `colour`, `tool` and `model` when
  restarting it, types the pane wrapper line (paths double-quoted, `--slot N`) into that slot's cmd with
  `console.Inject`, and marks the slot with the role and a `spawned` time. A start-mode pane reads its member
  from `crew.json` (falling back to the manifest), so spawned roles run like launched ones; `status`,
  `down` and the mailbox pick them up from `crew.json`.
- **spawn --crew NAME (NGH-029).** Takes the spawned role's brief, colour, tool, model, `reasoning` and
  `account` from `crews/NAME.yaml` instead of the shift's own manifest (and over a gone member's
  record); refused when NAME has no such role. An explicit `--cli` still overrides the tool, and the
  model then falls back as without `--crew`. The member written to `crew.json` carries all of it, so the
  pane wrapper starts that tool/model/reasoning/account. This is how Foreman proves each CLI as
  Apprentice inside a live default shift: `spawn SHIFT --role Apprentice --crew apprentice-codex`.
- **down --close / --hold.** Both write the handover, kill the crew and release any applicable direct-checkout/non-git folder lock. A landed worktree is also removed with its merged branch during teardown.
  `--close` (default) also closes the window; `--hold` explicitly leaves the tab open. When the
  crew is already dead and `handover.md` exists (Foreman already ran `down`, and a caller later runs
  `down --close`), the captured `handover.md` is preserved rather than overwritten with `(pane not readable)`.
  The member whose process tree is running `down` (Foreman ending its own shift) is killed last, after the
  lock is released and the JSON (with `held`) is printed. Foreman's brief makes `down` its last act
  after writing `result.json`.

## Role boundaries and permission plan (NGH-031)

Foreman is orchestration and review only for repository work. It reads diffs, reviews reports, routes
ordinary repository coding to Apprentice first with disjoint claims, and assigns every verification pass
to Tester. Coder is the senior buddy for upfront cross-cutting, concurrency and security work, for the
no-Apprentice fallback, and for guarded uplift after a stored Apprentice escalation, two linked Tester
failures or a timeout. Tester owns test-file edits; ordinary launcher/runtime corrections also go to
Apprentice unless a senior exception applies. Guidance returns through Foreman as a fresh same-asg Apprentice assignment;
TAKEOVER remains with Coder. Foreman never
edits repository files or runs the suite as a substitute for Tester. It may write the shift records under `{state_dir}`—for
example `result.json` and `handover.md`—because those are runtime state, not repository files.

### Guarded same-asg uplift (NGH-038)

The runtime transfer is an explicit, validated mailbox message rather than a scheduler or a new top-level
command:

```text
nightshift msg send SHIFT Foreman Coder --kind uplift --asg CODING_ID \
  --reason needs-senior|tester-failed-twice|timeout --body TEXT
```

Only Foreman may send an uplift to Coder, and it must name the current Apprentice coding assignment. The
transfer is accepted only after every previous-owner claim alias is released, under the claims-then-mailbox
lock order. A stored Apprentice note whose body starts `APPRENTICE-NEEDS-SENIOR` supplies the `needs-senior`
evidence. Tester verification is a distinct assignment (`--asg VERIFY_ID`) linked to its coding assignment
with `--coding-asg CODING_ID`; only a `TESTED-PASS` or `TESTED-FAIL` result under that verification asg is
counted. Two distinct linked `TESTED-FAIL` assignments trigger `tester-failed-twice`, even if the earlier
Apprentice dispatch already reported done. Failures accumulate across consecutive same-asg correction
dispatches to the same Apprentice and incarnation. An actual coding ownership change (including uplift,
handback, a different Apprentice or a new incarnation) resets that window; delayed verdicts from verification
assignments in an earlier ownership period do not count.

The timeout trigger is at least 900 seconds from the first `sent` or `ack` delivery timestamp
of the latest Apprentice coding dispatch (`delivered_epoch` is set once with `COALESCE`), with no matching
completion. Repeating `sent` or `ack` for that dispatch does not reset its clock. A fresh same-asg correction
dispatch uses its own first delivery timestamp; legacy timestamps are never inferred. Active coding ownership is the latest coding dispatch,
independent of Tester assignments; only a result from that current coding assignee after its dispatch closes
the work. Coder returns either `GUIDANCE` or `TAKEOVER` through Foreman. `GUIDANCE` is handed back as a
fresh Apprentice assignment under the same asg after Coder claims are released.

Foreman's `inbox` exposes due transfers as `uplift_due` entries
`{asg, source_dispatch_id, reasons}` and mailbox-derived `uplift_counts`
`{crew, total, by_reason}`. Counts deduplicate source dispatches rather than bus lines and are copied by
Foreman into `result.json`.

The built Claude repo-scoped policy for the Foreman member is one inline `--settings` JSON deny rule:
`{"permissions":{"deny":["Edit(//c/actual/cwd/**)"]}}`. Claude's canonical `Edit(path)` permission covers
the built-in `Edit`, `Write`, `NotebookEdit`, and `MultiEdit` tools, so the policy must not add
redundant inert deny entries for those tool names. The path is formed from the actual repository cwd;
on Windows, `C:/...` is represented as `/c/...`, with the rule rooted as `//c/...`. This deny applies
in every Claude permission mode, including `auto`, and is intended only for the Claude Foreman member
in `start`/`spawn`. It is a tool-level policy, not OS enforcement: arbitrary subprocess writes are not
covered. See the [Claude permissions](https://code.claude.com/docs/en/permissions),
[permission modes](https://code.claude.com/docs/en/permission-modes), and
[CLI reference](https://code.claude.com/docs/en/cli-reference).

Read-only capability checks on 16 Oct 2026 (`codex --help`, `copilot --help`, `agy --help`) show the
following. Codex exposes coarse `--sandbox read-only|workspace-write|danger-full-access` and approval
controls, but no per-tool or path-scoped deny option; read-only would also complicate commits and
shift-state writes, so it is not a direct equivalent. Copilot exposes coarse `--deny-tool` and
`--excluded-tools` controls, but its help does not show a Claude-style path-scoped deny; Nightshift's
current launcher uses `--allow-all` for `permissions: skip|auto`. agy exposes coarse `--sandbox` and `--mode`
controls but no per-tool deny option and no classifier mode; Nightshift's launcher passes
`--dangerously-skip-permissions` for `permissions: skip|auto` (see the manifest `permissions:` key). These are facts about the
capabilities surfaced and wired here, not a claim that Codex, Copilot, or agy can never provide a
different equivalent. Briefs and prompts remain procedural guidance, not a substitute for a CLI or OS
permission boundary.

## The four identities (owner, 16 Oct 2026)

The top row of the 2x4 container reserves four fixed identities, one per column: **Foreman** (runs the
ticket's goal statement, sole human contact), **Advisor** (NGH-027), **Coder**, **Tester** (NGH-024).
Advisor is not started with the rest of the crew; its manifest slot remains an empty shell until the first
consultation. The **Apprentice** (NGH-029) is a fifth, cheap
coder in slot 5 (bottom row, first column) of the default, codex, codex-copilot and apprentice-* crews;
slots 6..8 stay open for `nightshift spawn`. The
Advisor is an absent-on-start frontier session (Fable, Astra, Mistral Large 4 or Gemini's top
model, whichever the manifest names). It is spawned only when Foreman is stuck on something it cannot
resolve. Then, instead of asking the human,
Foreman asks the Advisor, and the Advisor is expected to take a sensible decision. Rules that
keep it bounded: Foreman asks only after it has tried itself; the question and the answer go
through the mailbox so the human can audit every decision; the Advisor never edits files and
never talks to Coder or Tester; the human is still asked when the ticket itself is wrong or a
decision cannot be undone. This is the reviewed design's "bounded consultant" (not a council).

**Startup gate (NGH-050).** Every non-human worker member on Claude, Codex, Copilot or agy (except
the active Foreman and on-demand Advisor) sends or records exactly one READY at startup, then does
no reading, planning, exploring, inbox polling or other tool use until Foreman delivers an assignment.
The only pre-assignment tool exception is sending or recording that READY, or following explicit
replacement recovery instructions.

As built (NGH-027):

- **Manifest role.** `crews/default.yaml` has `Advisor` in column 2: brief `advisor.md`, colour
  purple, count 1, role-level `model: claude-fable-5-1` (the crew model stays Opus 5.5).
- **Reserved lifecycle.** Every manifest Advisor has a reserved slot (including a `split` layout, which is
  promoted to a slot grid for this purpose). `status` reports it as `not_started` with `reserved_slot` until
  its first consultation; it has no process or mailbox incarnation before then.
- **Brief (`briefs/advisor.md`).** On a Foreman message: `ack`, act on the stored body only; read
  context read-only (`bus.jsonl` if present, else the mailbox rows, plus ticket, claims, builds,
  code, only what the question needs); never edit, build, test, spawn, write tickets or talk to
  anyone but Foreman; go dormant again after answering.
- **Mailbox guard.** `msg send` refuses (SystemExit, nothing stored) any message to the member
  labelled `Advisor` unless the sender is the human contact ("only Foreman may address the
  Advisor"), and any message from it unless the recipient is the human contact ("the Advisor
  answers only Foreman"). Foreman <-> Advisor is an ordinary message.
- **First consultation.** `nightshift consult SHIFT QUESTION --asg adv-<n>` is the only way to start an
  absent Advisor. It uses the reserved slot, creates the Advisor incarnation, stores the ordinary durable
  `assignment` mailbox/bus row with that incarnation, and supplies its `msg:<id>` header plus the question
  as the CLI's first prompt. This prevents a pre-incarnation message or duplicate injection. Later Foreman ↔
  Advisor messages use normal `msg send`.
- **Answer format.** One decision, no options: a `result` with the question's asg and body
  `DECISION: ...` / `WHY: <two lines>`. When the ticket is wrong or the decision cannot be undone the
  first line is `ASK-HUMAN: <why>` followed by the recommended decision; that, and the routine-matters
  cases Foreman's brief already names, are the only times Foreman goes to the human.
- **Switching model or CLI later.** Astra (Codex) or Mistral Large 4 (OpenCode) is the Advisor's
  `model:`/`tool:` lines in the manifest plus the agents-cli transport the Gemini Coder already uses
  (NGH-012); the mailbox guard and briefs do not change.

### Reserved Coder (NGH-056, Go binary)

Three live sessions by default (owner, 16 Oct): when the effective crew (after `--without`/`--role`) has an
`Apprentice` role, the single manifest `Coder` is held like the Advisor.
- **start.** The Coder's cell is an empty reserved slot (a `split` layout is promoted to a slot grid), it is
  not launched, and the bus `start` event's `members` omit it. `crew.json` gains `"coder_slot": N` right after
  `advisor_slot`, only when the Coder is reserved. The `start --dry-run` plan gives the Coder role
  `"status": "not_started", "reserved_slot": N`, the same as the Advisor. `status` reports a Coder with
  no `Coder.json` as `not_started` with `reserved_slot`. Crews without an Apprentice (e.g. `work`) keep the Coder
  live, so their output is unchanged.
- **summon.** `nightshift summon SHIFT BODY --asg ID [--kind assignment|uplift] [--reason R]` is the Coder's
  sibling of `consult`, and `consult` is unchanged. It first runs exactly `msg send Foreman Coder`'s checks
  under the claims lock (`dispatchMetadata`: `--reason` needs `--kind uplift`, the uplift trigger evidence, the
  Apprentice's claims released, and no plain-assignment Apprentice -> Coder transfer). It then records
  `"summon": {kind, body, asg[, reason]}` in `crew.json` and spawns the Coder in `coder_slot`. The pane
  creates the Coder incarnation, stores the message through the same `storeDispatchMessage` path (so an
  uplift carries `source_dispatch_id`/`reason`/`evidence` and the new incarnation), marks it sent, clears
  `summon` and uses `msg:<id> header + body` as the first prompt. A failed spawn restores `crew.json`.
  Refusals: `shift X has no reserved Coder`,
  `Coder is already started; use msg send for later assignments`, `Coder summon is already starting`, plus
  msg send's own messages.
- **spawn.** Neither reserved slot is given to another label (`Coder slot N is reserved until summon`).
  `spawn --role Coder` uses only `coder_slot` and, before the first summon, refuses with
  `use summon to start the Coder`.

## Accountant (NGH-034) — proposed / unbuilt, owner review required

This section is the design deliverable only. No Accountant, accounting adapter, accounting
manifest validation, hold command or budget enforcement exists yet. `load_crew` currently
discards unknown accounting keys. Approval precedes a separate implementation ticket; this
document does not make unattended Copilot crews budget-safe.

### Role, placement and brief

Add **Accountant** as the sixth manifest member in slot 6 of a 2x4 container, leaving slots 7–8
free. Preserve each crew's existing first five members and order (the work crew orders its
Apprentice differently). Give it `count: 1`, its own brief (`accountant.md`, plus the non-Claude
protocol variant where needed), colour, tool and model. Colour is proposed as cyan; adding a
terminal scheme is follow-up work. Foreman remains the sole human contact and the Accountant's
only messaging peer. Accountant reads the whole shift's roster, member/incarnation history and
bus, but neither assigns coding work nor edits the repository nor launches sessions.

Its brief must specify: reconcile usage on startup and periodically while the crew works;
include itself, dormant Advisor, spawned helpers and dead incarnations; append observations and
decisions to the accounting log and bus; send warnings/requests to Foreman via the mailbox;
enforce the configured budget stop without waiting for Foreman. Source locations and credentials
are resolved from private configuration, never copied into the brief. Accountant starts observing
immediately rather than waiting for an implementation assignment.

For the `claude-work` crew (`crews/claude-work.yaml`), all roles run Claude **Opus 5.5**,
including the proposed Accountant. `crews/copilot.yaml` is all Copilot **Gemini 3.8 Flash**,
including the proposed Accountant. A cheap observer elsewhere does not override those owner decisions.

### Sources and normalized contract

Manifest-selected adapters produce the same versioned observations:

| Adapter | Input and attribution |
|---|---|
| `copilot_tail` | `agents-cli tail` for each shift member running Copilot; parse the current status `Session: N AIC used`, bound to its identity, PID/start identity and provider session when available. |
| `cloud_spend` | Work cloud CLI's structured spend output; member/session attribution only when explicitly supplied. Otherwise report billing-account period usage separately. |
| `usage_http` | An externally provided usage feed; the private manifest holds only its address (`ip:port`), endpoint, version and field mapping. No feed implementation, home tool name, real address or credential belongs in this repo. |

The proposed HTTP boundary is a read-only `GET /v1/usage?shift_id=<id>` returning a versioned JSON
envelope `{schema, observations}`; this is a requested contract, not an existing endpoint claim.
A private manifest may map a different endpoint/field layout to it. CLI adapters normalize locally
to that envelope. Public manifests reference a private `source_ref`; neither that reference nor
accounting events expose the resolved address, command credentials or raw provider payload.

`schema: nightshift.accounting.observation/v1` requires these fields on each observation:

| Fields | Meaning |
|---|---|
| `source_ref`, `adapter`, `contract_version`, `series_id` | Opaque source reference and stable billing-series identity; aliases/adapters exposing the same charge share dedup ownership. |
| `shift_id`, `scope`, `member_id`, `incarnation_id` | Scope is `member_session`, `shift` or `account_period`. Member scope requires identity and incarnation; other scopes use null member fields. Account scope is never attributed to this shift implicitly. |
| `period_id`, `period_start`, `period_end` | Provider period identity and RFC3339 UTC bounds for period usage; null for a session-only metric. Daily policies explicitly supply their timezone. |
| `captured_at`, `observed_at`, `sample_id`, `counter_epoch` | RFC3339 UTC collection time, provider observation time (nullable), provider ordering/dedup key (nullable), and confirmed counter generation. |
| `dimension`, `value`, `unit`, `currency`, `counter_kind` | Valid value is a nonnegative finite decimal string with declared source precision; null when no usable reading exists. Dimensions are `credits`/`AIC`, `tokens`/declared token category, or `money`/currency code. Currency is required for money and null otherwise. Counter kind is `cumulative` or `delta`; delta requires a stable dedup sample ID. |
| `status`, `freshness`, `error`, `evidence` | Status `valid`, `missing`, `stale`, `parse_error`, `unavailable` or `not_applicable`; freshness `confirmed`, `stale` or `unknown`; nullable error code and redacted match/digest/reference. |

Select **one authoritative source per billing series and metric**. A second source is corroboration,
not extra spend. Unresolved overlap or missing attribution degrades coverage. Sum whole-shift usage
across all tools only within compatible dimension/unit/currency groups; keep AIC, token categories
and each currency separate. Account-wide cloud spend can enforce an account-period limit but is
never multiplied by panes or labelled a shift total. An all-tools shift total is complete only when
every charge-bearing member has attributable coverage; unavailable cost for a tool stays unknown.

For Copilot, accept one unambiguous current status value using source-declared numeric precision,
not an integer-only assumption. Reject negative, NaN, infinite, malformed, ambiguous or unsupported
formatting as degraded readings. Explicit reported zero is valid; missing text is not zero. Preserve
only the matched status line, collection provenance and digest, not unrelated pane conversation.

`captured_at` is not provider freshness. Copilot's status line has no timestamp: even a successful
live tail with an unchanged value cannot prove when the provider refreshed it. Record source age
as unknown unless independently evidenced; repeated polls cannot refresh known stale data. The
manifest declares poll interval, collection timeout, freshness evidence/max age and the policy for
unknown/stale coverage. Those values need owner review before unattended use.

### Accrual, restarts and accounting records

A fresh session's counter starts at zero at creation. Its **first observed value counts in full**,
including spend before the first poll; do not subtract the first reading. Sum each Copilot session's
accrued value across every member, including Accountant. Later cumulative samples add only increases
over that series' persisted high-water; repeated samples add nothing. For example, first readings
17 and 3 AIC total 20, not zero; rereading them still totals 20. Never sum successive snapshots.

Keep previous incarnations' accrued spend when a role respawns and add the fresh incarnation's first
value in full. A lower reading alone is not a reset: 17, delayed 14, then 20 in one session accrues
20. Use source sample ordering when available and retain the high-water. Start a new counter epoch
only on confirmed reset/session identity evidence; its first fresh value counts in full, retaining
the old epoch. Otherwise record degraded uncertainty, preserve accrued spend and apply the configured
unknown-data policy. Reused/resumed sessions need provable pre-shift history for any baseline
subtraction, with attribution evidence logged; no free reset or guessed baseline is allowed.

Proposed shift-local records (under the shift's existing state directory, including legacy roots):

- `accounting.jsonl`: append-only versioned snapshots and decisions, one complete JSON object per
  line. A snapshot has `schema: nightshift.accounting.snapshot/v1`, `snapshot_id`, `shift_id`,
  `captured_at`, `observer_incarnation`,
  `observations` (the contract above), `totals` grouped by scope/period/dimension/unit/currency,
  `quality` (`complete`, `degraded`, `unknown`), missing coverage and evaluated budget IDs.
  Totals distinguish a retained **known subtotal** from a complete current reading.
- `accounting-state.json`: atomic checkpoint of replay position, per-series high-water/epoch and
  sample ordering, source ownership, accrued totals, warning/action latches and budget-period IDs.
  Snapshot IDs combine unique observer incarnation and sequence; restarting Accountant restores
  the checkpoint and replays unprocessed ledger entries idempotently, never resets the budget.
  Partial trailing log records are detected, not treated as valid zero usage.
- `budget-stop.json`: durable terminal decision/evidence, described below; survives teardown.

Accountant is the single accounting writer. Flush the ledger record before its checkpoint advances;
recovery applies each identified record once. Decisions cite a snapshot ID, budget ID, threshold,
known subtotal, quality/unknown members and reason. Mirror snapshots and transitions onto `bus.jsonl`
as proposed `accounting_snapshot`, `accounting_warning`, `accounting_hold`, `accounting_release`,
`accounting_rejig`, `accounting_stop` and `accounting_error` events carrying the same IDs and compact
redacted fields. The accounting log is the durable accounting evidence: current `bus_append` is
best effort. Record a failed mirror for later reconciliation; missing bus delivery must not prevent
a budget stop. This is a log-first design, with no dashboard.

### Budgets and levels of action

Proposed manifest shape (not accepted by the current loader; numbers are examples, not defaults):

```yaml
accounting:
  level: 3
  sources:
    - {source_ref: work_copilot, adapter: copilot_tail, contract_version: v1}
  budgets:
    - {id: shift-aic, scope: shift, dimension: credits, unit: AIC,
       limit: "100", warn_at: "80", action: down}
    - {id: period-aic, scope: period, source_ref: work_account,
       dimension: credits, unit: AIC, limit: "500", warn_at: "400",
       period: provider_billing_period, action: hold}
```

The work-account source in that illustration must be separately configured with real account-period
coverage; summing this shift's session lines cannot reveal monthly account spend. Private configuration
also supplies polling/freshness/unknown-data policies. Validate positive limits, `0 < warn_at < limit`,
source coverage, units, period definition/timezone and authority before enabling enforcement. **100 AIC
is the owner's example, never a shipped default.** Budgets are per crew, persisted in effective shift
configuration; local values are set on your machine. Shift budgets run across period boundaries and respawns.
Period budgets use provider period IDs/reset boundaries (or an explicit daily timezone), not a reset
inferred from a lower value; source-wide period totals include other shifts without reattribution.

| Level | Authority and routing |
|---|---|
| 1 — observe | Reconcile and log usage/quality; no spend action. |
| 2 — warn | Level 1 plus a durable warning and mailbox note to Foreman at the lower threshold. Deduplicate per budget/period. |
| 3 — control | Level 2 plus the mandatory shift-budget `down` rule below. Optional period holds/releases require the agreed mechanism and policy. |
| 4 — rejig | Level 3 plus propose moving bounded work to Apprentice or changing a slot's tool/model. Foreman owns assignments, claims, handovers and any `spawn`; Accountant never launches a replacement. Direct reassignment authority remains unapproved. |

These levels describe capability, not a way to opt Copilot out of the owner's stop rule. The minimum
unattended Copilot design requires warn plus enforced termination (level 3) and a per-shift AIC budget.
All-Copilot model changes remain subject to every role using Gemini 3.8 Flash; all-Claude work roles
remain Opus 5.5. Rejig cannot silently override either policy.

At **known accrued shift AIC >= limit**, Accountant invokes `nightshift down <this exact shift>`
itself, without asking Foreman. It must not use `latest` or touch another shift. Unknown members do
not block termination when the known subtotal already reaches the limit. If the first sample crosses
both thresholds, persist/emit the warning then stop immediately; do not wait for warning acknowledgement.
When below the limit but coverage is uncertain, retain prior spend, warn with degraded quality and
apply the owner's explicit unknown-data policy; never certify the shift under budget from missing data.

Before invoking `down`, persist the final accounting decision and `budget-stop.json` with reason,
limit/known total/unit, coverage, snapshot/evidence references and time. Record unfinished work as
`result.json` **blocked**, with reason `budget_exhausted` and only the stage/evidence actually earned;
never claim `done` because a budget ended the crew. Then attempt the correlated bus/mailbox stop notice
and run `down` regardless of notification acknowledgement. Current `down` captures handover and emits
result/down events before killing its caller last, so the stop evidence must already be durable.
Teardown errors remain a stop failure to inspect; do not assert termination merely from requesting it.

The build ticket must make the stop latch visible to `warden`, `spawn`, `loop`, `resume` and future
dispatch: no automatic resurrection/retry or new baseline after a budget stop. Current blocked results
stop the loop and suppress core-slot recovery, but `resume` still selects unfinished tickets. Resumption
therefore needs an explicit approved budget extension or a permitted new period, linked history and
retained shift spend; a period rollover alone cannot clear an exhausted per-shift budget.

### Holding, release and enforcement limits

A proposed **cooperative hold** is separate from termination. Only Accountant issues a versioned
`accounting-hold.json` with hold ID, targets, reason, budget/period, issuing snapshot and release
condition; Foreman can request it and routes notices. Members check before accepting assignments,
starting chargeable turns/builds or spawning, stop taking new work and acknowledge the hold ID through
Foreman. This cannot interrupt an in-flight model turn, already-running build or uncooperative member.
Accountant keeps observing (its own usage counts). If not acknowledged within the configured grace,
log/warn; any escalation beyond the mandatory AIC stop needs an approved policy. No hold/release CLI
exists yet. Existing **`down --hold` kills the crew and only leaves the tab open**; it is never a pause.

At a confirmed period rollover, Accountant can release its hold only after validating fresh allowance,
all remaining budgets and the configured release condition, recording the hold ID and notifying Foreman.
Release does not respawn members, erase accrued shift usage, clear a terminal stop latch or reopen
assignments itself. Whether optional period holds are issued directly or requested through Foreman,
and whether level 4 permits more than recommendations, remain owner questions; mandatory AIC teardown
authority is already decided.

This proposal relies on the interactive Accountant's periodic model/tool polling; the launcher exits
and there is no independent watchdog here. Detection and teardown lag includes polling, tool timeout,
model scheduling, provider reporting delay and `down` runtime, with additional in-flight spend. A stalled
or dead Accountant cannot enforce anything; source freshness can be unknown. It is a budget-triggered
stop, **not an exact billed-spend cap**. If those limits are unacceptable for unattended work, a separately
approved deterministic enforcement mechanism is needed before such use.

### Review questions and follow-up acceptance

Owner still needs to choose actual shift/period limits, lower warning thresholds, poll/timeout/staleness
and unknown-data policy (including observer failure), provider period/timezone, optional hold/grace/release
authority, and whether level 4 may do more than ask Foreman. Cloud feed availability, attribution and
private mappings must be confirmed on your machine. These questions do **not** reopen warn-then-kill for Copilot.

Implementation acceptance must cover: every member including Accountant/dormant/spawned roles; first
sample spend; decimal AIC; repeat/delayed samples; confirmed reset vs unexplained decrease; respawn and
observer restart; duplicate adapters; unknown/stale/malformed values; period rollover with retained shift
usage; account totals without false shift attribution; known-subtotal stop with missing panes; exact-budget
stop and first-sample warning+stop; durable evidence before self-teardown; and no automatic restart/reset.
This ticket changes architecture only; saved fixtures and runtime verification belong to the build ticket.

## Components

| Piece | Role | Where |
|---|---|---|
| `nightshift_shifts.py` | the launcher/oracle: `start` (`--no-worktree` supported by NGH-040), `status`, `down` (`--close` default, explicit `--hold`), `spawn` (NGH-025), `land` (NGH-040), `cleaner`, `warden`, `gaffer` and `floor.json` (NGH-035), `floor say/hear` (NGH-053), the optional `loop`, `resume`, `claim/release/claims`, `msg/inbox/ack` (NGH-019), `build` (NGH-020), and `log` (the shift bus, NGH-026) | this repo |
| `cmd/nightshift` | standalone Go CLI: `status`, `claims`, `inbox`, `log`, `claim/release`, `msg/inbox/ack`, `build`, and native `land`, `down`, `cleaner` lifecycle operations with handover reporting | this repo |
| `crews/*.yaml` | crew manifests: roles, counts, model, permission mode, colours, which brief each role gets, each role's `tool` (`claude`, `agy`, `codex`, `copilot`), `model`, optional `reasoning` (`low`\|`medium`\|`high`\|`xhigh`; copilot also `none`\|`minimal`\|`max`), `context` (`default`\|`long_context`, copilot only), `account` (`1`\|`2`), `permissions` (`skip`\|`auto`\|`default`) and the top-level `max_credits` (the shift's Copilot credit budget, split evenly across the Copilot panes), pane layout | this repo |
| `briefs/*.md` | one page per role (`foreman.md`, `advisor.md`, `coder.md`, `tester.md`, `apprentice.md`, `decompiler.md`, `researcher.md`, `uitester.md`; NGH-063), rendered by `renderRoleBrief` with the task first for Foreman; the pre-NGH-063 set (`common.md`, `*-cli.md`, `coder-agy.md`, `foreman-agy-coder.md`) is kept in `briefs/legacy/` for crews with `briefs: legacy` | this repo |
| `personas/*.yaml` | each role's persona (identity, what good looks like, how you work, never), rendered above the brief (NGH-032) | this repo |
| shift folder | `C:\nightshift\shifts\<shift>\` — the source of truth for one new shift (below) | data, not code |
| tasks-cli | the work queue: the loop takes ticket IDs, reads `codebase:` for the working folder, crews write notes, the loop moves status | on PATH |
| `internal/console` | the console driver (NGH-068, a hard fork of agents-cli's console code, which is no longer a dependency): peek a pane, tail it with a guessed state, send a line per tool with submit verification, inject raw text, press a key | this repo |
| Claude Code mesh | `ListAgents` / `SendMessage` between Claude sessions over named pipes (`~/.claude/sessions/<pid>.json`) | Claude Code |
| Windows Terminal | one window per shift, one pane per role; a "Nightshift Crew" profile (Cascadia Mono 12, Campbell) | `wt.exe` |
| `nightshift.exe` | standalone binary built via `scripts\build.cmd` | on PATH (default `C:\nightshift\bin`) |

## Console driver (NGH-068)

`cmd/nightshift` drives panes through its own package `nightshift-readonly/internal/console` (owner decision 18 Oct: a
hard fork of agents-cli's console code; `C:\git\agents-cli` is read-only reference, never a go.mod dependency,
vendored copy or sync source). agents-cli is no longer needed on PATH and `doctor` no longer checks it.

| Function | Was | Does |
|---|---|---|
| `Peek(pid, above)` | `agents-cli _attach peek` | the pane's visible lines (`Screen.Lines`, `Title`, `Width`) |
| `Tail(pid, tool, lines)` | `agents-cli tail <pid> --lines N` | the last N lines plus `Screen.State` (`busy`, `idle`, `prompt`, ...) guessed for the tool |
| `Send(pid, tool, text, enter)` | `agents-cli send <pid> <text> --force` | the per-tool send (copilot, agy, gemini, else inject plus the submit verifier); returns submitted |
| `Inject(pid, text, enter)` | `agents-cli _attach inject` | raw console input, no verification |
| `Key(pid, tool, key)` | `agents-cli key` | a named key (`enter`, `down`, `escape`, ...) |

Callers: `floor_patrol.go` (`agentTail`, `headerSubmitted`, `headerInComposer`, `sendHeader`; the tool now comes from
`<Role>.json`, empty meaning claude), `write_bus.go` (nudges; a failed one records reason `console send did not submit
the nudge`), `patrol.go` (budget-stop escape), `usage_sample.go` (`paneScreen`), `lifecycle_report.go` (`paneTail`),
`launcher_spawn.go` (the slot wrapper line), `launcher_pane.go` (`peekLines`, `trustScreen`, trust-prompt keys,
`injectCommand`). `agents-cli name <pid> <identity>` (`registerName`) is dropped: it only registered the identity in
agents-cli's own registry for humans and other tools; nightshift always addresses a pane by pid.

Attaching to another console is done by a child process: the parent re-executes the helper binary with the hidden
`nightshift _attach <op> ...` subcommand (`console.HelperMain`; same argv, stdin and JSON protocol as agents-cli's
helper, text on stdin never argv, started detached and hidden). The helper binary is `os.Executable()` unless
`NIGHTSHIFT_CONSOLE_HELPER=<exe>` names another one; that variable is the only test seam (pytest stubs are small
`_attach` helpers rather than a fake agents-cli on PATH).

## A shift, step by step

1. `nightshift start <TICKET|"task"> [--crew NAME] [--cwd DIR] [--continue-from SHIFT] [--no-worktree]`
   reads the ticket through tasks-cli and resolves the base working folder (`--cwd` > ticket
   `codebase:` > current directory). For a git repository, the default is an isolated
   `C:\nightshift\worktrees\<shift>\` cwd: it creates branch `shift/<ticket>-<shortid>` from the
   target branch (default `main`) and records `repo_root`, `worktree_dir`, `branch`, `target_branch`,
   `base_sha` and `created_at` in `<state_dir>/worktree.json`; every pane runs from that worktree.
   `--no-worktree` opts into direct-checkout mode. Non-git folders also use direct mode; both modes
   take the **folder lock** (`C:\nightshift\locks\`, exit 3 if another live shift holds it). The
   worktree mode does not hold a whole-repository folder lock while the crew codes and tests. The
   command writes `prompt.txt` and runs `wt.exe` with one `new-tab`/`split-pane` per role. Each pane
   runs this same script as a **wrapper** (`--pane ROLE`), which launches the role's CLI, records
   `<Role>.json` (pid, name, start time, cwd), answers the CLI's own trust dialog if it appears, sets the
   role colour, and waits for the CLI to exit. `<Role>.json` also records the role's `tool` (claude or
   agy), which `status` reports per role.
2. Each Claude member starts with `--name <shift>-<role>`, `--permission-mode auto` (owner
   decision: classifier-guarded, never `manual`, never bypass, unless its manifest role sets `permissions:`), and
   `--append-system-prompt` = its brief. The wrapper scrubs inherited `CLAUDE*` env so a crew
   launched from inside a Claude session is not a child session of it. The human's foreground
   window is restored once the crew window exists.
3. The crew works. Foreman is the only member that talks to the human; Coder and Tester act only
   on Foreman's assignments (handoff below), and the Advisor sleeps until Foreman consults it. Rules the briefs enforce: never launch sessions/windows/agents
   yourself; the only way a session starts is Foreman's `nightshift spawn` into a free slot (anything else:
   write the command for the human); claim files before editing (`claims.json`, exit 4 on
   conflict); record messages in the mailbox before sending them (NGH-019).
4. Tester first passes the reviewed revision; Foreman commits it, then Tester reports `TESTED-PASS` for the
   exact unchanged HEAD of that clean commit. While work is in progress, Tester runs targeted tests for the changed
   area with `nightshift build --cmd`; the full configured build (`.\verify.cmd`: Go build, go test, Go golden comparisons
   under xdist; Python suite no longer in it, Python launcher frozen) runs once, on the final clean commit before land.
   Foreman runs `nightshift land <shift>` before writing `result.json`; landing refuses dirty or untested changes and
   does not create the commit. Exit `0` records `landed.json`. If `main` moved ahead, `land` rebases: a disjoint clean
   rebase carries over the passed build and lands in the same call (`decision: "build_carried"` in the `land_rebase`
   bus event, exit `0`); an overlapping clean rebase records `decision: "build_required"` on the bus and returns exit `2`
   (`rebased_needs_test`), requiring one Tester full build pass on the rebased HEAD before Foreman reruns `land`;
   exit `4` means a rebase conflict and requires the shift's Coder to resolve it, followed by a Tester pass, Foreman's
   commit, clean-commit verification and another `land`. A non-git or `--no-worktree` shift cannot land and follows the
   direct-checkout result path.
5. Foreman then runs `nightshift down --close` itself: it captures the last lines of each pane, writes
   `handover.md` (result, commits since start, test outcome, denials, claims, pane tails), kills the
   crew processes (Foreman's own tree last), releases the applicable lock, and closes only that
   shift's Terminal window. For a successful land, teardown removes the worktree and merged branch;
   for blocked, halted or crashed recovery without a land, it preserves the unmerged branch and
   metadata so the work can be recovered. `--hold` is an explicit override when the tab must remain open.

## The default crew: Foreman, Advisor, Coder, Tester, Apprentice (NGH-024, NGH-027, NGH-029)

- `crews/default.yaml` is Foreman (red) in slot 1, an initially empty reserved Advisor slot (purple,
  Fable, `advisor.md`) in slot 2, Coder (green) and Tester (blue, `tester.md`) in slots 3..4, and
  Apprentice (yellow, `apprentice.md`, agy on `gemini-3.8-flash-high`) in slot 5, leaving slots 6..8
  as empty cmd shells for `spawn`. Foreman's peers are `<shift>-advisor, <shift>-coder, <shift>-tester, <shift>-apprentice`;
  everyone else's peer is `<shift>-foreman`. The reserved Advisor startup and this worker idle gate
  apply to every crew manifest; other crews (`--crew NAME`) may vary their roster, tools, models,
  colours and slots.
- Apprentice (NGH-029, NGH-038): ordinary coding goes to Apprentice first with bounded work and an
  explicit file list disjoint from Coder's claims. Coder is the senior buddy for upfront cross-cutting,
  concurrency or security work, the no-Apprentice fallback, and validated uplift after the guarded
  triggers; Tester owns test-file edits. Output is reviewed by Foreman and goes through Tester exactly
  like Coder's. `START_MESSAGES['Apprentice']` identifies it as the primary coding role and tells it to
  send READY and await assignments. A non-Claude Apprentice is driven like the agy Coder (mailbox
  `msg send` → `agents-cli send` → `msg sent`, `agents-cli wait --match
  'APPRENTICE-DONE|APPRENTICE-BLOCKED'`, `tail`, ack); Foreman relays Tester verdicts and any Coder
  `GUIDANCE` as mailbox messages under the same asg.
- Per-CLI Apprentice manifests (2x4, the default Claude Foreman/Advisor/Coder/Tester plus Apprentice):
  `crews/apprentice-gemini.yaml` (agy, `gemini-3.8-flash-high`), `crews/apprentice-codex.yaml` (codex,
  `gpt-5.6-luna`, account 2), `crews/apprentice-copilot.yaml` (copilot, `gemini-3.8-flash`; `gemini-3.8-flash-high` is not in the
  Copilot catalog and falls back to Auto). They
  serve both `start --crew NAME` and `spawn --crew NAME`. OpenCode and Qwen have none yet: they are not
  in `TOOLS`, so `load_crew` would refuse the manifest.
- Apprentice CLI rotation (NGH-029), one live shift per CLI in this order, each recorded on the ticket
  with what broke and what was fixed: Gemini (agy, `apprentice-gemini`), Codex Luna on account 2
  (`apprentice-codex`), OpenCode with Mistral and Qwen (both waiting on a `TOOLS` entry), then Copilot
  CLI (`apprentice-copilot`, once TECH-225 lands). A live round trip is READY, ack of the assignment,
  the done report and the relayed Tester verdict, all on the shift bus.
- Handoff is Apprentice or Coder -> Foreman -> Tester. Foreman reviews the coding result and assigns
  verification to Tester with its own `--asg` id and `--coding-asg` link, saying what changed and what
  was asked. Coder and Tester never talk to each other; a senior `GUIDANCE` handback returns through
  Foreman as a fresh Apprentice assignment under the original coding asg.
- Tester runs the suite through `nightshift build` so the evidence is revision-bound and reviews the
  diff against the assignment. It edits test files only (claims disjoint from the current coding owner's)
  when verification requires; product, nit, or typo fixes are not made by Tester. Foreman verifies the
  resulting revision flow and commits only after Tester passes it.
- Its report starts `TESTED-PASS` or `TESTED-FAIL` and cites `builds/<n>.json` (head, dirty, exit),
  test counts, findings and the test files it changed. A TESTED-PASS on a clean tree is the
  tested-rung evidence Foreman puts in `result.json`; with a Tester present Foreman does not run the
  suite itself. The first TESTED-FAIL returns to the same coding owner under the same coding asg; two
  distinct linked failures during one Apprentice ownership period permit guarded same-asg uplift to Coder.
  Without a Tester, Foreman cannot
  claim the tested rung and reports the highest supported rung as short or blocked.

## Mixed crews: per-role tool and model (NGH-012)

- A manifest role may say `tool: claude` (default), `agy`, `codex`, or `copilot`; anything else is
  refused naming the allowed tools. `model:` on a role overrides the crew `model:` for that role only.
- Permissions are manifest data (NGH-029): a role may say `permissions: skip | auto | default` (anything
  else is refused); without it the role takes the crew-level `permission_mode` (default `auto`).
  `permission_flags(tool, permissions)` is the only place the flags are chosen, when the pane command is
  built: claude `skip` = `--dangerously-skip-permissions`, `default` = no flag, anything else =
  `--permission-mode <value>` (so `auto`, or a crew-level `plan`); agy, codex and copilot have no
  classifier mode, so `skip` and `auto` both mean approve-all (agy `--dangerously-skip-permissions`;
  codex `--dangerously-bypass-approvals-and-sandbox`; copilot `--allow-all`) and `default` (or any other value)
  adds nothing. The key travels into `crew.json` and through `spawn` (including `--crew NAME`), and
  `start --dry-run` shows each pane's resolved `permission_flags`. The Apprentice runs `permissions: skip` in
  `default.yaml` and the three `apprentice-*` manifests; Claude roles keep `auto`.
- An agy pane runs `agy` from PATH (or `%LOCALAPPDATA%\Microsoft\WinGet\Links\agy.exe`) with
  `--model <model> <permission flags> -i "<brief>\n\n<opening message>"` (`agy_command`). agy has no
  launch-time `--name` or system-prompt flag, so the brief is prompt text and `agents-cli name <pid> <identity>`
  is transport-only. The launcher keeps the exact `<ticket> shift` Windows Terminal title, applies the manifest
  Windows Terminal colour scheme and background, and configures agy's supported
  `~/.gemini/antigravity-cli/settings.json` `statusLine` command as `nightshift statusline` (invoking the running
  executable): per-pane `NIGHTSHIFT_ROLE`, `NIGHTSHIFT_IDENTITY` and manifest colour environment values make that
  persistent TUI line show the visible role and identity through redraws. It preserves a different existing user
  `statusLine` command and emits a launch diagnostic that the Nightshift role line is unavailable in that pane. `/color` is Claude-only; the trust dialog is
  answered per tool (below).
- Similarly for Copilot panes, `start` configures Copilot's user `statusLine` command in
  `%COPILOT_HOME%\settings.json` (or `~/.copilot/settings.json`) as `nightshift statusline`: per-pane
  `NIGHTSHIFT_ROLE`, `NIGHTSHIFT_IDENTITY` and manifest colour environment values make that
  persistent TUI line show the visible role and identity through redraws. It preserves a different existing user
  `statusLine` command and emits a launch diagnostic that the Nightshift role line is unavailable in that pane.
- `pid_alive` counts `agy.exe` as a crew process alongside `claude.exe`/`node.exe`.
- `start --dry-run` adds `tool`, `model`, `exe` and `command` to each non-Claude role; `command` is the
  argv the pane will run, built by the same `tool_command` that `pane()` uses, with `<brief>` and
  `<first message>` as placeholders. `reasoning` and `context` are shown when set. A Claude role on another
  model than the crew's (the Fable Advisor) adds `model`; Claude roles on the crew model keep the
  original shape (the default crew's plan is golden-tested byte for byte).
- Reasoning is per tool (NGH-037). `reasoning:` is checked against `TOOL_REASONING_LEVELS.get(tool,
  REASONING_LEVELS)`. Copilot accepts `none|minimal|low|medium|high|xhigh|max`; every other tool accepts
  `low|medium|high|xhigh`. Anything else is refused, naming the role, the tool and the allowed levels.
  Claude passes the level as `--effort`, Codex as `-c model_reasoning_effort=<level>`, and Copilot as
  `--reasoning-effort <level>`. agy has no reasoning flag.
- `context:` (NGH-037) selects a context tier, `default` or `long_context` (`CONTEXT_TIERS`). It is accepted
  only on tools in `CONTEXT_TOOLS` (copilot), and is refused on any other tool or with any other value.
  It reaches Copilot as `--context <tier>`, and is carried like `reasoning` through `crew.json`, `spawn`
  and `pane()`. Without `reasoning` or `context`, `copilot_command` builds the same argv as before.
- `max_credits:` (NGH-041) is a top-level manifest key: the shift's GitHub Copilot AI credit budget
  (owner, 16 Oct: per whole shift, not per pane). `load_crew` splits it evenly (integer floor) over every
  Copilot pane (the sum of `count` over `tool: copilot` roles) and gives each such role its share as
  `max_credits`. It refuses a value that is not a whole number (bool included), a crew with no Copilot
  panes, and a share below 30 (`COPILOT_MIN_CREDITS`, Copilot's minimum per `copilot help limits`), naming
  the budget, the pane count and the share. A per-role `max_credits:` is refused and points at the
  top-level key, so the budget has one source of truth. The share is carried like `reasoning` through
  `crew_members`, `crew.json` (which also records the budget as top-level `max_credits`), `spawn`
  (respawn and `--crew` included, same-tool only) and `pane()`, and reaches Copilot as
  `--max-ai-credits <share>` only, a soft cap per pane. `spawn` applies the shift budget to every copilot
  spawn (the launcher's per-pane share, the replaced pane's share returned to the pool, refused with 'no budget share left'
  when the rest is below the share or 30), and `spawn --dry-run` prints the member and pane argv. No
  `--usage-output-file`: `--no-auto-update` runs the bundled Copilot 1.0.74, which rejects that option and exits 1 at launch.
  `start --dry-run` shows the budget first in the plan and each Copilot role's share.
  Before writing `result.json`, the Claude Foreman (`foreman.md`) reads each Copilot pane's credits from
  `<state_dir>/<label>-usage.json` when present, else from `/usage` sent with agents-cli and read from its
  tail, and records
  `"usage": {"budget": B, "total_used": T, "panes": {"<label>": {"credits_used": n, "cap": share}}}`.
  `COPILOT_MODEL` (the default for a spawned copilot role) is `gemini-3.8-flash`.
- `crews/claude-copilot.yaml` (NGH-041, 2x4): Foreman, Advisor, Coder and Tester on Claude `claude-opus-5-5`
  (`foreman.md`, `advisor.md`, `coder.md`, `tester.md`); Apprentice is the only Copilot pane on
  `gemini-3.8-flash` with `reasoning: high`, `context: long_context`, `permissions: skip` (`apprentice.md`), and
  a shift budget `max_credits: 30`. Owner decision 16 Oct 12:52: the Apprentice is the primary coder on Flash
  and the Coder the senior buddy, paid only on uplift (NGH-038); Apprentice is listed before Coder for NGH-038
  Apprentice-first routing. The owner changes the budget in that one line.
- Mixed crews under a Claude Foreman: `foreman.md` drives every member whose `crew.json` tool is not
  claude only through the mailbox plus agents-cli (msg send, `agents-cli send` header and text, msg sent,
  `agents-cli wait --match '<ROLE>-DONE|<ROLE>-BLOCKED'`, tail, ack), including a non-Claude Tester's
  verification handoff, whose stored body starts with `TESTED-PASS` or `TESTED-FAIL`. Claude members stay
  on SendMessage. `foreman-cli.md` crews are unchanged.
- `crews/copilot.yaml` is the all-Copilot work crew (2x4, every role `tool: copilot`, `permissions: skip`).
  Every role runs `gemini-3.8-flash` with `reasoning: high` and `context: long_context` (owner's
  16 Oct budget decision; NGH-034's proposed Accountant follows the same rule).
- `crews/gemini-coder.yaml` is the first mixed crew: Claude Foreman (`foreman-agy-coder.md`), agy
  Coder on `gemini-3.8-flash-high` (`coder-agy.md`). The agy Coder cannot use ListAgents/SendMessage:
  it writes reports in its own window, each ending with `CODER-DONE` or `CODER-BLOCKED`. Foreman
  assigns with mailbox `msg send` → `agents-cli send <coder> "<header> <text>"` → `msg sent`,
  waits with `agents-cli wait <coder> --match 'CODER-DONE|CODER-BLOCKED' --timeout 30m`, reads
  `agents-cli tail`, and checks the marker ends the new report, not an old one. Its proactive check
  is `agents-cli ls`/`tail` instead of ListAgents.

## Window and pane titles (NGH-023, NGH-028)

- NGH-070: every `start` pane, crew panes and empty container slots alike, is titled
  `shift-<id> | <ticket>` (or just `shift-<id>` for a free-text task). So the
  window reads the same whichever pane has focus. `--suppressApplicationTitle` keeps the CLI from
  overwriting it. Copilot needs `COPILOT_DISABLE_TERMINAL_TITLE=1` because
  `--suppressApplicationTitle` alone does not hold. One helper (`shiftTitle`) builds it for
  `wt_command` and the dry-run plan (`roles` and `slots`). Before NGH-028 panes were
  `<ticket or shift> <Role>` and empty slots
  `<ticket or shift> slot N`.
- The legacy trial modes keep `Nightshift | <Role> | <trial>`.
- `loop` sets its own console title to `Nightshift <first>..<last>` (`Nightshift NGH-019` for one
  ticket; `loop_title`), and a finished loop closes the Terminal window with that title.
- `down` closes the hwnd recorded in `launch.json`; without one it falls back to the single window
  with the new shift title, or legacy `<ticket or shift> shift`, older `<ticket or shift> <Role>`, or a bare
  `Foreman`/`Coder` for shifts from before NGH-023.

## Codex identity, colour and ticket extraction (NGH-033)

Codex panes add an ordered footer (`tui.status_line`) of model (including reasoning), context remaining,
estimated thread cost and thread credits (NGH-067; the cost items are what the patrol's footer parser reads). It
deliberately omits the current directory and, since NGH-067, the thread title: Codex 0.163 has no start-time
name or title option (`codex --help`; `resume`, `archive` and `queue` only accept an existing "session name"),
`set_thread_title` exists only on the app-server, which crew panes run without (`--no-daemon`), and an injected
`/rename` reached the model as an ordinary message (owner, 18 Oct), so the footer showed Codex's own auto-title
instead of the role. Codex panes are therefore not renamed: the launcher no longer types `/rename` into them
(`setCodexRoleName` was removed), and a Codex pane's identity comes from its per-role colour scheme and its
pane title.

The five named Windows Terminal schemes are Nightshift red, purple, green, blue and yellow, with
one Campbell base profile. `install_profile` registers the schemes; initial `--colorScheme` selects
the scheme for each Codex pane. The matching pane background is applied with OSC 11 and VT enabled
before Codex launch, including
spawned and reused Codex slots. Codex itself emits OSC 111 (reset the default background), which undoes that
tint and leaves the slot's Windows Terminal scheme, so the scheme is the colour that shows (NGH-067): `start`
gives every non-Claude member's pane its `Nightshift <colour>` scheme, and since NGH-067 also the reserved
Advisor and Coder slots (opened as plain shells and started later by `consult`/`summon`), from the colour of the
member reserved there. A member spawned into an ordinary free slot keeps that slot's base scheme. Campbell `#0C0C0C` is restored on exit and before non-Codex reuse.
No per-role Codex syntax-theme override is required.

The launcher extracts a literal `Ticket <ID>:` header from the first line of the prompt or UTF-8
prompt file, with `<ID>` matching `TICKET_RE`. Extraction precedes the continuation handover append.
It writes the ID to `ticket.txt` and uses it for the common `<ticket> shift` title.

## Claude role colour startup (NGH-047)

Claude's installed CLI exposes no supported independent colour launch flag. Its hidden
`--agent-color` option belongs to Teammate UI sessions and is consumed with `--agent-id`,
`--agent-name` and `--team-name`; Nightshift does not enable that teammate metadata for ordinary
crew members. The launcher therefore starts Claude without the opening prompt, accepts the trust
dialog, injects `/color <manifest colour>` into the empty composer, and waits for the
`type: "agent-color", agentColor: <colour>` transcript record before injecting the first role
message. A failed or timed-out colour confirmation leaves the opening message unsent and reports
the failure visibly. This ordering is shared by normal and spawned panes, while preserving the
owner's global Claude theme, the common ticket/shift title and the existing Campbell pane scheme.

## Copilot identity, colour and terminal setup (NGH-039)

- `copilot_command` passes `--name '<pane label> (<identity>)'` so the role is the Copilot session
  name. Nothing is typed into a Copilot pane to name it (nor, since NGH-067, into a Codex pane).
- `crew_env` sets `COPILOT_DISABLE_TERMINAL_TITLE=1` for Copilot only. Copilot's
  `updateTerminalTitle` retitles the pane to `"<task> - GitHub Copilot"`, which beats
  `--suppressApplicationTitle`. The env var is the documented opt-out and keeps the
  `'<ticket> shift'` title.
- Copilot asks once per terminal it detects as VS Code, Cursor or Windsurf (remembered in
  asked_setup_terminals); its check returns early when COPILOT_SETUP_TERMINAL is the string false,
  before any terminal detection, so crew_env sets it for Copilot panes and no pane waits on the
  prompt; Nightshift writes no Shift+Enter binding.
- Copilot panes get the same per-role Windows Terminal scheme (Nightshift `<colour>`) and the
  same OSC 11 background tint as Codex panes, including the Campbell restore on exit. Claude
  and agy stay on Campbell.

## Personas above the protocol (NGH-032)

Each role's persona lives in `personas/<role>.yaml` (`role`, `identity`, `good_looks_like`,
`how_you_work`, `never`). `role_brief` renders it first, as markdown: a `# <Role>` heading, the
identity paragraph, then "What good looks like", "How you work" and "What you never do" as bullet
lists, then `---` and a `# Protocol` heading, followed by `common.md` and the role's brief (the legacy set;
since NGH-063 the current set has no `# Protocol` section or `common.md`, see "One-page briefs"). The
placeholders (`{identity}`, `{shift}`, `{nightshift}`, ...) are filled across the whole text. Every
CLI (claude, agy, codex, copilot) gets this text, because `--start` builds all of them from `role_brief`.

A manifest role may name its file with `persona:` (relative to `personas/`, as `brief:` is to
`briefs/`); without the key a role gets `personas/<role lower>.yaml` when that file exists, and a
custom role with neither gets no persona block. `load_crew` refuses a named persona that is missing
or not a YAML mapping. The persona is carried on each member in `crew.json`; a shift whose
`crew.json` predates it falls back to the default lookup. `spawn` of a label that is not a role
(`Coder2`, `Helper`, `Advisor2`) gets `<label>.yaml` if it exists, else `coder.yaml`, matching its
`coder.md` brief fallback. `role_brief` substitutes `{identity}` with the member's full unique
session identity; mailbox `resolve_role` then maps that identity back to the canonical crew label.
Thus these Coder, Tester, Advisor and Apprentice variants address a custom spawned member without changing peer
references such as `Foreman`.

## Context exhaustion and compaction safety (NGH-030)

NGH-069 adds proactive, supervisor-driven compaction without changing briefs or mailbox schemas.
Crew YAML accepts `compact_at` (integer percent 0..100) at crew or role level; a role overrides
the crew value. Omitted values default to 50 for Foreman and 70 for other roles; 0 disables triggers.
Explicit values survive launch, spawn and respawn. The brief's ban on members initiating compaction
still applies; the supervisor owns this new path.

`patrolCompaction` is shared by the patrol (after budget enforcement) and warden (only without a live
patrol, never in dry-run). A dedicated named mutex keyed by `compaction.lock` serializes both entry
points without a stale file-lock takeover during slow console sends. It requires a live current
incarnation, an empty idle composer, no live `wait` tool call, no unfinished incoming assignment or
uplift for that incarnation, and no waiting/running `builds/*.json` anywhere in the shift (including
gate builds). Busy, permission, draft, unreadable and unknown screens are skipped.

Context is independent of cost sampling: Codex's last three nonempty footer rows supply remaining
percentage even when dollars or credits win `parseFooter`. Claude and Copilot's status-line JSON
provides `context_window.used_percentage`; callbacks must match the discovered `session_id`.
Child panes receive `NIGHTSHIFT_STATE_DIR` so the existing Copilot status-line command can save it.
`<Role>-context.json` stores `pid`, `started`, `context_used`, `context_at` (epoch seconds), accepted
only for that incarnation and at most 90 seconds old. Missing/future/invalid samples never trigger.

`<Role>-compaction.json` persists the incarnation, `phase`, `before`, `requested_at`, `after`,
`last_compacted` and `anchor_at`. Intent (`pending`) is saved before console input; a failed or
uncertain send is never retried automatically. Completion requires a later fresh sample with a
strictly lower used percentage and an idle safe pane; a receipt alone is insufficient. It saves
`confirmed` then appends one `compacted` bus event with `role`, `before`, `after`. Repeated patrols
do not repeat the event. A completed cycle must observe usage below threshold before rearming.

After confirmation, Codex/Copilot receive one brief-path reanchor note. `anchor_pending` is saved
before sending, then `waiting` only for a confirmed send. Both phases require a new live
`waiting/<Role>.json` whose `since` is at least `anchor_at`; an old waiter is insufficient.
After 60 seconds without it, phase becomes `stuck` and raises `member-stuck` once. Neither uncertain
compaction nor uncertain reanchor is automatically injected again. Claude keeps its system brief.
`status` adds fresh `context_used` and incarnation-fenced `last_compacted`, also in readable output.

Verified commands: Claude/Codex/Copilot `/compact`, Gemini `/compress`. agy and Vibe are skipped:
no equivalent was verified. Gemini's command is known but no reliable context signal is currently
wired, so it also skips automatic triggering. Copilot uses the installed CLI's status-line payload
(`@github/copilot/index.js`: `session_id`, `context_window.used_percentage`), not AIC as context.
References: [Codex commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
[Copilot context](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/context-management),
[Gemini commands](https://geminicli.com/docs/reference/commands/),
[Claude status line](https://code.claude.com/docs/en/statusline).
No Claude launch threshold override is set: the documented auto-compact token-window setting is
not a percent threshold, and installed-version support was not established. Warden is primary.

Every non-Foreman member brief forbids invoking `/compact` and relying on automatic compaction.
At the actual child-process launch boundary, `crew_env` uses the member's base role, not its pane
label. Claude members receive `DISABLE_AUTO_COMPACT=1`; Claude Foreman receives
`DISABLE_AUTO_COMPACT=0` and `DISABLE_COMPACT=0`, allowing compaction even if the parent inherited
a ban. These overrides affect only a copied child environment; no global settings are written.
`start --dry-run` exposes each role's `compaction` status and exact environment overrides using
the same `compaction_control` helper as the real launch (including the live communication route).

Verified tool support (16 Oct 2026):

- **Claude:** [`DISABLE_AUTO_COMPACT`](https://code.claude.com/docs/en/env-vars) disables automatic
  compaction, including idle compaction; manual `/compact` remains available but member policy bans it.
- **Codex:** [configuration reference](https://developers.openai.com/codex/config-reference) documents
  `model_auto_compact_token_limit` as a threshold; installed `codex.exe --help` confirms `-c` overrides
  are invocation-scoped. No disable equivalent was verified, so no threshold is represented as a ban.
- **Copilot:** [context management](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/context-management)
  documents automatic compaction. Installed `@github/copilot/index.js` reads
  `COPILOT_BACKGROUND_COMPACTION_THRESHOLD` and `COPILOT_BUFFER_EXHAUSTION_THRESHOLD`, but its
  `CompactionProcessor.onRequestError` forces compaction after a provider context-limit error,
  bypassing the background threshold. The SDK's `infiniteSessions` setting is not a verified CLI flag.
- **agy (Antigravity):** installed `agy.exe --help` and the official
  [CLI reference](https://antigravity.google/docs/cli/reference) and
  [settings](https://antigravity.google/docs/settings?tab=cli) expose no verified disable control.
  The binary contains `AGY_CLI_AUTO_COMPACT_OVERRIDE`, but its semantics are unverified; it is not set.

Non-Claude members remain launchable, with `automatic: unsupported`, a per-tool reason, and empty
environment overrides in the dry run. Their brief ban is policy only; it is not an enforced runtime
guarantee. Foreman is shown as `permitted`. Copilot's manifest `context:` tier and Codex's remaining
context status line are separate from compaction controls.

When a member exhausts context, it stops and Foreman respawns that role in the same slot. The
replacement is marked in `crew.json`; its opening prompt names the absolute `handover.md` path and
the exact `inbox SHIFT LABEL` command, requests recovery of acked but unfinished assignments, and
requires Foreman to re-dispatch stale assignments to the new incarnation before it acts. Existing
same-slot selection and role/tool/model/brief attributes are preserved. Foreman alone may compact;
its brief variants require this order around any compaction event: before compaction, write
`handover.md` with current assignments, acked and unfinished work, and waits, then write a
mailbox/bus note; after compaction, first reread `ticket.txt`, `prompt.txt`, `requires.txt` and the
result ladder, claims, inbox, and `handover.md`, then continue.

`spawn` sends the wrapper command with `console.Inject(cmdPid, line, true)` (helper `_attach inject PID 1`, text on stdin), matching the
installed helper protocol; command text is not placed in argv. `inject_command` (Claude `/color`) does the
same (NGH-041; the Codex `/rename` injection did too until NGH-067 removed it); passing the text as argv made the
helper fail, which is how Claude panes lost their role colours.

Trust dialogs (NGH-041): `accept_trust_prompt(child, tool)` runs in every claude, agy, codex and copilot
pane. `TRUST_PROMPTS` holds each CLI's literal dialog text, the highlighted option that accepts it and,
for Claude, the banner that proves it is already past it: Claude `Yes, I trust this folder`; agy
`Do you trust the contents of this project?` / `> Yes, I trust this folder`; Codex `Trust this folder?
Codex can read...` / `› Trust and continue`; Copilot `Do you trust the files in this folder?` /
`❯ 1. Yes` (not option 2, which remembers the folder). The texts come from agents-cli's agy fixtures
and from the strings in the installed codex.exe and Copilot `index.js`. Answering is the same as for
Claude: poll `console.Peek` for about 20 s, act only while the dialog text is on screen (the
sentence is matched across wrapped lines and box borders), press Down until the accepting option is
highlighted, then Enter. The CLI runs in the shift's own folder, so the dialog is about that folder.
When Codex says trusting would apply to the repository root instead, the dialog is left for the human. Failed injection reports stdout and
stderr (including structured helper errors), restores `crew.json`, and leaves the slot available.

## The shift folder is the source of truth

```
C:\nightshift\shifts\<shift>\
  prompt.txt       the task as given to Foreman        ticket.txt   ticket id (when started from one)
  launch.json      wt argv, launcher pid, window hwnd   Foreman.json / Coder.json / Tester.json   pid, name, start time, cwd, history
  crew.json        members (incl. spawned), slots, advisor_slot, [coder_slot,] cwd  slots/<N>.json  what each container slot holds (NGH-025)
  result.json      the shift contract (above)           handover.md  written by down
  claims.json      file claims per role                 denials.json auto-mode classifier refusals seen
  mailbox.sqlite3  messages: stored → sent → acknowledged, with sender/recipient incarnation (NGH-019)
  bus.jsonl        the shift bus: every message, sent, ack and start/spawn/result/down event, in order (NGH-026)
  bus.lock         held for a moment by each bus append (O_EXCL, stale after 10s)
```
Anything that needs to survive a dead pane, a restart or a different CLI lives here, not in a
process. `status` and `resume` are computed from it.

The machine floor state is shared by shifts:

```
C:\nightshift\floor\
  bus.jsonl       directed and broadcast supervisor messages (NGH-053)
  bus.lock        held briefly by each append
  cursors\        atomically replaced JSON cursor per reader, with a sibling reader lock
```

## The loop (optional batch runner)

Since NGH-025 a shift ends itself (Foreman runs `down`); the loop is an optional way to run
several tickets unattended. `nightshift loop T1 T2 …` (or `--project NGH [--max N]`) is deterministic
Python, no model: start a shift → poll for `result.json`, crew death, a 20-minute idle stall, and
classifier denials → `down --close` → note the ticket → next. A died/stalled shift is rerun with a fresh crew
that gets the previous handover (`--retries`, default 2); `blocked` stops the loop for a human.
By default the loop and `resume` send no completion or halt email. `--notify` explicitly opts
into owner notification through Mailkeep; the compatible `--no-notify` flag remains accepted.
Notification is configured via the `NIGHTSHIFT_NOTIFY_TO` (recipient email) and `NIGHTSHIFT_NOTIFY_IDENTITY`
(Mailkeep identity) environment variables, looking up `mailkeep` on PATH. When not configured or when
`mailkeep` is absent from PATH, notification is skipped with a stderr note and never fails the loop.
The loop closes its own window on success. `nightshift resume` reruns tickets whose last shift
never reached done (after a reboot). The loop is the "guardian"; a model-driven "master of masters"
is a persona decision not yet taken (NGH-014).

## Messaging: two transports, one record

- Claude ↔ Claude: native `SendMessage`, instant, Claude-only, no persistence.
- Anything else (Codex, Gemini via `agy`, Copilot): `agents-cli send/wait/tail` into the pane, with a
  `CODER-DONE` / `CODER-BLOCKED` convention (NGH-012, NGH-022).
- The **mailbox** (NGH-019) sits above both: every message is stored first, carries a
  `msg:<id>` header, is acked by id by the right recipient incarnation, and `inbox` re-delivers
  what was stored but never sent. Transports are notifications; the mailbox is what is true.
- `msg` also accepts the guarded `uplift` kind for NGH-038. Its Foreman-to-Coder transfer carries
  `source_dispatch_id`, `reason` and serialized `evidence`; those fields are mirrored on the bus.
  `APPRENTICE-NEEDS-SENIOR` notes, distinct Tester verification rows linked by `coding_asg`, and
  `delivered_epoch` provide the durable trigger evidence described above. Foreman's `inbox` reports
  `uplift_due` and mailbox-derived `uplift_counts`; Foreman copies the latter into `result.json`.

## The shift bus (NGH-026)

`<state_dir>/bus.jsonl` is the whole shift in one file, one JSON object per line, appended in order.
There is no bus process: each command appends its own line and `nightshift log` reads the file.

| Event | Written by | Fields (besides `seq`, `ts`, `event`) |
|---|---|---|
| `start` | `start`, once the shift folder exists (never on `--dry-run`) | `shift`, `ticket`, `cwd`, `crew`, `members` (labels) |
| `msg` | `insert_message`, the one place every mailbox row is stored: `msg send`, `ready`, `assign`, `note`, `report` and the protocol-violation note `ack` files | `id`, `kind`, `sender`, `recipient`, `asg`, `body`; uplift rows also carry `source_dispatch_id`, `reason`, `evidence` |
| `sent` | `msg sent`; `ready`/`assign`/`note`/`report` after delivery (NGH-060) | `id`; NGH-060 rows add `via` (`wait` = recipient was blocked in `wait`, `nudge` = one-line nudge typed) |
| `ack` | `ack`; `wait` (NGH-060) | `id`, `by`, `outcome` (`acknowledged`, `already acknowledged`, `protocol violation` + `reason`); `wait` adds `via: "wait"` |
| `nudge-failed` | `ready`/`assign`/`note`/`report` when the nudge could not be delivered (NGH-060) | `id`, `to`, `reason`; the row stays `stored` for the warden |
| `nudge-retry` | `warden`, re-nudging a row unacked for 10 minutes (NGH-065) | `id`, `to`, `attempt` (1-3) |
| `member-stalled` | the patrol (or the floor `warden` for a shift without one), on the third retry (NGH-065) | `role`, `id`, `attempts` |
| `budget-warning` | the patrol, when a unit's shift total reaches 80% of its budget, once per unit (NGH-066) | `unit`, `total`, `budget` |
| `budget-stop` | the patrol, when a unit's total reaches its budget, once per unit (NGH-066) | `unit`, `total`, `budget` |
| `paused` | the patrol's budget stop, after the handover is written (NGH-066) | `reason` (`budget-stop`), `unit`, `total`, `budget` |
| `member-died` | the shift's `patrol` (NGH-066; the lead's `wait` poll in NGH-061) | `role`, `pid`, `incarnation`, `asg` (the member's active assignment or null) |
| `asg-timeout` | the shift's `patrol` (NGH-066; the lead's `wait` poll in NGH-061) | `asg`, `recipient`, `delivered_epoch` |
| `loop-detected` | `build --asg` when it refuses a fourth failing build (NGH-061) | `asg`, `builds` (the three n), `signature` |
| `commit` | `commit` (NGH-061) | `asg`, `sha`, `build` (n of the build Tester passed) |
| `respawn` | `respawn`, after the slot was typed into (NGH-061) | `role`, `slot`, `asg` (null when none), `msg` (the original active row id, null when none) |
| `spawn` | `spawn`, only after the wrapper line was typed into the slot | `role`, `slot`, `tool`, `identity` |
| `result` | `down`, when `result.json` exists | `result` (its parsed contents; `null` + `error` when unreadable) |
| `down` | `down`, after the lock is released and before its own process tree is killed | `stopped` (roles), `lock_released` (folders) |

- `seq` is the event's 1-based line number, `ts` local ISO time to the second. `bus_append` holds
  `bus.lock` (O_EXCL, waits up to 5s, a lock older than 10s is stale) while it counts lines and writes the
  new one in a single `os.write` in append mode, so concurrent members get unique, increasing `seq`.
- A bus failure never breaks the command that appended: it prints `warning: shift bus not written` on
  stderr and the mailbox row, spawn or down goes ahead.
- `nightshift log SHIFT [--follow] [--since N] [--json]` prints it, one line per event
  (`#12 08:53:49 msg Foreman->Coder assignment asg:a1 msg:3  <body>`), `--json` the raw lines,
  `--since N` only `seq > N`. `--follow` polls the file every second by byte offset, holds back a
  trailing line until its newline arrives, and exits once it has read a `down` event (or on Ctrl+C).
  Without `bus.jsonl` it says `no bus yet` on stderr and exits 0, or keeps polling under `--follow`.
- Every member may read `bus.jsonl` at any time; Tester reads it before acting on an assignment, the
  Advisor reads it for context, and Foreman's `result.json` notes that the bus is complete.

### Pull-based delivery (NGH-060)

Five commands (`cmd/nightshift/write_bus.go`) let a member exchange messages without typing a body
into another pane (docs/instructions-review.md §6). They use the same mailbox rows, `dispatchMetadata`
checks and bus events as `msg send`; `msg send|sent`, `ack` and `inbox` are unchanged and the briefs
do not use these commands yet. ROLE goes through the crew label lookup; the lead is the crew's human
contact (Foreman).

| Command | Stores | Notes |
|---|---|---|
| `ready SHIFT ROLE` | `note` `READY`, ROLE -> lead | A second READY from the same incarnation exits 4; the Advisor never sends READY (exit 4). |
| `assign SHIFT ROLE TEXT [--asg ID] [--coding-asg CID] [--ready-timeout S]` | `assignment`, lead -> ROLE | Without `--asg` an id `<role>-<n>` is generated. Waits (default 300s) until ROLE's current incarnation has sent READY or acknowledged any row (consult/summon starts never send READY); not ready: exit 3, nothing stored. A reserved member still `not_started`: exit 4 `<ROLE> is asleep; use nightshift consult\|summon`. |
| `note SHIFT ROLE TEXT [--asg ID] [--ready-timeout S]` | `note`, lead -> ROLE | Same readiness wait; no generated asg. |
| `report SHIFT ROLE TEXT [--asg ID]` | `result`, ROLE -> lead | `--asg` defaults to the latest assignment/uplift to ROLE's current incarnation. First line is the verdict; a first line starting `APPRENTICE-NEEDS-SENIOR` is stored as `note`. Text `READY` exits 4 (use `ready`). |
| `wait SHIFT ROLE [--timeout S] [--json]` | nothing; acknowledges | Blocks (polling about once a second; default 540s, 0 = forever) until a `stored`/`sent` row for ROLE's current incarnation exists, acknowledges the lowest id (bus `ack` with `via: "wait"`) and prints `msg:N kind:K asg:A from:SENDER` then the stored body verbatim (`--json`: the row). Timeout: exit 3 and the stdout line `timeout: nothing for ROLE in Ns; run nightshift wait SHIFT ROLE again`. While blocked it owns `<state_dir>/waiting/<ROLE>.json` `{pid, since}`: one live wait owns it, a second wait for the same ROLE leaves it alone and re-claims it on its next poll once the owner's pid is gone, and a wait removes it on exit or Ctrl+C (exit 130) only while it still records its own pid. Negative `--timeout`/`--ready-timeout`: exit 2. |

The four storing commands print the header line `msg:N kind:K asg:A`, then deliver: if the recipient
has a waiter file whose pid is running, the row is marked sent (`sent` with `via: "wait"`). Otherwise,
unless `NIGHTSHIFT_NUDGE=off`, one line `msg:N kind:K asg:A  run: nightshift wait SHIFT ROLE` is typed
into the recipient's live pane (pid and tool from `<ROLE>.json`) with the warden's `console.Send`
path, and the row is marked sent with `via: "nudge"`. A nudge that cannot be delivered leaves the row
`stored` for the warden, appends `nudge-failed` and warns on stderr; the command still exits 0.

Exit codes: 0 ok; 2 usage error (argparse-style, including empty text); 3 timed out (`wait`, or
`assign`/`note` waiting for READY); 4 refused (duplicate READY, unreleased claims, any other
`dispatchMetadata` refusal, member asleep, the lead addressing itself, Advisor READY, `report READY`);
5 reserved; 1 an unexpected failure (unknown shift or role, mailbox error). Every non-zero exit except
the `wait` timeout prints one stderr line saying what to do next.

### The in-shift patrol and the budget (NGH-066)

`nightshift patrol SHIFT [--once]` (`patrol.go`) is the shift's own warden (docs/instructions-review.md §11). `start`
launches it after the crew as a hidden, detached child (`CREATE_NO_WINDOW | DETACHED_PROCESS`, no inherited handles;
skipped when `NIGHTSHIFT_PATROL=off`, which test harnesses running a real `start` set) and records
`<shift>/patrol.json` {pid, started}; a failed launch is a one-line warning and the floor warden keeps the shift.
`down` kills it after the panes; it also exits by itself on the next tick once the bus has `down` or `paused` or
the shift folder (crew.json) is gone. It ticks every 30 seconds (`NIGHTSHIFT_PATROL_INTERVAL` seconds overrides;
`--once` runs one tick and prints its report as JSON). Each tick:

1. Liveness and stalls: `member-died` and `asg-timeout` (the NGH-061 rules, moved out of the lead's `wait`, which
   now only returns rows), the first-delivery pass for stored rows, the idle-lead nudge, and the NGH-065 re-nudges
   with `member-stalled`.
2. Costs: for each member whose pid runs in its current incarnation, Coder2's `sampleMember` (file source first,
   else the pane footer read through `paneScreen`) and `recordUsage` into `<Role>-usage.json`.
3. Budget: crew.json `budget_usd` (Claude, Bedrock and API panes), `max_credits` (Copilot panes, the existing shift
   budget) and `budget_tokens` (subscription panes with no price), each compared with `shiftUsageTotals` in its own
   unit (`usd`, `credits`, `tokens`). A crew manifest sets `budget_usd` and `budget_tokens` as top-level
   non-negative numbers; the loader validates them and `start` copies them into crew.json. Reaching 80% of a budget
   raises `budget-warning <unit> <total>/<budget>` once; reaching 100% raises `budget-stop <unit> <total>/<budget>`
   once and then, with no model judgement: `console.Key(pid, tool, "escape")` for every live member, handover.md written
   from live state (as `nightshift handover` writes it), bus `paused` {reason, unit, total, budget}, and result.json
   `{"status": "paused", "stage": <the existing result's stage, else implemented>, "summary": "budget stop: <unit>
   <total>/<budget>"}`; the patrol then exits. The shift stays resumable with `nightshift start --continue-from
   <shift_dir>`, which carries the handover into the new task as before.

The floor-level `warden` keeps lock clean-up, claim normalisation and pane recovery for every shift, but skips
delivery, the idle-lead nudge and re-nudges for a shift whose patrol (patrol.json) is running; a shift without one
(started before NGH-066, or whose patrol failed to start) keeps the old warden path. `cleaner` is unchanged.

#### Cost sampling (`cmd/nightshift/usage_sample.go`)

The patrol takes one figure per live member per tick with `sampleMember(dir, member)` and appends it with
`recordUsage`; `shiftUsageTotals(dir)` gives the budget totals.

- Sources, file first. Claude and Codex: the token total of the transcript recorded in `<Role>.json` (`transcript`,
  NGH-048), unit `tokens`, source `transcript`. Otherwise the pane's screen (`paneScreen(pid)`, a package var over
  `console.Tail(pid, tool, 40)`, stubbed in tests) through `parseFooter(tool, screen)`, source `screen`.
  Copilot and agy write no usage file Nightshift can read (the bundled Copilot rejects `--usage-output-file`), so they
  are always read from the screen. A Claude pane's dollars come from its status line, which records them itself.
- `parseFooter` (pure; the last match on screen wins; an empty tool is claude):

| Tool | Footer | Unit |
|---|---|---|
| copilot | `Session: N AIC used` (any `N AIC`) | `credits` |
| claude | `$1.23` (the status line below) | `usd` |
| codex | `~$1.82` (status-line item `estimated-thread-cost`) | `usd` |
| codex | `5.2 credits` (status-line item `thread-credits`) | `credits` |
| codex | `Context 83% left` (status-line item `context-remaining`) or the default footer's `83% context left` | `context_left` |
| agy | `83% context left` | `context_left` |

Patterns are tried in that order per tool, so a Codex pane showing all three yields its cost, else its credits, else
context left (recorded, never budgeted); the last match on screen wins. Codex cost and credits are matched only in the
last three non-empty lines (the status line), and cost needs the `~$` prefix, so `$1` or `5 credits` in the
conversation never counts. Both items are Enterprise-only in Codex and omitted when unavailable; the parser then falls
back to context left. `sampleMember` reads a Codex pane's screen first: a cost or credits figure there wins over the
transcript's token total; otherwise tokens from the transcript, otherwise the screen's context left. Evidence for the
shapes (codex-cli 0.163.0 binary, read as bytes, no pane launched): the item descriptions "Estimated current-thread
credits (Enterprise workspaces only; omitted when unavailable)" and "Estimated current-thread cost in USD (Enterprise
workspaces only; ...)", the status-line preview fixture `Context 0% left ... 5.2 credits~$1.82`, and the
`tui\src\chatwidget\status_surfaces.rs` fragments `Context `, `% left`, ` credits`.

- `<state_dir>/<Role>-usage.json`: `{"role", "samples": [{"at", "unit", "value", "source"}], "latest": {the last
  sample}}`, at most 200 samples, written under `<Role>-usage.json.lock`. A file in another shape starts again.
- `shiftUsageTotals`: for every `*-usage.json`, the latest sample of each unit, summed per unit
  (`map[unit]value`). An old-format file (top-level `credits_used`, no samples) contributes nothing to the totals.
- `result.json` `usage` (`write_result.go`): per member `credits_used` = the latest `credits` sample (an old file's
  top-level `credits_used` still counts), `cap` = the member's `max_credits`, plus `usd` when the member has a `usd`
  sample; `total_used` sums credits; `total_usd` is added when any member has dollars. `budget` stays crew.json
  `max_credits`.

#### Claude status line (`launcher_pane.go`, `floor_bus.go`)

Every Claude pane started by a shift gets `--settings <state_dir>/<Role>.claude-settings.json` (one file per pane,
like `<Role>.brief.md`; `~/.claude/settings.json` is never touched):
`{"statusLine": {"type": "command", "command": "\"<nightshift.exe>\" statusline --state-dir \"<state_dir>\" --role <Role> --identity <identity> --colour <colour>", "padding": 0}}`.
The Foreman's file also carries its existing `permissions.deny` `Edit(/<repo>/**)` (formerly passed inline), as
Claude takes one `--settings`. Consult/summon/respawn panes (the rebuilt Claude command) get the same file without the
deny.

NGH-070: `nightshift statusline` with `--role` reads Claude's status-line JSON from stdin and prints
`shift-<id> | <model> | ctx <N>% left`, then ` | $12.30` only when `cost.total_cost_usd` is present
(including zero, two decimal places). There is no role token, cwd basename or token-total fallback.
`model.display_name` falls back to `model.id`; `context_window.remaining_percentage` falls back to
100 minus `used_percentage`. Missing/invalid fields omit their segment. These fields are documented in
[Claude's status-line contract](https://code.claude.com/docs/en/statusline); the local package is 2.1.295.
The existing per-pane `<Role>.claude-settings.json` remains the launch override; global settings are untouched.
Cost changes still record a `usd` sample (source `statusline`) in `<Role>-usage.json`, and NGH-069's
session-fenced context callback still runs. The patrol's existing dollar parser accepts the final pipe segment.
Without `--role`, the role-coloured `NIGHTSHIFT_ROLE`/`NIGHTSHIFT_IDENTITY` formatter (agy and Copilot)
is unchanged. Codex keeps its native footer. The command always exits 0.

New native shifts use `shift-<8 hex digits>` for state folders, worktree basenames and the identity prefix
(`<shift>-foreman`, etc.). `shortShiftName` normalizes the legacy prefix for display only: no existing folder,
worktree or live window is renamed. Directory discovery and exact/suffix/latest resolution continue to accept
`Nightshift-shift-*` for status, down and queue; `--continue-from` still reads either folder path.
The recorded hwnd remains the primary close target; title fallback recognizes both naming generations.
Screenshot acceptance needs an existing window running this version; this increment does not launch or modify
a live crew merely to obtain the requested 2x4 screenshot.

#### Foreman page (`briefs/foreman.md`)

Two event lines: `budget-warning UNIT`: tell the human; finish in-flight work. `budget-stop UNIT`: shift paused; tell
the human and stop. Foreman role layer (standard): default, claude-copilot, apprentice-codex, claude-work 246;
codex, gemini, copilot-premium 237; work 232; copilot compact 135.

### Failure events and lifecycle commands (NGH-061)

Failures the tool can count or time become events, so the lead learns of them by blocking in `wait` instead
of watching clocks or panes (docs/instructions-review.md §6). An event is a mailbox row of kind `event`, sender
`Nightshift`, recipient the lead, asg the assignment concerned (or null), body one line starting with the event
name; its `reason` column holds the dedup key and `evidence` the bus fields as JSON. Each also appends a bus line
named after the event. `wait SHIFT <lead>` prints an event like any message:
`msg:N kind:event asg:A from:Nightshift` then the body; `inbox` lists unacknowledged events. Each is raised at
most once per (event, subject).

| Event | Body | Raised when | Subject |
|---|---|---|---|
| `member-died` | `member-died <Role>`, plus ` active asg <asg>` when it had one | the shift's patrol (NGH-066) finds a member whose `<Role>.json` records a pid that is not running for its `started` incarnation. A member with no `<Role>.json` (not_started) or no `pid` yet (starting) never counts, and nothing is raised once the bus has a `down` event. | role, incarnation |
| `asg-timeout` | `asg-timeout <asg>` | the shift's patrol finds the latest assignment/uplift for an asg, from the lead to an Apprentice or Coder, delivered 900s or more ago with no later `result` from that recipient incarnation for that asg | asg, recipient incarnation |
| `loop-detected` | `loop-detected <asg>` | `build --asg ASG` finds that the last three finished builds recorded for that asg all failed with the same error signature: it refuses with exit 4 and the stderr line `loop detected: escalate or report BLOCKED`, runs nothing and reserves no build number. A build for the asg that passes (or is cancelled or times out) breaks the streak. Builds without `--asg` never trip it. | asg, the three build numbers |

`build` (NGH-061) takes an optional `--asg ASG` and records three more fields in `builds/<n>.json`; its printed
record, usage line and exit codes are unchanged:

- `asg`: the `--asg` value, or null.
- `tree`: the git tree id of the working tree including untracked, non-ignored files, taken when the build starts
  (with `head` and `dirty`). It is written with a temporary `GIT_INDEX_FILE` (`read-tree HEAD`, or `--empty` on an
  unborn branch, then `add -A` and `write-tree`), so the real index is untouched; null outside git.
- `signature`: for a failed build, the sha256 of the exit code and the log lines that name failures (pytest
  `FAILED `/`ERROR ` lines, go `--- FAIL:` lines), falling back to the last five non-empty lines, with every digit
  run replaced by `N` so timestamps and durations do not matter (`buildSignature(exit, logText)`); null otherwise.

`nightshift commit SHIFT ASG -m "one line"` (`write_commit.go`) commits exactly what Tester passed. It takes the
latest `result` from a Tester whose body starts `TESTED-PASS` and whose asg is ASG, or is the asg of a verification
assignment to that Tester with `coding_asg` ASG; reads the first `builds/<n>.json` its body cites; and refuses with
exit 4 and one stderr line when there is no such pass, the pass cites no build, the build record is missing, it
did not exit 0, it recorded no tree, or the current tree of the build's `cwd` differs from the recorded `tree`.
Otherwise it runs `git add -A` and `git commit -m MESSAGE` in that folder (never amends), prints
`committed <sha> for <ASG> (builds/<n>.json)` and appends bus `commit`. A missing, empty or multi-line `-m` is
exit 2 with one stderr line; a failing git step is exit 1 with git's first error line.

- `nightshift respawn SHIFT ROLE` (`launcher_respawn.go`): restarts a member whose recorded pid is dead, in the
  slot whose `slots/<N>.json` names it, through `spawn --slot N`. Its active assignment is the latest
  `assignment`/`uplift` row to ROLE with no later `result` from ROLE for that asg. When there is one, respawn
  writes the pending crew.json key `respawn` {role, kind, asg, body, sender, msg, plus coding_asg /
  source_dispatch_id / reason / evidence when the row had them}; the pane's start hook (`launcher_pane.go`,
  pendingKey `respawn`, the same path as consult/summon) records the row for its own NEW incarnation with the same
  kind, asg, body and metadata (not re-validated: it was validated when first sent), marks it sent, clears the key
  and uses `msg:N kind:K asg:A` + blank line + body as the first prompt, so `wait`/`ack` work. No active
  assignment: the normal startup prompt. A failed spawn puts crew.json back. Prints
  `respawned <ROLE> in slot <N> (asg <asg>)` or `respawned <ROLE> in slot <N> (no active assignment)`.
  Exit 4: `<ROLE> is running; nothing to do`, `<ROLE> was never started; use nightshift spawn, consult or summon`,
  `no slot is recorded for <ROLE>; start it with nightshift spawn`. Spawn failures keep spawn's own message and code.
- `nightshift uplift SHIFT ASG TEXT --reason needs-senior|tester-failed-twice|timeout` (`launcher_respawn.go`):
  Foreman -> Coder uplift with msg send's validation (`dispatchMetadata`: a current Apprentice coding assignment,
  the trigger evidence, the Apprentice's claims released). A `not_started` Coder is started with
  `summon --kind uplift`; otherwise the row is stored and delivered as `assign` does (waiter or one-line nudge).
  Exit 4 with the reason, nothing stored, e.g. `uplift trigger not satisfied: timeout; nothing stored`,
  `<label> must release claims before transfer: <paths>`, `<asg> is already uplifted to Coder (msg:N); nothing stored`.
  Exit 2 = usage (missing --reason, empty text).
- `nightshift handover SHIFT` (`lifecycle_report.go`): writes `<state_dir>/handover.md` (CRLF, like down) from
  live state without capturing or stopping panes: `# Handover`, Task, Written, then `## Active assignments`
  (`- <asg> -> <recipient> (msg:N <kind>, age <dur>)`, every dispatch with no result from that incarnation),
  `## Live waiters` (`waiting/<label>.json` whose pid runs), `## Last report per member` (latest `result` row
  per sender, first line), then the same report `down` writes (result, denials, claims, unacknowledged
  messages, builds, token usage, commits). Prints the path. `down` keeps writing its own handover unchanged.
- `nightshift result SHIFT done|blocked --stage RUNG --summary TEXT [--changed PATH]... [--evidence 'stage|what|outcome']...`
  (`write_result.go`): `--changed` and `--evidence` are repeatable. Writes result.json {status, stage, summary,
  changed, evidence [{stage, what, outcome}], bus (path of bus.jsonl), builds [{n, head, dirty, exit, asg}] for
  every builds/<n>.json, uplift_counts (the object `inbox` prints for the lead), usage when any
  `<label>-usage.json` exists: {budget: crew.json max_credits, total_used: sum of the files' credits_used,
  panes: {label: {credits_used, cap: the member's max_credits}}}}. Prints the path. Exit 4:
  `done at <stage> needs a passed nightshift build on a clean tree on the current HEAD; run nightshift build first, or report --stage implemented or blocked`
  (done above implemented with no build that passed, exit 0, dirty false, whose head equals the current HEAD of
  worktree.json's worktree_dir, else of the build's own cwd). Exit 2: usage, including an evidence
  value without two `|`. `down` still appends the `result` bus event.

Failure lines (stderr, one extra line after the existing output; stdout and exit codes unchanged):

- `land` exit 4 (conflict): `rebase conflict: assign the conflict to Coder in the worktree, have Tester pass it, then land again`
- `land` exit 2 (rebased_needs_test only, not usage errors): `rebased cleanly: have Tester pass the new HEAD, then land again`
- `claim` exit 4: after the existing `<path> is already claimed by <holder> ...` lines,
  `ask Foreman to release or reassign <path>[, <path>...]` (the conflicting paths in argument order).
- `spawn` with no free slot (no --slot given): after `no free slot in shift <shift> (<n> slots, all taken)`,
  `reserved slots: <N> (Advisor, until consult), <N> (Coder, until summon); slot <last> is the Ops pane (only with --slot <last>); down a member or ask the human for a slot`
  (`reserved slots: none; ...` when the crew reserves none; the role's own reserved slot is not listed).

## Concurrency

Legacy/direct-checkout and non-git paths use a folder lock between shifts (NGH-010); file claims
between coders inside a shift (NGH-011); and the build slot (NGH-020) serialises expensive builds
per folder and records revision-bound evidence. Git worktree mode does not hold a whole-repository
folder lock while the crew codes and tests; it uses the per-shift worktree and takes a short-lived
repository landing lock only during `nightshift land`. Worktree claims are shift-local. In direct/in-place
mode, claims are checked against every live shift: a claim held by another shift conflicts, and `claim`
reports its owner and time, exits 4, and changes none of the requested paths. Dead shifts are ignored.

### Per-shift git worktrees and Foreman landing (owner decision, 16 Oct 2026; NGH-040)

Running concurrent shifts directly in a single checkout causes cross-shift file-claim stalls on shared
files (`nightshift_shifts.py`, `test_nightshift_shifts.py`, `docs/ARCHITECTURE.md`). Rather than adding a
dedicated merge-queue pane ("Refinery"), Nightshift uses **per-shift git worktree isolation** paired
with **Foreman-driven landing (`nightshift land`)**:

1. **Worktree per shift (`C:\nightshift\worktrees\<shift>\`).** When `nightshift start` targets a git
   repository (unless `--no-worktree` is passed), it resolves `repo_root` and the target branch (default
   `main`), creates branch `shift/<ticket>-<shortid>` from that target branch's `HEAD`, checks out a
   linked worktree at `C:\nightshift\worktrees\<shift>\`, and writes `<state_dir>/worktree.json`
   (`{repo_root, worktree_dir, branch, target_branch, base_sha, created_at}`). Every crew pane and
   container slot runs with `cwd = C:\nightshift\worktrees\<shift>\` (including Claude Foreman's
   path-scoped `Edit(//c/nightshift/worktrees/<shift>/**)` deny rule). No whole-shift folder lock is held
   on `repo_root` while the crew codes and tests; `claims.json` coordinates roles *within* the shift
   (`Coder`, `Apprentice`, `Tester`) without blocking concurrent shifts in their own worktrees.
   `--no-worktree` explicitly keeps the direct-checkout path and its folder lock. A non-git working
   folder cannot create a branch or land; it stays on the direct path and keeps the folder lock.
2. **Foreman-driven landing (`nightshift land <shift>`).** `Tester` first passes the reviewed revision;
   `Foreman` commits exactly that revision, then `Tester` reports `TESTED-PASS` on its clean `HEAD`, and
   `Foreman` runs `nightshift land <shift>` before writing `result.json` and calling `nightshift down`.
   While work is in progress, Tester runs targeted tests for the changed area with `nightshift build --cmd`
   (reporting which tests were run); the full configured build (`.\verify.cmd`: Go build, go test, Go golden
   comparisons under xdist; Python suite no longer in it, Python launcher frozen) runs once on the final clean
   commit before land.
   `land` refuses a dirty worktree, an untested `HEAD` or a `HEAD` changed since the Tester evidence;
   it never creates the commit. `nightshift land` acquires the short-lived repository landing lock
   (`C:\nightshift\locks\<repo>.json`) only for the landing operation and handles three cases:
   - **Case 1 — Fast-forward (`main` unchanged since worktree base):** Advances `main` cleanly to the
     worktree `HEAD` without clobbering unrelated uncommitted notes in `repo_root` (refusing if dirty
     tracked files in `repo_root` overlap the landed diff), writes `<state_dir>/landed.json`
     (`{repo_root, target_branch, base_sha, landed_sha, landed_at}`), appends a `land` event to
     `bus.jsonl`, releases the repo lock, and returns `{"status": "landed", "sha": "<landed_sha>"}`
     (exit `0`).
   - **Case 2 — Clean rebase (`main` moved ahead, `git rebase main` succeeds cleanly):** Before rebasing,
     `land` computes `mergeBase = git merge-base targetHead head`, `shiftFiles = git diff --name-only --no-renames mergeBase head`,
     `mainFiles = git diff --name-only --no-renames mergeBase targetHead`, and `overlap` as their sorted exact-path intersection.
     On a clean rebase, `land` appends a `land_rebase` bus event with keys: `decision` (`"build_carried"` or `"build_required"`),
     `old_head`, `new_head`, `target_head`, `merge_base`, `shift_files`, `main_files`, and `overlap` (empty list `[]` when disjoint).
     - **Disjoint clean rebase (`overlap` empty):** The passed build of the pre-rebase `HEAD` carries over (`decision: "build_carried"`).
       Both pre-rebase checks (target checkout and local-changes overlap) run before the rebase in the disjoint case (so a refused land never leaves the shift branch rebased with its build evidence orphaned).
       `land` updates `worktree.json` `base_sha`, sets `head` to the rebased `HEAD`, and continues in the same invocation into the
       fast-forward landing path (Case 1), writing `landed.json`, appending the `land` event, releasing the repo lock, and returning
       `{"status": "landed", "sha": "<landed_sha>"}` (exit `0`) in a single call.
     - **Overlapping clean rebase (`overlap` non-empty):** Requires a fresh test pass (`decision: "build_required"`). `land` updates
       `worktree.json` `base_sha`, releases the repo lock, and returns `{"status": "rebased_needs_test", "new_base": "<sha>", "head": "<rebased_sha>"}`
       (exit `2`). `Foreman` must not write `result.json` or tear down yet: it dispatches one verification pass to `Tester` (`nightshift build`)
       on the rebased tree, then re-runs `nightshift land <shift>` only after a clean TESTED-PASS (which now fast-forwards under Case 1).
   - **Case 3 — Rebase conflict (`git rebase main` reports conflicts):** Aborts the rebase by default
     (`--keep-conflicts` leaves it in progress) and releases
     the repo landing lock immediately so other shifts are never held up, returning
     `{"status": "conflict", "target_head": "<sha>", "files": [...]}` (exit `4`). `Foreman` assigns its
     own `Coder` to rebase onto `main` and resolve in the shift worktree. `Tester` passes the resolved
     revision, `Foreman` commits it, and `Tester` verifies the clean updated `HEAD` before another land.
3. **Teardown and reaping (`down` and `cleaner`).** When `<state_dir>/landed.json` exists, `nightshift down`
   and `nightshift cleaner` remove `C:\nightshift\worktrees\<shift>\`, prune worktrees, and delete the
   merged `shift/<ticket>-<shortid>` branch. When a shift ends or is reaped without landing (`blocked`,
   halted, or crashed), recovery teardown saves dirty edits in a recovery commit and preserves
   `shift/<ticket>-<shortid>` and its `worktree.json`/handover metadata in `handover.md` and `floor.json`
   so unmerged work is never lost. `--hold` leaves an unlanded worktree intact instead. A failed save
   leaves the worktree intact with recovery pointers recorded before the attempt.
   Shutdown completes independently of reclamation: a failed save or removal still permits the `down`
   event, runtime lock release and crew termination. Before removal, `recovery.json`, `handover.md` and
   `floor.json` record `cleanup_pending`, `disposition` (`landed`, `preserved` or `held`) and `error`.
   `cleaner` retries pending cleanup even for previously reaped shifts, after directory handles close.
   It checks Git's recorded worktree registrations and removes only empty unregistered remnants;
   unexpected files are preserved. Pending clears after removal, pruning and applicable merged-branch
   deletion all complete. An unlanded `--hold` leaves the worktree with no cleanup pending until a
   subsequent explicit cleanup.

## Design vs built — reconciliation (keep current)

| Reviewed design said | Built | Status |
|---|---|---|
| No resident runtime; launch from the human's shell; Windows Terminal, two panes | same; the default crew is now five panes in a 2x4 container (NGH-024, NGH-027, NGH-029) | matches |
| Named Claude sessions on the native mesh; permission-mode parity | same (`--name`, `--permission-mode auto` for Claude roles; manifest `permissions:` override) | matches |
| Mailbox with stored→sent→acked, incarnation fencing, `inbox`/`ack`, protocol-violation notes | NGH-019 | matches |
| Folder lock, worktree lease at `assign` time | direct-checkout/non-git folder lock (NGH-010), intra-shift file claims (NGH-011), per-shift git worktrees (`C:\nightshift\worktrees\<shift>\`) and Foreman `nightshift land` (NGH-040) | NGH-040 built |
| Build slot, revision-bound evidence, cancellation (G4) | NGH-020 | in progress |
| Acceptance gates G0–G5 as the delivery sequence | replaced by ticketed increments run by the loop; G0–G3 and most of G5 are covered, G4 pending | departure in method, not in content |
| `down` with handover; `resume` re-dispatches acked-but-unfinished work | `down`/handover, loop retries with handover carry-over, `resume` after reboot | matches |
| Advisors as bounded consultants; later Tester/Reviewer/DevOps/Controller | Tester (NGH-024) and a dormant Fable Advisor that only Foreman may consult (NGH-027) built into the default crew; out-of-shift floor CLI roles `cleaner`, `warden`, `gaffer` built (NGH-035) | matches / extended |
| Second coder, heterogeneous CLIs later | Apprentice in slot 5 (NGH-029), Codex crew (NGH-033), Copilot crew (NGH-037/NGH-039) | built |
| "Portable package", CLI language open | Standalone Go binary (`build\nightshift.exe`) with full parity across all subcommands: read-only views, in-shift writes, lifecycle commands, and complete launcher capabilities (`start`, `spawn`, `warden`, `gaffer`, `loop`, `resume`, embedded assets); never delegates to Python | NGH-021 Increments 2–4 (NGH-052) built |
| — (not in the design) | loop, result ladder, crew manifest, opt-in Mailkeep notification, denial capture | additions |
| — (owner, 16 Oct) | container with slot grid (2x4/1x4), `slots/`, `spawn`, Foreman-owned `down --close/--hold` (close default), launcher exits; loop optional | NGH-025, built |
| — (owner, 16 Oct) | `C:\nightshift\` state root, `floor.json`, and `nightshift cleaner / warden / gaffer` CLI subcommands | NGH-035, built |
| — (owner, 16 Oct) | Accountant in slot 6: accounting log, source contracts, budgets, warning then shift teardown | NGH-034, proposed / unbuilt; owner review before build |
| — (owner, 16 Oct) | Per-shift git worktrees (`C:\nightshift\worktrees\<shift>\`) and Foreman-driven `nightshift land` (fast-forward / clean rebase + retest / conflict resolution by shift's own Coder + Tester) | NGH-040, built |

## Not decided / not built

- Accountant implementation and optional hold/rejig policy: **NGH-034**, proposed above; the mandatory
  Copilot warning and shift-budget teardown requirement is decided.
- Personas beyond Foreman + Advisor + Coder + Tester + Apprentice (DevOps, Reviewer, validator council):
  design in NGH-014, to be agreed with the owner before any is added. Roadmap on NGH-001.
- Which frontier model the Advisor should run long term across all manifests (Claude Fable in `default.yaml`,
  GPT-6 Astra in `codex.yaml`; Copilot's owner-set policy is Gemini 3.8 Flash for every role).
- Whether to converge all messaging on mailbox + agents-cli, or keep native mesh for Claude.
- Parallel shifts across separate codebases (NGH-013).

## Brief rendering, site layer and `doctor --cwd` (NGH-062)

`roleBrief` (launcher_crew.go, signature unchanged) delegates to `renderRoleBrief(stateDir, role, cwd, repoRoot,
crew, member)` in `brief_render.go`, which returns `renderedBrief{Text, Mode, Layers, Warnings}`. `crew` is the
crew.json-shaped object and `member` the role's crew.json member; `roleBrief` finds the member in the shift's
crew.json by identity. Every pane, `start` (warnings and the dry-run fields) and `doctor --cwd` render through it.

Order of a brief:

1. `# Task` and the human's text verbatim (the human-contact member only; read from `<state_dir>/prompt.txt`,
   never placeholder-substituted). The pane no longer appends the task after the brief; Claude panes still get the
   brief by file (`--append-system-prompt-file`).
2. `## Your shift`: a table of members by slot (slot, role, tool, model, `live` or `asleep` for a reserved
   Advisor/Coder not yet started) plus the human's Ops pane when the layout has one; then the worktree, branch and
   target branch from `worktree.json` and the repository's Build, Test one, Lint and Typecheck commands from
   `.nightshift/config.yaml` (absent ones omitted). Facts carry no word budget.
3. `# This repository`: `.nightshift/instructions.md` and `.nightshift/<role>.md`; then `# This site`: the site
   prose (`<state root>/instructions.md`). A heading appears only when its prose exists.
4. The role page. Standard mode: the persona (identity and what good looks like) and the role's one-page brief,
   then a generated `## Messaging` paragraph (members: `nightshift wait` and `report`, plus for Claude that a
   SendMessage only says to run `wait`; a Claude lead gets just that SendMessage line, its page already runs on
   `wait`). When the persona printed `# <Role>`, the brief's own title line is dropped and the brief follows under
   `## Your loop`, so it does not read as part of "What good looks like". A crew with
   `briefs: legacy` renders persona, `---`, `# Protocol`, `common.md` and the brief exactly as NGH-062 did.
5. `# Rules`, generated per role and never removed by another layer: members edit only claimed files and never
   commit or push; the lead never edits repository files or runs the suite and sends the human one line per
   milestone; everyone acts only on the body `wait` or `ack` prints. In the current set a role page that says its
   role never edits files (Advisor) or repository files (Decompiler, Researcher) gets "Never edit files" (or
   "repository files") in place of the claim rule. The current brief set (NGH-063) also adds the two
   rules `common.md` used to carry: never launch sessions, windows, tabs or crews (members ask Foreman; only
   Foreman runs spawn, consult, summon and respawn), and where shift state lives (`<state_dir>`, its `bus.jsonl`
   and `nightshift log`).

Per-crew text. Rewritten briefs may wrap text in `<!-- if KEY -->...<!-- end -->` (keys `apprentice`,
`reserved_coder`, `advisor`, `copilot`, `non_claude`, `worktree`, `queue`); the block stays only when the crew has
that member or feature. The legacy brief files are not tagged, so the renderer drops every sentence, and every whole
list item, of the role page that matches an absent feature (`crewFeatureWords`): Apprentice; summon / reserved
Coder; Advisor / consult; Copilot credit / credits / max_credits; agents-cli / non-Claude; worktree / land; queue.
Features: an Apprentice or Advisor member; `coder_slot` (reserved Coder); a Copilot member on a credit share or a
crew `max_credits`; any non-Claude member; `worktree.json` in the shift (or a `worktree` key in the crew object);
a container layout (`slots`, which has the Ops pane and the live queue). A crew without an Apprentice therefore has
no sentence naming the Apprentice in any member's brief. Non-Claude members also lose every sentence naming
ListAgents or SendMessage and the old "does not apply to you" line; Claude members other than the lead lose
sentences that drive panes with agents-cli (the lead keeps them while the crew has a non-Claude member).

Modes. A crew role's `brief_mode: standard|compact` (validated by the crew loader, kept in crew.json) wins;
otherwise `briefModeFor` applies the published table: compact for models containing `flash`, `luna` or `mistral`,
`terra` on reasoning `low`, any Copilot member on a credit share, and Codex `-mini`/`-nano` models; standard for
everything else (Opus, Sonnet, Sol, Astra, Argon). Compact replaces the role page with a numbered checklist of
literal commands (`ready`, `wait`, `claim`, `report`/`release`; the lead `wait`, `assign`/`note`, `commit`,
`result`), a three-line report template (`<VERDICT>: <summary>`, `Files:`, `Checks:`), the first 30 words of
`.nightshift/<role>.md` and the circuit breaker ("If a file not named in your assignment needs editing or a command
fails twice, run `nightshift report SHIFT ROLE "BLOCKED: <reason>"` and stop"; an Apprentice reports
`"APPRENTICE-NEEDS-SENIOR: <reason>"` instead, which `report` stores as the note the needs-senior uplift trigger
reads; the lead `result ... blocked`);
the shift facts, repository and site prose (repository: `instructions.md` only) and the rules still apply.

Budgets (words, `strings.Fields`): role page 250 standard / 150 compact, repository prose 150 combined per role,
site prose 50. Over budget is a warning, never an error: `brief <Label>: <layer> layer <n> words, budget <b>`,
printed by `start` and `doctor --cwd`. The legacy briefs exceed the role budget; the NGH-063 pages fit it.

### One-page briefs and `briefs: legacy` (NGH-063)

Every role has one brief file, whatever its tool: the renderer supplies the per-tool lines, so the `-cli`,
`-agy` and `foreman-agy-coder` variants are gone and every `crews/*.yaml` names `<role>.md`. A brief holds only
the role page: the numbered loop with literal commands (`{nightshift}`, `{shift}`, `{label}` placeholders),
role-specific rules, and `<!-- if KEY -->` blocks for crew features (a block on lines of its own takes its
newlines with it; an inline block keeps the line's end). `common.md` is retired: its live content is the
"Your shift" table and the two generated rules above. Personas are identity plus what good looks like.

The Foreman page is the §5 loop with the amendments (read the diff before Tester; TESTED-FAIL back with `note`;
`commit` makes the commit; between events scope the next assignment, never the code; an empty queue means stop
and wait) and four one-line event rules: `member-died` -> `respawn` and note it; `loop-detected` -> report
BLOCKED with the evidence, never retry the same fix; `asg-timeout` -> re-dispatch with the prior reports once the
claims are released; context running out -> `handover`, then stop. The Decompiler keeps its tool check and
`DECOMPILER-BLOCKED: missing tool <name>`; the Researcher and UI Tester pages follow the same shape.

The old files stay for one release: a crew manifest's top-level `briefs: legacy` (the only value; omit it for the
current set) makes the loader validate `brief:`/`persona:` names in `briefs/legacy/` and `personas/legacy/`,
`start` writes `briefs: legacy` into crew.json, and the renderer reads `legacy/common.md` plus the legacy brief
and persona, reproducing NGH-062's text. `assets.go` embeds both folders. A crew file outside the repository may still name a retired brief (`<role>-cli.md`, `coder-agy.md`, `foreman-agy-coder.md`): without `briefs: legacy` the loader resolves it to the current `<role>.md` (`coder.md`, `foreman.md`), records that name in crew.json, and prints one stderr line per crew per process, `crew <name>: brief <old> is retired; using <new>; update the crew file` (`briefs <a>, <b> are retired; using <x>, <y>` for several); under `briefs: legacy` the old names load the legacy files unchanged.

Role-layer words from `start --dry-run` (`brief_layers.role`, in this repository; budget 250 standard, 150 compact):

| Crew | Foreman | Advisor | Coder | Tester | Apprentice |
|---|---|---|---|---|---|
| default | 244 standard | 207 standard | 246 standard | 221 standard | 101 compact |
| work | 221 standard | 207 standard | 101 compact | 101 compact | - |
| codex | 235 standard | 197 standard | 236 standard | 211 standard | 211 standard |
| copilot | 135 compact | 101 compact | 101 compact | 101 compact | 101 compact |

Specialists: Decompiler 235 and Researcher 173 (standard). With `briefs: legacy` the default crew renders
3671 / 1273 / 1497 / 1557 / 101, as NGH-062 did. This repository's own `.nightshift` prose is 165 words for the
Foreman (instructions.md plus foreman.md) and 50 for the other roles, so the Foreman's repository layer is the one
remaining warning.

Site call sites in the launcher: `crewEnv` sets `siteEnv()` (sorted) after the inherited environment and before the
per-tool settings; `toolCommand` inserts `siteWebFlags(tool)` before the prompt (and its `-i`); the pane's Claude
argv appends `siteWebFlags("claude")`. `.nightshift/config.yaml` accepts the new top-level keys (`build`/`land`
validate them: `test_one`, `lint`, `typecheck` non-empty strings, `protected_paths` a list of non-empty strings).

### Site layer (`cmd/nightshift/site_layer.go`)

`loadSite()` reads `<state root>/site.yaml` and `<state root>/instructions.md` (the site prose, trimmed; Coder's
renderer budgets it at 50 words). Absent files give no limits, no env and `WebSearch: true`. `site.yaml` keys:
`allowed_tools` (list), `allowed_models` (list), `env` (map of string to string), `web_search` (bool, default true);
any other key is an error (`<path>: unknown site keys: K`), and `start`/`doctor --cwd` stop with exit 1 on an invalid file.

- Allow-lists: empty or absent = any. Entries are `path.Match` globs (`claude-opus-*`); a tool entry also matches its
  `spawn --cli` alias (`gemini` = `agy`). An empty member tool is `claude`. `start` (before the dry-run plan too)
  and `doctor --cwd` refuse the first member not allowed, exit 4, one stderr line:
  `<site.yaml path>: <Role> uses tool <tool>, not in allowed_tools (<list>)` or
  `<site.yaml path>: <Role> uses model <model>, not in allowed_models (<list>)`.
- `siteEnv() map[string]string`: the `env` map; `crewEnv` sets it for every member (Coder's one-line call).
- `siteWebFlags(tool) []string`, non-empty only with `web_search: false` (Coder appends it to `toolCommand`'s argv and to
  the Claude argv in launcher_pane.go):

| Tool | Flags |
|---|---|
| claude | `--disallowedTools WebSearch,WebFetch` |
| copilot | `--deny-tool url` (Copilot's `url()` permission with no argument covers all URL access of its shell and web-fetch tools) |
| codex | none: Codex has live web search only with `--search`, which Nightshift never passes |
| agy | none: no such flag |

### Repository `protected_paths` (`write_claims.go`)

`.nightshift/config.yaml` top-level `protected_paths`: repo-relative globs, `path.Match` syntax, case-insensitive,
`*` does not cross `/`; a pattern matching a parent folder protects everything under it (`secrets` protects
`secrets/a/key.txt`). The repository is the shift's `worktree.json` `worktree_dir`, else crew.json `cwd`. A missing or
unparseable config protects nothing (build/land report config errors). `claim` checks before taking the claims lock
and refuses the whole claim (all or nothing, nothing written), exit 4, one stderr line for the first protected path:
`<canonical path> is protected by .nightshift/config.yaml; ask the human`.

### `nightshift doctor --cwd REPO [--crew NAME]` (`init.go`)

Renders every role's brief with `renderRoleBrief("", label, REPO, repoRoot, crew, member)`, the crew being the
crew.json-shaped object `start` would write (`crewObject`, slots from `crewCells`; identities use the placeholder shift
`Nightshift-shift-doctor`). Crew: `--crew`, else the repository's `default_crew`, else `default`. Site allow-lists are
checked first (exit 4 as above). Human output:

    Briefs for <repo root> (crew <name>)
      <Role>       <mode>    role <w>/<b>  repository <w>/<b>  site <w>/<b>
    warning: brief <Role>: <layer> layer <n> words, budget <b>     (one per over-budget layer)
    All within budget.                                           (instead, when there are none)

JSON (`--json` or not a console): `{"repo", "crew", "roles": [{"role", "mode", "layers": [{"name", "words",
"budget"}]}], "warnings": [...]}`. Exit 0 even over budget; 1 for a missing folder, bad config or crew; 2 for
`--crew` without `--cwd` (`argument --crew: requires --cwd`). Plain `doctor` is unchanged.

### `start` (`launcher_start.go`)

Slot placement moved into `crewCells` and the crew.json object into `crewObject` (same keys and order as before).
After the members are known, `start` refuses site-disallowed members (exit 4), renders every member's brief
(`renderBriefs`) and prints each renderer warning to stderr (when printing; dry-run included), then continues.
The `--dry-run` plan's role objects gain, after `permission_flags`: `brief_mode`, `brief_layers` {role, repository,
site: words} and `brief_warnings` (list).


## Per-repository `.nightshift` configuration and instruction layer (NGH-043)

Nightshift keeps its global role personas and protocol briefs repository-agnostic. A repository
may supply a `.nightshift/` layer for its own build contract, instructions, role additions and
crew manifests:

```text
<repo>/.nightshift/
  config.yaml        # optional machine-readable repository contract
  instructions.md    # optional instructions appended for every role
  foreman.md         # optional role-specific instructions
  crews/             # optional repository-specific crew manifests
```

The supported `config.yaml` keys are `target_branch` (string, default branch for worktree
creation and landing), `default_crew` (string, used when `start --crew` is omitted), `build`
(a mapping with string `cmd` and numeric `timeout`, in seconds), and, since NGH-062, the top-level
strings `test_one`, `lint` and `typecheck` (shown in every brief's shift facts) and `protected_paths`
(a list of globs `claim` refuses). The config timeout is expressed
in seconds; the CLI `--timeout` option remains expressed in minutes. Unknown keys and non-mapping
YAML fail fast with a `SystemExit` naming the config path. When present, `start --dry-run` includes
the resolved configuration as `repo_config`.

Configuration discovery resolves the canonical repository root from `worktree.json`'s `repo_root`
when running in an isolated shift worktree; otherwise it uses the resolved working directory.
When `build --cwd` is omitted, the build uses the shift's recorded worktree/direct working folder.
When a git worktree is created, an existing `<repo_root>/.nightshift/` directory—including
untracked or modified files excluded through `.git/info/exclude`—is mirrored into the worktree
without staging or committing it. Tracked `.nightshift/` edits remain dirty and are preserved;
untracked local-only copies are never staged into recovery commits. `land` and `down` ignore these
untracked `.nightshift/` copies.

`start` resolves the target branch once from the explicit CLI option, repository config, or
`main`, and records that resolved target in `worktree.json`. Landing keeps that recorded target
stable unless `land --target-branch` explicitly overrides it.

Repository crew lookup checks `<repo_root>/.nightshift/crews/<name>.yaml` before falling back to
the built-in `crews/<name>.yaml`. This lets a work repository provide an approved manifest without
modifying Nightshift's built-in crew definitions.

### Crew lookup, `crews`, and start-time overrides (NGH-056, Go binary)

- **Lookup order.** `--crew NAME` takes the first of: machine `<state root>/crews/NAME.yaml`
  (`C:\nightshift\crews\`, honouring `NIGHTSHIFT_STATE_ROOT`), repository
  `<repo_root>/.nightshift/crews/NAME.yaml`, built-in `crews/NAME.yaml`. Every `loadCrew` caller
  (start, the pane fallback, spawn) uses the same order. An unknown name lists the crews from all three
  sources, deduplicated and sorted. Machine files keep machine-local crews out of the user profile and
  out of the repository.
- **`nightshift crews [--cwd DIR]`** prints JSON, sorted by name: one
  `{"name", "source": "machine"|"repo"|"built-in", "path", "active"}` entry per visible manifest, where
  `active` marks the file `--crew NAME` picks and `false` the ones it shadows. `--cwd` picks the repo layer.
- **`start --crew-file PATH`** loads that manifest directly (crew name = file stem, relative paths against
  the current directory). It cannot be combined with `--crew`; a missing file exits 1 with `no crew manifest <abs path>`.
- **Overrides** (start only), applied to the manifest's YAML before the normal validation, so every
  manifest check still applies:
  `--without ROLE` (repeatable, case-insensitive; the human_contact role cannot be dropped),
  `--role ROLE=TOOL:MODEL[:REASONING]` (repeatable, once per role; sets tool, model and reasoning and drops
  reasoning when it is not given; a tool change drops `context` if the new tool has no context tier, and
  switches the brief between `<stem>.md` and `<stem>-cli.md` when the target brief exists), and
  `--max-credits N` (replaces the top-level budget). A budget whose Copilot panes were all removed is dropped
  unless `--max-credits` was given; otherwise it is re-split with the usual 30-per-pane minimum.
- **Recording.** Only when an override is used, or `--crew` resolved to a machine file, `crew.json`
  (right after `"crew"`), the bus `start` event and the `start --dry-run` plan carry
  `"overrides": {"crew_file", "without", "role", "max_credits"}`, with unused keys omitted. Without these
  features the output is unchanged. Panes use the effective members in `crew.json`; spawn's manifest fallback
  reloads the effective crew from the recorded `crew_file` and overrides (`loadSavedCrew`).

`role_brief` stacks prompts in four sections: `# <Role>` for the persona from `personas/`,
`# Protocol` for `briefs/common.md` plus the role brief, `# Repository` for the resolved build
command and optional `.nightshift/instructions.md` plus `.nightshift/<role_lower>.md`, and `# Task`
for the Foreman's verbatim human task. Standard placeholders (`{shift}`, `{identity}`,
`{state_dir}`, `{nightshift}`, `{cwd}`) are expanded in repository instructions. The repository
section is omitted when no repository config or markdown instructions exist.

This repository self-hosts the layer with `target_branch: main` and configured build command
`.\verify.cmd` (building `build\nightshift.exe` via `scripts\build.cmd`, running `go test ./...`,
and running Go golden comparisons under pytest xdist: `test_go_readonly.py`, `test_go_write_commands.py`,
`test_go_lifecycle.py`, `test_go_launcher.py`, `test_go_crews.py`, `test_go_telemetry.py`; `test_nightshift_shifts.py`
is no longer in the configured build and the Python launcher is frozen). Its shared
architecture rule lives in `.nightshift/instructions.md`; Foreman-specific build and exact-clean-
commit conventions live in `.nightshift/foreman.md`, so other repositories do not inherit them.

## Go parity checklist (NGH-052)

Every Python subcommand and option from `nightshift_shifts.py` has been ported to the standalone Go binary (`build\nightshift.exe`). The table below records the status and verification evidence for every command and flag.

| Command | Flag | Go status | Evidence |
|---|---|---|---|
| `start` | `prompt` (positional) | native, golden-tested | `test_go_launcher.py:test_start_dry_run_matches_oracle` |
| `start` | `--prompt-file` | native, golden-tested | `test_go_launcher.py:test_start_dry_run_matches_oracle`, `test_go_launcher.py:test_start_missing_prompt_file_reports_the_oracle_error_line` |
| `start` | `--cwd` | native, golden-tested | `test_go_launcher.py:test_start_dry_run_matches_oracle` |
| `start` | `--no-worktree` | native, golden-tested | `test_go_launcher.py:test_start_dry_run_matches_oracle` |
| `start` | `--target-branch` | native, golden-tested | `test_go_launcher.py:test_start_dry_run_matches_oracle` |
| `start` | `--continue-from` | native, golden-tested | `test_go_launcher.py:test_start_dry_run_matches_oracle` |
| `start` | `--crew` | native, golden-tested | `test_go_launcher.py:test_start_dry_run_matches_oracle`, `test_go_launcher.py:test_start_dry_run_every_crew_manifest` |
| `start` | `--dry-run` | native, golden-tested | `test_go_launcher.py:test_start_dry_run_matches_oracle` |
| `start` | `(live start: install_profile, wt launch, worktree add)` | native, live-only | `launcher_start.go`: opens Windows Terminal via wt.exe and initializes worktree |
| `start` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `status` | `--all` | native, golden-tested | `test_go_readonly.py:test_go_matches_python_golden`, `test_go_readonly.py:test_go_status_rejects_python_image_and_recognizes_live_cmd_slots` |
| `status` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `down` | `shift` (positional) | native, golden-tested | `test_go_lifecycle.py:test_go_down_handover_receipt_and_bus` |
| `down` | `--hold` | native, golden-tested | `test_go_lifecycle.py:test_go_down_captures_live_tail_denials_and_retains_handover_after_hold` |
| `down` | `--close` | native, golden-tested | `test_go_lifecycle.py:test_go_down_handover_receipt_and_bus` |
| `down` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `spawn` | `shift` (positional) | native, golden-tested | `test_go_launcher.py:test_spawn_dry_run_matches_oracle` |
| `spawn` | `--role` | native, golden-tested | `test_go_launcher.py:test_spawn_dry_run_matches_oracle` |
| `spawn` | `--cli` | native, golden-tested | `test_go_launcher.py:test_spawn_dry_run_matches_oracle` |
| `spawn` | `--crew` | native, golden-tested | `test_go_launcher.py:test_spawn_dry_run_matches_oracle` |
| `spawn` | `--slot` | native, golden-tested | `test_go_launcher.py:test_spawn_dry_run_matches_oracle` |
| `spawn` | `--dry-run` | native, golden-tested | `test_go_launcher.py:test_spawn_dry_run_matches_oracle`, `test_go_launcher.py:test_spawn_dry_run_oracle_cases_reach_the_intended_branch` |
| `spawn` | `(live spawn: console.Inject)` | native, live-only | `launcher_spawn.go`: injects launcher command line into live pane shell |
| `spawn` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `loop` | `tickets` (positional) | native, golden-tested | `test_go_launcher.py:test_loop_usage_errors_match_oracle` |
| `loop` | `--project` | native, golden-tested | `test_go_launcher.py:test_loop_usage_errors_match_oracle` |
| `loop` | `--max` | native, golden-tested | `test_go_launcher.py:test_loop_usage_errors_match_oracle` |
| `loop` | `--cwd` | not golden-tested | parsed in `launcher_loop.go`; batch execution is live-only |
| `loop` | `--stall-minutes` | native, golden-tested | `test_go_launcher.py:test_loop_usage_errors_match_oracle` |
| `loop` | `--retries` | not golden-tested | parsed in `launcher_loop.go`; retry logic is live-only |
| `loop` | `--notify` | not golden-tested | parsed in `launcher_loop.go`; Mailkeep notification is live-only |
| `loop` | `--no-notify` | not golden-tested | parsed in `launcher_loop.go`; Mailkeep notification is live-only |
| `loop` | `(event stream and held-lock path)` | native, live-only | `launcher_loop.go`: multi-shift loop orchestration with result polling |
| `loop` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `resume` | `--project` | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `resume` | `--dry-run` | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `resume` | `--stall-minutes` | not golden-tested | parsed in `launcher_loop.go`; stall timeout is live-only |
| `resume` | `--notify` | not golden-tested | parsed in `launcher_loop.go`; Mailkeep notification is live-only |
| `resume` | `--no-notify` | not golden-tested | parsed in `launcher_loop.go`; Mailkeep notification is live-only |
| `resume` | `(lock clearing plus loop)` | native, live-only | `launcher_loop.go`: stale lock removal and continuation invocation |
| `resume` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `land` | `shift` (positional) | native, golden-tested | `test_go_lifecycle.py:test_go_land_requires_isolated_worktree` |
| `land` | `--target-branch` | native, golden-tested | `test_go_lifecycle.py:test_go_land_target_branch_precedence` |
| `land` | `--keep-conflicts` | native, golden-tested | `test_go_lifecycle.py:test_go_land_conflict_abort_or_keep` |
| `land` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `claim` | `shift`, `role`, `paths` (positional) | native, golden-tested | `test_go_write_commands.py:test_go_claim_release_persisted_byte_parity`, `test_go_write_commands.py:test_go_claim_cross_shift_live_conflict_and_worktree_isolation` |
| `claim` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `release` | `shift`, `role`, `paths` (positional) | native, golden-tested | `test_go_write_commands.py:test_go_claim_release_persisted_byte_parity` |
| `release` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `claims` | `shift` (positional) | native, golden-tested | `test_go_readonly.py:test_go_matches_python_golden`, `test_go_readonly.py:test_go_claims_matches_python_duplicate_keys_and_float_serialization` |
| `claims` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `msg send` | `shift`, `FROM`, `TO` (positional) | native, golden-tested | `test_go_write_commands.py:test_go_mailbox_guards_and_transitions` |
| `msg send` | `--kind` | native, golden-tested | `test_go_write_commands.py:test_go_mailbox_guards_and_transitions` |
| `msg send` | `--body` | native, golden-tested | `test_go_write_commands.py:test_go_mailbox_guards_and_transitions` |
| `msg send` | `--asg` | native, golden-tested | `test_go_write_commands.py:test_go_uplift_rejects_claims_for_every_owner_alias` |
| `msg send` | `--reason` | native, golden-tested | `test_go_write_commands.py:test_go_uplift_rejects_claims_for_every_owner_alias` |
| `msg send` | `--coding-asg` | not golden-tested | parsed in `write_msg.go`; verified by schema but not in golden assertion |
| `msg send` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `msg sent` | `shift`, `id` (positional) | native, golden-tested | `test_go_write_commands.py:test_go_mailbox_guards_and_transitions` |
| `msg sent` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `inbox` | `shift`, `role` (positional) | native, golden-tested | `test_go_readonly.py:test_go_matches_python_golden` |
| `inbox` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `ack` | `shift`, `role`, `id` (positional) | native, golden-tested | `test_go_write_commands.py:test_go_mailbox_guards_and_transitions` |
| `ack` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `build` | `shift`, `n` (positional) | native, golden-tested | `test_go_write_commands.py:test_go_build_records_logs_config_and_locks` |
| `build` | `--cwd` | native, golden-tested | `test_go_write_commands.py:test_go_build_records_logs_config_and_locks` |
| `build` | `--cmd` | native, golden-tested | `test_go_write_commands.py:test_go_build_records_logs_config_and_locks` |
| `build` | `--timeout` | native, golden-tested | `test_go_write_commands.py:test_go_build_explicit_timeout_overrides_zero_second_config_under_contention` |
| `build` | `--cancel` | not golden-tested | parser checked in `test_go_write_commands.py:test_go_write_parser_exact_bytes`; cancellation runtime not golden-tested |
| `build` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `log` | `shift` (positional) | native, golden-tested | `test_go_readonly.py:test_go_matches_python_golden` |
| `log` | `--follow`, `-f` | native, golden-tested | `test_go_readonly.py:test_go_follow_reads_appended_complete_lines_and_stops_at_down` |
| `log` | `--since` | native, golden-tested | `test_go_readonly.py:test_go_log_since_matches_python_integer_type_filter` |
| `log` | `--json` | native, golden-tested | `test_go_readonly.py:test_go_matches_python_golden` |
| `log` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `cleaner` | `--dry-run` | native, golden-tested | `test_go_lifecycle.py:test_go_cleaner_floor_locks_and_dry_run`, `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `cleaner` | `(live cleaner run)` | native, golden-tested | `test_go_lifecycle.py:test_go_cleaner_reaps_locks_claims_legacy_and_repeat_is_noop` |
| `cleaner` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `warden` | `shift` (positional) | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `warden` | `--dry-run` | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `warden` | `(non-dry-run delivery/nudge/recovery)` | native, live-only | `floor_patrol.go`: delivery of stored assignments, nudge via the console package, and crash recovery |
| `warden` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `gaffer` | `--max-shifts` | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `gaffer` | `--ticket` | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `gaffer` | `--crew` | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `gaffer` | `--repo` | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `gaffer` | `--tick` | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle` |
| `gaffer` | `--dry-run` | native, golden-tested | `test_go_launcher.py:test_floor_dry_runs_match_oracle`, `test_go_launcher.py:test_gaffer_floor_variants_match_oracle` |
| `gaffer` | `(non-dry-run dispatch)` | native, live-only | `floor_patrol.go`: launches shift under concurrency cap and marks floor state |
| `gaffer` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `consult` | `shift`, `question` (positional) | not golden-tested yet | parsed in `launcher_consult.go`; consult's real spawn is live-only |
| `consult` | `--asg` | not golden-tested yet | parsed in `launcher_consult.go`; consult's real spawn is live-only |
| `consult` | `(real spawn and prompt)` | native, live-only | `launcher_consult.go`: consult's real spawn is live-only |
| `consult` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `floor` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `floor say` | `FROM`, `TEXT` (positional) | not golden-tested yet | parsed in `floor_bus.go`; appends event to `floor/bus.jsonl` |
| `floor say` | `--to` | not golden-tested yet | parsed in `floor_bus.go`; defaults to `all` |
| `floor say` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `floor hear` | `ME` (positional) | not golden-tested yet | parsed in `floor_bus.go`; reads events addressed to reader or `all` |
| `floor hear` | `--since` | not golden-tested yet | parsed in `floor_bus.go`; non-regressing replay lower bound |
| `floor hear` | `--follow` | not golden-tested yet | parsed in `floor_bus.go`; live polling loop |
| `floor hear` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `statusline` | `(env-driven)` | not golden-tested yet | parsed in `floor_bus.go`; prints ANSI statusline from `NIGHTSHIFT_ROLE` / `IDENTITY` / `ROLE_COLOUR` |
| `statusline` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |
| `(top-level)` | `--pane` (Claude branch) | native, live-only | `launcher_pane.go`: trust dialog, /color, opening message |
| `(top-level)` | `--pane` (Codex naming) | retired (NGH-067) | Codex panes are not renamed; identity is the per-role colour scheme |
| `(top-level)` | `--pane` (OSC 11 background) | native, live-only | `launcher_pane.go`: OSC 11 pane background color escape sequence |
| `(top-level)` | `--pane` (stub argv) | native, golden-tested | `test_go_launcher.py:test_hidden_pane_start_runs_same_stub_argv_and_records` |
| `(top-level)` | `--state-dir` | native, golden-tested | `test_go_launcher.py:test_hidden_pane_start_runs_same_stub_argv_and_records` |
| `(top-level)` | `--start` | native, golden-tested | `test_go_launcher.py:test_hidden_pane_start_runs_same_stub_argv_and_records` |
| `(top-level)` | `--cwd` | native, golden-tested | `test_go_launcher.py:test_hidden_pane_start_runs_same_stub_argv_and_records` |
| `(top-level)` | `--crew` | native, golden-tested | `test_go_launcher.py:test_hidden_pane_start_runs_same_stub_argv_and_records` |
| `(top-level)` | `--slot` | native, golden-tested | `test_go_launcher.py:test_hidden_slot_entry_point_records_parent_shell` |
| `(top-level)` | `--slot` (cmd handover) | native, live-only | `launcher_pane.go`: cmd handover shell prompt |
| `(top-level)` | `--live` | native, live-only | `launcher_pane.go`: diagnostic ping/pong trial with live agents |
| `(top-level)` | `--brief-file` | native, live-only | `launcher_pane.go`: live trial brief file override |
| `(top-level)` | `(no args: layout trials)` | native, live-only | `main.go`: top-level Windows Terminal layout trials (`no args/--live`) |
| `(top-level)` | `-h`, `--help` | native, golden-tested | `test_go_launcher.py:test_help_matches_oracle` |

### Accepted differences
- The top-level program name reported in usage output is `nightshift.exe` instead of `nightshift_shifts.py`.
- A missing `--prompt-file` prints only the last traceback line (`[Errno 2] No such file or directory: ...`), exiting with code 1 in both implementations.
- YAML syntax errors display Go `yaml.v2` error text rather than PyYAML's formatting.
- Go's `-h` / `--help` text is the standard 80-column argparse rendering and ignores `COLUMNS` (Python argparse rewraps to `COLUMNS - 2`).

Installing `nightshift.exe` to PATH (default `C:\nightshift\bin`) makes the standalone binary available system-wide for live crew execution.

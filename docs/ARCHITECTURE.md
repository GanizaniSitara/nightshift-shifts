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
- `C:\nightshift\locks\` — working-folder locks and the build lock tree.
- `C:\nightshift\builds\` — the defined machine-level `BUILDS_DIR` path; the current `build`
  command still writes revision-bound evidence to `<state_dir>/builds/<N>.json`, not centrally.
- `C:\nightshift\floor.json` — machine-level floor state for reaping, patrol and dispatch.

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

Warden delivers only stored messages addressed to the recipient's current incarnation. A modern
`agents-cli send` receipt must say `submitted: true`. An older `enter: true`/`sent: <header>` receipt
also needs tail evidence: the header in history above an empty current composer. Explicit
`submitted: false` remains unconfirmed. The per-shift `warden-delivery.json` records each attempt
before injection; subsequent patrols check uncertain delivery through the tail without injecting
the same header again. If confirmation stays unavailable, the row remains stored for inspection.

Claude idle time comes from its session record. Other CLIs require `agents-cli tail` to report
`idle`, plus at least 300 seconds since the latest Foreman bus activity or process start. Busy or
unknown state is never nudged. `warden-nudge.json` throttles attempts, including uncertain sends.

Mailbox reads use a disposable database/WAL copy outside shift state, so a dry-run cannot create
SQLite WAL/SHM sidecars beside the real mailbox. A named Windows mutex keyed by the canonical
mailbox path guards the copy and every central `mailbox(create=True)` connection through close
and checkpoint. SQLite opens only the scratch copy for reads and rebuilds its SHM there. This
snapshot consistency guarantee covers writers using that helper; direct external SQLite writers
do not participate. Missing mailboxes create no state, and mutex waits have an explicit timeout.

All three commands are wired through the main parser and `COMMANDS` mapping. They are maintenance
and dispatch operations, not additional crew sessions: cleaner and warden are safe to run repeatedly,
and `--dry-run` is the inspection mode for all three.

## Container, not loop (owner decision, 16 Oct 2026; NGH-025)

The owner has reset the shape of the thing. Nightshift is **not a persistent process**. The
executable launches a container and exits: one Windows Terminal tab holding a grid of slots
(2x4 = 8 slots across two rows of four columns; 1x4 = 4 slots also supported), the first slots
filled with the crew from the manifest, the rest empty shells. From then on **Foreman owns the
container**: it may fill an empty slot through `nightshift spawn` (any CLI; the Nightshift CLI
is the one component that knows how to put a session into a tab slot, agents-cli only drives
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
  into). Empty and dead slots are free; a live member's slot is never taken. When a container pane's
  member exits, the pane leaves a cmd prompt: a spawned wrapper just returns to the slot shell it was
  typed into; a crew pane wt launched directly runs `cmd.exe /k ... --slot N` and becomes a slot
  shell, and `slot_shell` keeps the dead role in the slot record (so `status` shows it dead and the
  role can go back into its own slot). `status` adds `slots: [{slot, role, state, reusable}]` for a
  container shift.
- **Incarnation history.** When a pane writes `<role>.json` over an earlier one, the earlier record
  (minus its own history) is appended to `history`; `started` stays the current incarnation, so
  mailbox fencing is unchanged. `inbox` filters `inbound_unacked` to `recipient_incarnation == current`
  so a respawned member never tries to ack a dead incarnation's messages.
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
  `agents-cli _attach inject`, and marks the slot with the role and a `spawned` time. A start-mode pane reads its member
  from `crew.json` (falling back to the manifest), so spawned roles run like launched ones; `status`,
  `down` and the mailbox pick them up from `crew.json`.
- **spawn --crew NAME (NGH-029).** Takes the spawned role's brief, colour, tool, model, `reasoning` and
  `account` from `crews/NAME.yaml` instead of the shift's own manifest (and over a gone member's
  record); refused when NAME has no such role. An explicit `--cli` still overrides the tool, and the
  model then falls back as without `--crew`. The member written to `crew.json` carries all of it, so the
  pane wrapper starts that tool/model/reasoning/account. This is how Foreman proves each CLI as
  Apprentice inside a live default shift: `spawn SHIFT --role Apprentice --crew apprentice-codex`.
- **down --close / --hold.** Both write the handover, kill the crew and release the folder lock.
  `--close` (default) also closes the window; `--hold` explicitly leaves the tab open. When the
  crew is already dead and `handover.md` exists (Foreman already ran `down`, and a caller later runs
  `down --close`), the captured `handover.md` is preserved rather than overwritten with `(pane not readable)`.
  The member whose process tree is running `down` (Foreman ending its own shift) is killed last, after the
  lock is released and the JSON (with `held`) is printed. Foreman's brief makes `down` its last act
  after writing `result.json`.

## Role boundaries and permission plan (NGH-031)

Foreman is orchestration and review only for repository work. It reads diffs, reviews reports, assigns
repository changes to the crew's implementation roles, and assigns every verification pass to Tester.
Coder owns the launcher and correction changes. A nit, typo, or missing-test fix is a new Coder assignment; after Coder
returns it, Foreman dispatches the revision to Tester and commits only what Tester passes. Foreman never
edits repository files or runs the suite as a substitute for Tester. It may write the shift records under `{state_dir}`—for
example `result.json` and `handover.md`—because those are runtime state, not repository files.

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

The top row of the 2x4 container holds four fixed identities, one per column: **Foreman** (runs the
ticket's goal statement, sole human contact), **Advisor** (NGH-027), **Coder**, **Tester** (NGH-024);
all four are built, see "The default crew" below. The **Apprentice** (NGH-029) is a fifth, cheap
coder in slot 5 (bottom row, first column) of the default, codex, codex-copilot and apprentice-* crews;
slots 6..8 stay open for `nightshift spawn`. The
Advisor is a dormant session on a frontier model (Fable, Astra, Mistral Large 4 or Gemini's top
model, whichever the manifest names): it gets no instructions beyond its identity and does
nothing until Foreman is stuck on something it cannot resolve. Then, instead of asking the human,
Foreman asks the Advisor, and the Advisor is expected to take a sensible decision. Rules that
keep it bounded: Foreman asks only after it has tried itself; the question and the answer go
through the mailbox so the human can audit every decision; the Advisor never edits files and
never talks to Coder or Tester; the human is still asked when the ticket itself is wrong or a
decision cannot be undone. This is the reviewed design's "bounded consultant" (not a council).

As built (NGH-027):

- **Manifest role.** `crews/default.yaml` has `Advisor` in column 2: brief `advisor.md`, colour
  purple, count 1, role-level `model: claude-fable-5-1` (the crew model stays Opus 5.5).
- **Dormant opening.** `START_MESSAGES['Advisor']` tells it it is dormant: no READY, no polling,
  reading or running anything until Foreman messages it. Foreman's peers include `<shift>-advisor`;
  the Advisor's only peer is `<shift>-foreman`.
- **Brief (`briefs/advisor.md`).** On a Foreman message: `ack`, act on the stored body only; read
  context read-only (`bus.jsonl` if present, else the mailbox rows, plus ticket, claims, builds,
  code, only what the question needs); never edit, build, test, spawn, write tickets or talk to
  anyone but Foreman; go dormant again after answering.
- **Mailbox guard.** `msg send` refuses (SystemExit, nothing stored) any message to the member
  labelled `Advisor` unless the sender is the human contact ("only Foreman may address the
  Advisor"), and any message from it unless the recipient is the human contact ("the Advisor
  answers only Foreman"). Foreman <-> Advisor is an ordinary message.
- **Spawn from the manifest.** `spawn SHIFT --role R` for a role not in `crew.json` but named in the
  shift's crew manifest takes that role's brief and colour, and its model when the manifest role's
  tool is the spawned tool. So `spawn SHIFT --role Advisor` in an older three-pane shift gives a
  purple Fable Advisor on `advisor.md`; any other role behaves as before.
- **Foreman's consult rule (`briefs/foreman.md`).** Never message the Advisor routinely and do not
  wait for its READY. Only when stuck after trying: `msg send ... Foreman Advisor --kind assignment
  --asg adv-<n>` with the question, what was tried and what is seen; wait for the result, ack it,
  act on its DECISION, and `tasks-cli note` question and decision.
- **Answer format.** One decision, no options: a `result` with the question's asg and body
  `DECISION: ...` / `WHY: <two lines>`. When the ticket is wrong or the decision cannot be undone the
  first line is `ASK-HUMAN: <why>` followed by the recommended decision; that, and the routine-matters
  cases Foreman's brief already names, are the only times Foreman goes to the human.
- **Switching model or CLI later.** Astra (Codex) or Mistral Large 4 (OpenCode) is the Advisor's
  `model:`/`tool:` lines in the manifest plus the agents-cli transport the Gemini Coder already uses
  (NGH-012); the mailbox guard and briefs do not change.

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

At work, `crews/claude-work.yaml` is all Claude **Opus 5.5**, including the proposed Accountant;
there is no Fable there. `crews/copilot.yaml` is all Copilot **Gemini 3.8 Flash**, including the
proposed Accountant. A cheap observer elsewhere does not override those owner decisions.

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
configuration; work values are set at work. Shift budgets run across period boundaries and respawns.
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
authority, and whether level 4 may do more than ask Foreman. Work/cloud feed availability, attribution and
private mappings must be confirmed at work. These questions do **not** reopen warn-then-kill for Copilot.

Implementation acceptance must cover: every member including Accountant/dormant/spawned roles; first
sample spend; decimal AIC; repeat/delayed samples; confirmed reset vs unexplained decrease; respawn and
observer restart; duplicate adapters; unknown/stale/malformed values; period rollover with retained shift
usage; account totals without false shift attribution; known-subtotal stop with missing panes; exact-budget
stop and first-sample warning+stop; durable evidence before self-teardown; and no automatic restart/reset.
This ticket changes architecture only; saved fixtures and runtime verification belong to the build ticket.

## Components

| Piece | Role | Where |
|---|---|---|
| `nightshift_shifts.py` | the launcher: `start`, `status`, `down` (`--close` default, explicit `--hold`), `spawn` (NGH-025), `cleaner`, `warden`, `gaffer` and `floor.json` (NGH-035), the optional `loop`, `resume`, `claim/release/claims`, `msg/inbox/ack` (NGH-019), `build` (NGH-020), `log` (the shift bus, NGH-026) | this repo |
| `crews/*.yaml` | crew manifests: roles, counts, model, permission mode, colours, which brief each role gets, each role's `tool` (`claude`, `agy`, `codex`, `copilot`), `model`, optional `reasoning` (`low`\|`medium`\|`high`\|`xhigh`; copilot also `none`\|`minimal`\|`max`), `context` (`default`\|`long_context`, copilot only), `account` (`1`\|`2`) and `permissions` (`skip`\|`auto`\|`default`), pane layout | this repo |
| `briefs/*.md` | the instructions a role is launched with (`common.md` + `<role>.md`: `foreman.md`, `advisor.md`, `coder.md`, `tester.md`, `apprentice.md`, plus `*-cli.md` for non-Claude crews; placeholders filled in, `{state_dir}` and `{nightshift}` with forward slashes); the task text is appended for Foreman | this repo |
| `personas/*.yaml` | each role's persona (identity, what good looks like, how you work, never), rendered above the brief (NGH-032) | this repo |
| shift folder | `C:\nightshift\shifts\<shift>\` — the source of truth for one new shift (below) | data, not code |
| tasks-cli | the work queue: the loop takes ticket IDs, reads `codebase:` for the working folder, crews write notes, the loop moves status | `C:\Tools\tasks-cli.exe` |
| agents-cli | universal console driver for any agent TUI: peek a pane, inject a line, press a key, name/wait on a session | `C:\Tools\agents-cli` |
| Claude Code mesh | `ListAgents` / `SendMessage` between Claude sessions over named pipes (`~/.claude/sessions/<pid>.json`) | Claude Code |
| Windows Terminal | one window per shift, one pane per role; a "Nightshift Crew" profile (Cascadia Mono 12, Campbell) | `wt.exe` |
| `C:\scripts\nightshift.cmd` | shim on PATH that calls the repo script | C:\scripts |

## A shift, step by step

1. `nightshift start <TICKET|"task"> [--crew NAME] [--cwd DIR] [--continue-from SHIFT]`
   reads the ticket through tasks-cli, resolves the working folder (`--cwd` > ticket `codebase:` >
   current directory), takes the **folder lock** (`C:\nightshift\locks\`, exit 3 if another
   live shift holds it), writes `prompt.txt`, and runs `wt.exe` with one `new-tab`/`split-pane`
   per role. Each pane runs this same script as a **wrapper** (`--pane ROLE`), which launches the
   role's CLI, records `<Role>.json` (pid, name, start time, cwd), answers Claude's trust dialog
   if it appears, sets the role colour, and waits for the CLI to exit. `<Role>.json` also records
   the role's `tool` (claude or agy), which `status` reports per role.
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
4. Foreman ends the shift by writing `result.json`: `status` done|blocked, `stage`
   implemented|tested|deployed|validated, `evidence` per rung, `summary`, `changed`. A ticket may
   say `requires: <rung>` (default tested); done below the bar is recorded as `short`.
5. Foreman then runs `nightshift down --close` itself: it captures the last lines of each pane, writes
   `handover.md` (result, commits since start, test outcome, denials, claims, pane tails), kills the
   crew processes (Foreman's own tree last), releases the lock, and closes only that shift's Terminal
   window. `--hold` is an explicit override when the tab must remain open.

## The default crew: Foreman, Advisor, Coder, Tester, Apprentice (NGH-024, NGH-027, NGH-029)

- `crews/default.yaml` is Foreman (red), Advisor (purple, Fable, `advisor.md`), Coder (green),
  Tester (blue, `tester.md`) in slots 1..4 (top row) of the 2x4 container, and Apprentice (yellow,
  `apprentice.md`, agy on `gemini-3.8-flash-high`) in slot 5, leaving slots 6..8 as empty cmd shells
  for `spawn`. Foreman's peers are `<shift>-advisor, <shift>-coder, <shift>-tester, <shift>-apprentice`;
  everyone else's peer is `<shift>-foreman`. Other crews (`--crew NAME`) are unaffected.
- Apprentice (NGH-029): Coder (expensive model) gets anything needing judgement; Apprentice (cheap
  model) gets bounded, well-specified jobs with an explicit file list disjoint from Coder's claims; its
  output is reviewed by Foreman and goes through Tester exactly like Coder's. `START_MESSAGES['Apprentice']`
  tells it to send READY and await bounded assignments. A non-Claude Apprentice is driven like the agy
  Coder (mailbox `msg send` → `agents-cli send` → `msg sent`, `agents-cli wait --match
  'APPRENTICE-DONE|APPRENTICE-BLOCKED'`, `tail`, ack); Foreman relays the Tester verdict to it as a mailbox
  note, and on TESTED-FAIL it waits for the follow-up assignment.
- Per-CLI Apprentice manifests (2x4, the default Claude Foreman/Advisor/Coder/Tester plus Apprentice):
  `crews/apprentice-gemini.yaml` (agy, `gemini-3.8-flash-high`), `crews/apprentice-codex.yaml` (codex,
  `gpt-5.6-luna`, account 2), `crews/apprentice-copilot.yaml` (copilot, `gemini-3.8-flash-high`). They
  serve both `start --crew NAME` and `spawn --crew NAME`. OpenCode and Qwen have none yet: they are not
  in `TOOLS`, so `load_crew` would refuse the manifest.
- Apprentice CLI rotation (NGH-029), one live shift per CLI in this order, each recorded on the ticket
  with what broke and what was fixed: Gemini (agy, `apprentice-gemini`), Codex Luna on account 2
  (`apprentice-codex`), OpenCode with Mistral and Qwen (both waiting on a `TOOLS` entry), then Copilot
  CLI (`apprentice-copilot`, once TECH-225 lands). A live round trip is READY, ack of the assignment,
  the done report and the relayed Tester verdict, all on the shift bus.
- Handoff is Coder -> Foreman -> Tester. Coder reports an assignment done; Foreman reviews it and
  assigns verification to Tester with its own `--asg` id, saying what changed and what was asked.
  Coder and Tester never talk to each other.
- Tester runs the suite through `nightshift build` so the evidence is revision-bound and reviews the
  diff against the assignment. It edits test files only (claims disjoint from Coder's) when verification
  requires; a missing-test finding is assigned to Coder, and product, nit, or typo fixes are not made by
  Tester. Foreman verifies the resulting revision flow and commits only after Tester passes it.
- Its report starts `TESTED-PASS` or `TESTED-FAIL` and cites `builds/<n>.json` (head, dirty, exit),
  test counts, findings and the test files it changed. A TESTED-PASS on a clean tree is the
  tested-rung evidence Foreman puts in `result.json`; with a Tester present Foreman does not run the
  suite itself. A TESTED-FAIL goes back to Coder as a new assignment. Without a Tester, Foreman cannot
  claim the tested rung and reports the highest supported rung as short or blocked.

## Mixed crews: per-role tool and model (NGH-012)

- A manifest role may say `tool: claude` (default), `agy`, `codex`, or `copilot`; anything else is
  refused naming the allowed tools. `model:` on a role overrides the crew `model:` for that role only.
- Permissions are manifest data (NGH-029): a role may say `permissions: skip | auto | default` (anything
  else is refused); without it the role takes the crew-level `permission_mode` (default `auto`).
  `permission_flags(tool, permissions)` is the only place the flags are chosen, when the pane command is
  built: claude `skip` = `--dangerously-skip-permissions`, `default` = no flag, anything else =
  `--permission-mode <value>` (so `auto`, or a crew-level `plan`); agy, codex and copilot have no
  classifier mode, so `skip` and `auto` both mean approve-all (agy `--dangerously-skip-permissions`, as
  `C:/scripts/launch.ps1` starts agy; codex `--dangerously-bypass-approvals-and-sandbox`; copilot
  `--allow-all`) and `default` (or any other value) adds nothing. The key travels into `crew.json` and through
  `spawn` (including `--crew NAME`), and `start --dry-run` shows each pane's resolved `permission_flags`.
  The Apprentice runs `permissions: skip` in `default.yaml` and the three `apprentice-*` manifests (owner
  decision, 16 Oct: an agy Apprentice otherwise stops at a shell prompt on every command); Claude roles keep `auto`.
- An agy pane runs `%LOCALAPPDATA%\Microsoft\WinGet\Links\agy.exe --model <model> <permission flags> -i
  "<brief>\n\n<opening message>"` (`agy_command`). agy has no `--name` or system-prompt flag, so the brief
  is the prompt text. The wrapper scrubs the env the same way (`crew_env`), then names the pane with
  `agents-cli name <pid> <identity>` (`register_name`, failures ignored) so it can be addressed by its
  shift identity. The trust-dialog answer and `/color` are Claude-only and skipped.
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

- Every `start` pane, crew panes and empty container slots alike, is titled `<ticket or shift> shift`:
  `NGH-025 shift`; a free-text shift uses its shift name (`Nightshift-shift-1a2b3c4d shift`). So the
  window reads the same whichever pane has focus. `--suppressApplicationTitle` keeps the CLI from
  overwriting it. Copilot needs `COPILOT_DISABLE_TERMINAL_TITLE=1` because
  `--suppressApplicationTitle` alone does not hold. One helper (`pane_title`) builds it for
  `wt_command` and the dry-run plan (`roles` and `slots`). Before NGH-028 panes were
  `<ticket or shift> <Role>` and empty slots
  `<ticket or shift> slot N`.
- The legacy trial modes keep `Nightshift | <Role> | <trial>`.
- `loop` sets its own console title to `Nightshift <first>..<last>` (`Nightshift NGH-019` for one
  ticket; `loop_title`), and a finished loop closes the Terminal window with that title.
- `down` closes the hwnd recorded in `launch.json`; without one it falls back to the single window
  titled `<ticket or shift> shift`, or the older `<ticket or shift> <Role>`, or a bare
  `Foreman`/`Coder` for shifts from before NGH-023.

## Codex identity, colour and ticket extraction (NGH-033)

Codex panes add an ordered footer containing the thread title, model (including reasoning) and
context remaining. It deliberately omits the current directory. The startup `/rename <Role> (<identity>)`
helper names the pane once: it requires a live child and an empty current composer, including while
the pane is busy. It rejects drafts, multiline or dialog ambiguity, unknown hints and errors, and
uses a double-read, deadline and at-most-one injection.

The five named Windows Terminal schemes are Nightshift red, purple, green, blue and yellow, with
one Campbell base profile. `install_profile` registers the schemes; initial `--colorScheme` selects
the scheme for each Codex pane. The matching pane background is applied with OSC 11 and VT enabled
before Codex launch, including
spawned and reused Codex slots. Campbell `#0C0C0C` is restored on exit and before non-Codex reuse.
No per-role Codex syntax-theme override is required.

The launcher extracts a literal `Ticket <ID>:` header from the first line of the prompt or UTF-8
prompt file, with `<ID>` matching `TICKET_RE`. Extraction precedes the continuation handover append.
It writes the ID to `ticket.txt` and uses it for the common `<ticket> shift` title.

## Copilot identity, colour and terminal setup (NGH-039)

- `copilot_command` passes `--name '<pane label> (<identity>)'` so the role is the Copilot session
  name, the same idea as Codex `/rename`. No `/rename` injection for Copilot.
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
lists, then `---` and a `# Protocol` heading, followed by `common.md` and the role's brief. The
placeholders (`{identity}`, `{shift}`, `{nightshift}`, ...) are filled across the whole text. Every
CLI (claude, agy, codex, copilot) gets this text, because `--start` builds all of them from `role_brief`.

A manifest role may name its file with `persona:` (relative to `personas/`, as `brief:` is to
`briefs/`); without the key a role gets `personas/<role lower>.yaml` when that file exists, and a
custom role with neither gets no persona block. `load_crew` refuses a named persona that is missing
or not a YAML mapping. The persona is carried on each member in `crew.json`; a shift whose
`crew.json` predates it falls back to the default lookup. `spawn` of a label that is not a role
(`Coder2`, `Helper`, `Advisor2`) gets `<label>.yaml` if it exists, else `coder.yaml`, matching its
`coder.md` brief fallback.

## The shift folder is the source of truth

```
C:\nightshift\shifts\<shift>\
  prompt.txt       the task as given to Foreman        ticket.txt   ticket id (when started from one)
  launch.json      wt argv, launcher pid, window hwnd   Foreman.json / Coder.json / Tester.json   pid, name, start time, cwd, history
  crew.json        members (incl. spawned), slots, cwd  slots/<N>.json  what each container slot holds (NGH-025)
  result.json      the shift contract (above)           handover.md  written by down
  claims.json      file claims per role                 denials.json auto-mode classifier refusals seen
  mailbox.sqlite3  messages: stored → sent → acknowledged, with sender/recipient incarnation (NGH-019)
  bus.jsonl        the shift bus: every message, sent, ack and start/spawn/result/down event, in order (NGH-026)
  bus.lock         held for a moment by each bus append (O_EXCL, stale after 10s)
```
Anything that needs to survive a dead pane, a restart or a different CLI lives here, not in a
process. `status` and `resume` are computed from it.

## The loop (optional batch runner)

Since NGH-025 a shift ends itself (Foreman runs `down`); the loop is an optional way to run
several tickets unattended. `nightshift loop T1 T2 …` (or `--project NGH [--max N]`) is deterministic
Python, no model: start a shift → poll for `result.json`, crew death, a 20-minute idle stall, and
classifier denials → `down --close` → note the ticket → next. A died/stalled shift is rerun with a fresh crew
that gets the previous handover (`--retries`, default 2); `blocked` stops the loop for a human.
By default the loop and `resume` send no completion or halt email. `--notify` explicitly opts
into owner notification through Mailkeep; the compatible `--no-notify` flag remains accepted.
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

## The shift bus (NGH-026)

`<state_dir>/bus.jsonl` is the whole shift in one file, one JSON object per line, appended in order.
There is no bus process: each command appends its own line and `nightshift log` reads the file.

| Event | Written by | Fields (besides `seq`, `ts`, `event`) |
|---|---|---|
| `start` | `start`, once the shift folder exists (never on `--dry-run`) | `shift`, `ticket`, `cwd`, `crew`, `members` (labels) |
| `msg` | `insert_message`, the one place every mailbox row is stored: `msg send` and the protocol-violation note `ack` files | `id`, `kind`, `sender`, `recipient`, `asg`, `body` |
| `sent` | `msg sent` | `id` |
| `ack` | `ack` | `id`, `by`, `outcome` (`acknowledged`, `already acknowledged`, `protocol violation` + `reason`) |
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

## Concurrency

As currently built: folder lock between shifts (NGH-010); file claims between coders inside a shift
(NGH-011); build slot (NGH-020) serialises expensive builds per folder and records revision-bound
evidence. Claims are checked against every live shift: a claim held by any role in another live shift
conflicts, and `claim` reports the owning shift and claim time, exits 4, and changes none of the
requested paths when any requested path conflicts. Claims belonging to dead shifts are ignored.

### Target evolution: per-shift git worktrees and Foreman landing (owner decision, 16 Oct 2026; NGH-040)

Running concurrent shifts directly in a single checkout causes cross-shift file-claim stalls on shared
files (`nightshift_shifts.py`, `test_nightshift_shifts.py`, `docs/ARCHITECTURE.md`). Rather than adding a
dedicated merge-queue pane ("Refinery"), Nightshift evolves toward **per-shift git worktree isolation**
paired with **Foreman-driven landing (`nightshift land`)**:

1. **Worktree per shift (`C:\nightshift\worktrees\<shift>\`).** When `nightshift start` targets a git
   repository (unless `--no-worktree` is passed), it resolves `repo_root` and the target branch (default
   `main`), creates branch `shift/<ticket>-<shortid>` from `HEAD`, checks out a linked worktree at
   `C:\nightshift\worktrees\<shift>\`, and writes `<state_dir>/worktree.json`
   (`{repo_root, worktree_dir, branch, target_branch, base_sha, created_at}`). Every crew pane and
   container slot runs with `cwd = C:\nightshift\worktrees\<shift>\` (including Claude Foreman's
   path-scoped `Edit(//c/nightshift/worktrees/<shift>/**)` deny rule). No whole-shift folder lock is held
   on `repo_root` while the crew codes and tests; `claims.json` coordinates roles *within* the shift
   (`Coder`, `Apprentice`, `Tester`) without blocking concurrent shifts in their own worktrees.
2. **Foreman-driven landing (`nightshift land <shift>`).** After `Tester` reports `TESTED-PASS` on a
   clean worktree commit, `Foreman` runs `nightshift land <shift>` before writing `result.json` and
   calling `nightshift down`. `nightshift land` acquires the short-lived repository landing lock
   (`C:\nightshift\locks\<repo>.json`) only for the landing operation and handles three cases:
   - **Case 1 — Fast-forward (`main` unchanged since worktree base):** Advances `main` cleanly to the
     worktree `HEAD` without clobbering unrelated uncommitted notes in `repo_root` (refusing if dirty
     tracked files in `repo_root` overlap the landed diff), writes `<state_dir>/landed.json`
     (`{repo_root, target_branch, base_sha, landed_sha, landed_at}`), appends a `land` event to
     `bus.jsonl`, releases the repo lock, and returns `{"status": "landed", "sha": "<landed_sha>"}`
     (exit `0`).
   - **Case 2 — Clean rebase (`main` moved ahead, `git rebase main` succeeds cleanly):** Rebases the
     worktree branch onto the new `main` `HEAD`, releases the repo lock, and returns
     `{"status": "rebased_needs_test", "new_base": "<sha>", "head": "<rebased_sha>"}` (exit `2`).
     `Foreman` dispatches one verification pass to `Tester` (`nightshift build`) on the rebased tree and
     then re-runs `nightshift land <shift>` (which now fast-forwards under Case 1).
   - **Case 3 — Rebase conflict (`git rebase main` reports conflicts):** Aborts the rebase and releases
     the repo landing lock immediately so other shifts are never held up, returning
     `{"status": "conflict", "target_head": "<sha>", "files": [...]}` (exit `4`). `Foreman` assigns its
     own `Coder` (who already holds full context of the ticket's changes) to rebase onto `main` and
     resolve the conflicting files in `C:\nightshift\worktrees\<shift>\`, dispatches `Tester` to verify,
     and re-runs `nightshift land <shift>`.
3. **Teardown and reaping (`down` and `cleaner`).** When `<state_dir>/landed.json` exists, `nightshift down`
   and `nightshift cleaner` remove `C:\nightshift\worktrees\<shift>\`, prune worktrees, and delete the
   merged `shift/<ticket>-<shortid>` branch. When a shift ends or is reaped without landing (`blocked`,
   halted, or crashed), `shift/<ticket>-<shortid>` is preserved and recorded in `handover.md` and
   `floor.json` so unmerged work is never lost.

## Design vs built — reconciliation (keep current)

| Reviewed design said | Built | Status |
|---|---|---|
| No resident runtime; launch from the human's shell; Windows Terminal, two panes | same; the default crew is now five panes in a 2x4 container (NGH-024, NGH-027, NGH-029) | matches |
| Named Claude sessions on the native mesh; permission-mode parity | same (`--name`, `--permission-mode auto` for Claude roles; manifest `permissions:` override) | matches |
| Mailbox with stored→sent→acked, incarnation fencing, `inbox`/`ack`, protocol-violation notes | NGH-019 | matches |
| Folder lock, worktree lease at `assign` time | folder lock (NGH-010) and intra-shift file claims (NGH-011) built; evolving to per-shift git worktrees (`C:\nightshift\worktrees\<shift>\`) + Foreman `nightshift land` (NGH-040) | NGH-040 queued |
| Build slot, revision-bound evidence, cancellation (G4) | NGH-020 | in progress |
| Acceptance gates G0–G5 as the delivery sequence | replaced by ticketed increments run by the loop; G0–G3 and most of G5 are covered, G4 pending | departure in method, not in content |
| `down` with handover; `resume` re-dispatches acked-but-unfinished work | `down`/handover, loop retries with handover carry-over, `resume` after reboot | matches |
| Advisors as bounded consultants; later Tester/Reviewer/DevOps/Controller | Tester (NGH-024) and a dormant Fable Advisor that only Foreman may consult (NGH-027) built into the default crew; out-of-shift floor CLI roles `cleaner`, `warden`, `gaffer` built (NGH-035) | matches / extended |
| Second coder, heterogeneous CLIs later | Apprentice in slot 5 (NGH-029), Codex crew (NGH-033), Copilot crew (NGH-037/NGH-039) | built |
| "Portable package", CLI language open | Python in its own repo; binary decision NGH-021 | open |
| — (not in the design) | loop, result ladder, crew manifest, opt-in Mailkeep notification, denial capture | additions |
| — (owner, 16 Oct) | container with slot grid (2x4/1x4), `slots/`, `spawn`, Foreman-owned `down --close/--hold` (close default), launcher exits; loop optional | NGH-025, built |
| — (owner, 16 Oct) | `C:\nightshift\` state root, `floor.json`, and `nightshift cleaner / warden / gaffer` CLI subcommands | NGH-035, built |
| — (owner, 16 Oct) | Accountant in slot 6: accounting log, source contracts, budgets, warning then shift teardown | NGH-034, proposed / unbuilt; owner review before build |
| — (owner, 16 Oct) | Per-shift git worktrees (`C:\nightshift\worktrees\<shift>\`) and Foreman-driven `nightshift land` (fast-forward / clean rebase + retest / conflict resolution by shift's own Coder + Tester) | NGH-040, target |

## Not decided / not built

- Accountant implementation and optional hold/rejig policy: **NGH-034**, proposed above; the mandatory
  Copilot warning and shift-budget teardown requirement is decided.
- Per-shift git worktrees and `nightshift land` implementation: specified and queued in **NGH-040**.
- Personas beyond Foreman + Advisor + Coder + Tester + Apprentice (DevOps, Reviewer, validator council):
  design in NGH-014, to be agreed with the owner before any is added. Roadmap on NGH-001.
- Which frontier model the Advisor should run long term across all manifests (Claude Fable in `default.yaml`,
  GPT-6 Astra in `codex.yaml`; Copilot's owner-set policy is Gemini 3.8 Flash for every role).
- Whether to converge all messaging on mailbox + agents-cli, or keep native mesh for Claude.
- Single binary (NGH-021): PyInstaller bundle vs Go port — owner's call.
- Parallel shifts across separate worktrees / codebases (NGH-013, NGH-040).

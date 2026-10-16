# Nightshift portable shifts design, reviewed revision

Revision of `docs/portable-shifts-design.md` incorporating the Fable critique
(`docs/portable-shifts-fable-critique.md`, items C1 to C15). Owner decisions of 15 October 2026 are
preserved unchanged. The original design is left as written. This remains a proposal: nothing below
exists until its gate passes.

Markers: **[OWNER CHOICE]** needs the owner to pick; **[UNRESOLVED]** is an implementation choice
deferred to the gate that informs it; **[VERIFIED]** is backed by source or a live observation in the
critique; everything else is design.

## Purpose and first release

Nightshift brings up a named shift of agents, gives each member an identity and an assignment,
coordinates work through one authoritative mailbox, and brings the shift down with a durable handover.
One package and one manifest schema work at home and at PIMCO, both on Windows Terminal. Local
profiles supply executable paths, authentication references and repository locations; work data and
credentials never travel with the package.

The first release has exactly two members: **Foreman** and **Coder**, both Claude Code on Claude Opus
5.5, in one Windows Terminal window, one tab, two equal columns. Foreman is the human's normal
conversational interface. The Coder pane is for visibility and emergency intervention; the human does
not normally operate it. Both panes are real interactive sessions. No print-mode workers, no log
viewer standing in for the TUI.

The future eight-pane layout (two rows by four) is unchanged as a target; pane count never dictates
active member count. More coders and heterogeneous CLIs follow a measured first shift.

## Owner decisions (unchanged)

Prefix NGH; Foreman leads; initial roles Foreman and Coder; both Opus 5.5; Windows Terminal at home
and work; one row, two columns; the human talks to Foreman only; worktrees and slow builds handled
explicitly; later roles Investigator, Reviewer, Tester, DevOps, Controller; optional frontier-model
Advisor, two independent advisors as a later option; Foreman-only peering and a controlled bus as the
starting point.

## Engineering choices after review

| Original proposal | Reviewed choice | Why |
| --- | --- | --- |
| Resident shift runtime owning registry and mailbox | **`nightshift` CLI + one SQLite file per shift + launcher stub.** No resident process in the first release. Owner agreed 15 October. | C8. The sender's own session performs delivery; SQLite WAL serialises CLI writers; `locking.exclusive` guards only launch/resume. **Owner constraint: nothing can be installed as a Windows service at work**, so no design element may depend on a service, a scheduled task registered by an installer, or any elevated install. |
| Windows named pipe as local command interface | **Deferred.** Not in the first release. | C8. Nothing needs it until a non-Claude adapter cannot call a CLI. |
| Adapter delivering bus messages to the Claude session | **Native `SendMessage` is the delivery channel. The mailbox is the record.** No injection path in the runtime. | C1, C4. The mechanism exists and is name-addressed [VERIFIED]; injection has no working busy/prompt guard for Claude today [VERIFIED]. |
| agents-cli for observation and controlled session operations | **Human-side diagnostic only.** Not a runtime dependency. | C4, C12. Go build, unknown commit, Rust port unverified on Windows [VERIFIED]. |
| Build runner component | **A lock plus a recipe wrapper inside the CLI.** | C10. |
| Seven message kinds | **Four:** `assignment`, `ack`, `result`, `note` (flagged). | C8. |
| Commands up/status/join/hold/down/resume | **up, status, down, resume.** `join` becomes a status view; `hold` deferred. | C7, C8. |

Implementation language of the CLI: **[UNRESOLVED]** until G1. Python inside the existing package is
the default because `worktree.py`, `locking.py` and `shift_state.py` are reusable as they stand
[VERIFIED]; a Go or Rust binary is justified only if start-up latency inside the Coder's Bash tool
proves to matter.

## Roles and authority

| Role | Responsibility | First release |
| --- | --- | --- |
| Foreman | Interpret the brief, cut bounded assignments, dispatch, track, resolve blockers, assess evidence, report to the human | Active |
| Coder | Implement one assignment in its leased worktree, run permitted checks, submit evidence | Active |
| Investigator, Reviewer, Tester, DevOps, Controller | As in the original design | Later: role briefs only |
| Advisor | Bounded consultation on artefacts and a concrete question; no dispatch, no implementation | Optional |

Role, member name, model, CLI and account remain separate fields; a model change does not change
identity or authority. Foreman is not duplicated by a manager above it. In the first release Foreman
accepts on explicit evidence (diff, recorded checks) and states in every handover that independent
review is not staffed.

Only Foreman may `assign`, `steer` and request `stop`. Coder sends only to Foreman. The CLI refuses a
reserved verb from a non-Foreman token and logs the attempt. **This is advisory enforcement**: all
members run under one OS account with shell access; strong isolation is out of scope and the handover
says so (C13). The native mesh does not restrict routes; the role brief and the mailbox do.

## Shift definition

```yaml
name: example-change
profile: home
repository: example-repository
brief: brief.md
defaults:
  cli: claude
  model: claude-opus-5-5
  permission_mode: <owner choice>      # identical for every member, see below
layout:
  rows: 1
  columns: 2
members:
  - id: foreman
    role: foreman
    pane: 0
  - id: coder-1
    role: coder
    pane: 1
limits:
  coders: 1
  heavy_builds: 1
coordination: foreman-only
```

**[OWNER CHOICE] Permission mode.** Both members must run under the **same** permission mode: a
receiver in a different mode holds cross-session messages for its human's approval, and nobody is
operating the Coder pane [VERIFIED, C1]. The options are (a) both in the default mode, which means
the human will be asked to approve Coder tool calls in the Coder pane, contradicting "the human talks
to Foreman only", or (b) both with permissions bypassed, which the owner has previously reserved for
explicit opt-in. The design does not pick; the manifest field is mandatory and `up --check` refuses a
mismatch.

## Identity and registration

The runtime resolves the manifest to a unique shift ID and a registry row per member: member ID,
incarnation number, stub pid and start time, Claude agent pid and start time, Claude `sessionId`,
mesh name, workspace, pane index, current assignment. Titles and fuzzy process-name matches are never
addresses.

**Pane bootstrap (C3).** Every pane's command line is `cmd /k nightshift member-start --shift <id>
--member <member>`. The stub records its own pid and start time against the member, sets
`NIGHTSHIFT_SHIFT`, `NIGHTSHIFT_MEMBER` and `NIGHTSHIFT_TOKEN` (random, per incarnation) in its
environment, then runs `claude --name ngh-<shift>-<member> --model claude-opus-5-5
--permission-mode <mode> <startup context>`. Everything the Claude session spawns inherits those
variables together with `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID` [VERIFIED].

**Mesh names are shift-unique**: `ngh-<shift>-foreman`, `ngh-<shift>-coder-1`. Duplicate names resolve
"latest wins" in `SendMessage` [VERIFIED]; a bare `foreman` would misroute across shifts.

**Readiness (C1).** A member is ready only when all of the following hold within 60 s of the stub
starting: a descendant Claude process exists; `%USERPROFILE%\.claude\sessions\<agent-pid>.json`
exists with `messagingSocketPath`, `sessionId` and the expected `name`; and the member appears in the
other member's `ListAgents`. A spawned process alone is not a ready member. A member that fails
readiness is reported as failed; `up` stops there and never substitutes injection.

**Startup context** combines the shift brief, the role brief, member identity, allowed routes, the
workspace boundary, acceptance criteria, the message header convention (below) and any saved
handover. Foreman receives the objective; Coder receives one bounded assignment. Existing repository
instructions still apply.

## Mailbox and delivery

**Mailbox.** One SQLite file per shift, WAL mode, written only through `nightshift msg ...`. Message
row: message ID, shift ID, sender and recipient member IDs, sender incarnation, assignment ID,
kind, body or artefact path, created time, correlation ID, delivery state (`stored`, `sent`,
`acknowledged`, `handled`). The mailbox is the authoritative record; terminal text is not.

**Authentication (C5).** `nightshift msg send` presents `(NIGHTSHIFT_MEMBER, NIGHTSHIFT_TOKEN,
CLAUDE_CODE_SESSION_ID)`; the CLI accepts the write only if all three match the registry row for a
live incarnation. Same-account limits apply as stated under authority.

**Delivery.** The sender's own session delivers with `SendMessage(to: <recipient mesh name>,
message: <header line + body>, notify_when_idle: true)` immediately after the mailbox write, then
records `sent`. Delivery means the message reached the session, not that it was read [VERIFIED].

**Header line (C6).** Every bus message body starts with one line:
`NGH shift=<id> msg=<id> kind=<kind> asg=<id> inc=<n>`. On receipt the recipient runs `nightshift
ack <msg-id>`. If the CLI answers "already acknowledged", the recipient stops. Acceptance of an
assignment is keyed by `(assignment ID, incarnation)`; a result from an older incarnation is stored
and flagged, never accepted. Exactly-once execution is not promised; accepted work is never replayed.

**Routing.** Star: Foreman to Coder, Coder to Foreman. Broadcast disabled. Peer consultation later
still goes through the mailbox.

**Idle members** are woken by the delivery itself; nobody polls an inbox. **[UNRESOLVED until G2]**
whether delivery to an idle session starts a turn with no human input; if it does not, the role brief
adds a bounded `notify_when_idle` wait on Foreman's side and the limitation is reported, not hidden.

**Failure ladder (C4).** Native delivery, then hold and escalate: Foreman tells the human which member
is unreachable, with the registry row and the last mailbox state. There is no injection path in the
runtime. `agents-cli tail` and `send` remain available to the human from their own shell for
diagnosis; Foreman may read `agents-cli tail` snapshots if G0 shows they work for Terminal panes, and
treats screen stability, an idle composer or process exit as observations, never as acceptance.

## Assignments and worktrees

An assignment has an ID, objective, acceptance criteria, **base commit**, expected scope, owner,
dependencies and evidence. States: queued, assigned, working, blocked, submitted, checking, accepted,
cancelled; transitions are validated by the CLI and persisted. A `result` is a submission, not
acceptance.

**Worktrees (C9).** Each coding assignment owns branch `ngh/<shift>/<asg>` and a lease under the
configured worktrees directory, created from the recorded base commit, never from the dirty shared
checkout. The existing `orchestration/worktree.py` lease semantics apply: one checkout per branch, a
conflict is raised and reported rather than served, dirty leases are kept on kill. A replacement Coder
resumes the same lease. Foreman inspects with read-only git commands against the lease and never runs
writing tools there. Dirty user checkouts are detected and preserved; nothing resets or cleans them.

Later parallel coders get separate leases; Foreman screens for scope conflicts before dispatch;
contract changes are agreed first or serialised; one integration writer combines accepted candidates
into a shift branch; publishing and deployment require authority stated in the brief.

## Builds and verification

One heavy-build slot (C10): a dedicated build worktree per repository, held under `locking.exclusive`
while a recipe runs. The wrapper asserts the tree is clean, checks out the exact commit detached,
records `(commit, recipe hash, toolchain version)`, runs the recipe inside a Windows Job object with
kill-on-close so cancellation takes the whole process tree, and stores status, log path, artefacts,
start and end time and exit code. Identical triples share a valid result. A newer commit may supersede
a queued job; running jobs are never killed merely because a newer request appeared. A `running` row
whose pid and start time no longer match a live process becomes `interrupted` on reconciliation,
never `passed`.

Cache validity is a recipe declaration (`cache: keep | clean`); the wrapper does nothing cleverer.
Coder may run bounded lightweight checks in its lease; tests on uncommitted code are not
commit-specific evidence. Review examines a fixed diff; checks run on that same candidate. More
coders only after measuring queue wait, build time, correction time and delivered throughput.

## Shift lifecycle

Commands: `nightshift up <manifest>`, `status <shift>`, `down <shift>`, `resume <shift>`, plus
`up --check` (prerequisites only).

**Launch principal (C2).** `up` must run in the human's interactive logon session: it prints its
session ID and desktop name and refuses when they are not the active console session and `Default`.
The LocalSystem supervisor service never opens the sheet. **`up` is run by the human from their own
shell.** No service and no installer-registered task is involved: the owner confirmed on 15 October
that nothing can be installed as a service at work, so the human-run shell is the only launch
principal the package supports. The home supervisor service is unrelated to shifts.

**Up** validates the manifest and prerequisites (`claude`, `git`, `wt.exe` resolved to a path, the
repository, the profile), allocates the shift ID and SQLite file under `locking.exclusive`, leases
the first assignment's worktree, opens the two-pane sheet through `wt.exe -w ngh-<shift>` and verifies
a visible window by enumeration (exit code is not evidence), waits for readiness of both members, and
records partial startup explicitly. Repeating `up` for an active shift reports it; it never duplicates.

**Status** shows the registry, mailbox summary, assignment states, build slot and the last handover.
This is what the original `join` becomes: a view, not an incarnation.

**Down** sends a `note` requesting a handover, drains or explicitly cancels outstanding work per
policy, persists state, stops only owned processes by pid and start time, and reports anything
unresponsive; forced termination is explicit. Branches, leases, uncommitted changes and evidence
remain. Cleanup is a separate operation after recoverability is checked.

**Resume** reconciles persisted state with live processes, then opens new panes running
`claude --resume <sessionId> --name <mesh name>` per member (new incarnation, same session where
supported, replacement from the handover otherwise), and reports the difference. Each resumed member's
first instruction is `nightshift inbox`; it trusts the mailbox over its own memory. Outstanding
messages reconcile by ID; accepted work is not repeated. Restarts discover already-owned children by
pid and start time before launching replacements.

**Terminal closure (C7).** Closing the window is expected to terminate the members (pseudoconsole
closure). It is never treated as deliberate shutdown: `status` reports what survived and `resume` is
the recovery. **[UNRESOLVED until G5]** confirmed behaviour on this Terminal build.

A permission prompt is a human-awaiting state, never idle and never safe to act on; timeouts escalate
through Foreman; nothing answers approvals automatically.

## Advisors

Advisor is a bounded consultation: Foreman supplies a concrete question, constraints and artefacts;
the Advisor returns ranked risks, missing decisions, simpler alternatives and specific changes. It
cannot assign, steer or implement. Spend and time are bounded per consultation. Advisors receive
artefacts, never the live conversation.

**One frontier Advisor by default (C15).** A second, independently consulted frontier model is
triggered, not default: when Advisor and Foreman disagree on a hard-to-reverse decision (schema,
publish, deploy, authority), or when the owner asks. Protocol when triggered: same evidence to both,
critiques collected before either sees the other's, Foreman synthesises agreement and disagreement and
adjudicates on evidence. Two similar answers establish nothing. Genuine policy and product choices
escalate to the human.

This revision is the output of the one authorised Fable consultation; no further review rounds or
implementation agents are authorised by it.

## Portability and packaging

The versioned package contains the `nightshift` CLI, the launcher stub, role briefs, the sample
manifest, home and work profile examples, the build recipe schema and the acceptance gates below.
Authentication stays in the installed CLI configuration; no credentials in manifests, prompts or logs.
Model IDs are explicit; no silent downgrade.

**Dependencies (C12).** Core loop: `claude`, `git`, `wt.exe`, the package. Workstation conveniences
that are **not** dependencies: `agents-cli`, `launch.ps1`, `tasks-cli`. Ticket notes, where a ticket
exists, go through `tasks-cli`; the retired tasks-MCP default (`127.0.0.1:8876`) still present in
`shift.py`, `goal_shift.py`, `dispatcher.py` and `watchdog.py` [VERIFIED] must not be inherited by
anything the package reuses, and a missing note writer fails loudly in `up --check`. No installer for
the first spike; reuse installed tools; fail clearly when one is missing; never mutate package
environments. **No services, ever, in the package**: the work machine cannot install them (owner,
15 October), so the package is a folder plus a user-level CLI and nothing that needs elevation,
registration or a daemon. Web deployment continues to leave unattended dispatch disabled.

## Acceptance gates (replaces the acceptance sequence; C14)

Each gate is go/no-go for the next. A failed gate produces a ticket with the exact command line and
observation, not a workaround.

- **G0 Terminal.** From an interactive Session-1 shell, open one new Terminal window with two equal
  panes running the placeholder; verify a visible window by enumeration; verify both panes respond to
  input; verify `agents-cli tail` reads a pane (records whether snapshot diagnosis survives the move
  to Terminal). Negative control: the same command from the service context fails as recorded on 15
  October.
- **G1 Identity.** Two stubs start two Opus 5.5 sessions with shift-unique names and the same
  permission mode; both `.claude/sessions/<pid>.json` files appear with `messagingSocketPath`,
  `sessionId` and the names; each appears in the other's `ListAgents`. Repeat three fresh launches;
  any missing registration reproduces the known failure and is filed.
- **G2 Delivery.** Round trip with header line and `ack` while the recipient is idle, busy, holding a
  draft, and sitting on a permission prompt; no draft damage, nothing approved; exactly one idle
  notice. Duplicate-name test fixes the naming rule; mixed-permission-mode test demonstrates the
  hold-for-approval behaviour; kill and `--resume` test shows the same `sessionId` and that the name
  persists or must be re-applied.
- **G3 One assignment.** Foreman writes an assignment to the mailbox and delivers it; Coder acks,
  changes a harmless fixture repository in its lease from the recorded base commit, submits a commit
  and evidence; Foreman inspects the diff and accepts or sends one correction. A deliberate resend
  proves one acceptance. No human message to Coder.
- **G4 Build slot.** A queued heavy build on the exact revision, revision-bound evidence, reuse of a
  compatible warm workspace, baseline timings recorded, one cancellation that kills the child tree,
  one simulated crash reconciled to `interrupted`.
- **G5 Stop and resume.** `down` with an unfinished assignment and a dirty lease; close the window and
  observe process fate; `resume`; Coder's first act is reading its inbox; accepted work not repeated;
  handover read back with assignments, revisions, results, blockers, usage labelled unknown where
  unknown.

A first live shift may be declared after G3; G4 and G5 follow before any second coder.

## Known blockers and next decisions

- Terminal activation: leading cause is launch from outside the interactive session (the supervisor is
  LocalSystem; the only Terminal process is in Session 1) [VERIFIED]. G0 confirms.
- Mesh registration is observed to fail for some launcher-started sessions [VERIFIED]; G1 measures.
- **[OWNER CHOICE]** permission mode for both members.
- **[OWNER CHOICE]** whether Foreman may read `agents-cli tail` snapshots of the Coder pane at all, or
  diagnosis is transcript-only (the repository already uses transcripts for stall detection).
- **Resolved 15 October:** no resident runtime (owner agreed); launch principal is the human's own
  shell; no services or installer-registered tasks anywhere in the package (cannot be installed at
  work).
- **[UNRESOLVED]** CLI language (default Python in-package); idle-wake behaviour; name persistence on
  resume; Terminal closure behaviour.
- The repository has unrelated modified and untracked files; preserve them; no broad staging.

## Reference points

Unchanged from the original, with one addition: `%USERPROFILE%\.claude\sessions\<pid>.json` is the
session registry the readiness gate reads, and `SendMessage`/`ListAgents` are the delivery contract.
Historical tickets TECH-013 and TECH-204 remain history, not specification.

## Change summary

| Change | Critique item |
| --- | --- |
| Native `SendMessage` is the single delivery channel; registration, shift-unique names and permission-mode parity become readiness gates; the spike is specified step by step | C1 |
| `up` must run in the interactive session; the LocalSystem supervisor never opens the sheet; window verified by enumeration | C2 |
| Pane bootstrap stub binds member to process lineage and environment; no title matching | C3 |
| Injection removed from the runtime; failure ladder is native then hold-and-escalate | C4 |
| Member authentication via stub-issued token plus inherited session ID | C5 |
| Header line on every message; idempotent `ack`; acceptance keyed by assignment and incarnation | C6 |
| `join` becomes a status view; `resume` assumes members die with the window; Coder reads inbox first | C7 |
| Resident runtime, named pipe, `hold`, three message kinds and the build-runner component removed from the first release | C8 |
| Assignments pin a base commit; leases reuse `worktree.py` semantics; Foreman reads leases read-only | C9 |
| Build slot as lock plus recipe wrapper; Job-object cancellation; recipe-declared cache validity; crash reconciles to interrupted | C10 |
| Resume specified on `--resume <sessionId>`; name persistence tested, not assumed | C11 |
| Package dependencies reduced to claude, git, wt.exe; dead tasks-MCP default excluded; `up --check` fails loudly | C12 |
| Authority enforcement labelled advisory in every handover | C13 |
| Acceptance sequence replaced by ordered gates G0 to G5; first live shift after G3 | C14 |
| One Advisor by default; second advisor on a disagreement trigger or owner request | C15 |

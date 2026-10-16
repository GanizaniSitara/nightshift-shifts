# Nightshift portable shifts design

Draft for implementation and one independent Fable review. Owner decisions recorded on 15 October 2026. This is a proposed extension to Nightshift, not a claim that the runtime below exists.

## Purpose and first release

Nightshift brings up a named shift of agents, gives each member its identity and assignment, coordinates work, and brings the shift down with a durable handover. The same package and configuration schema should work at home and at PIMCO, both using Windows Terminal. Local profiles supply executable paths, authentication references and repository locations; work data and credentials do not travel with the package.

The first release has exactly two active members: Foreman and Coder, both running Claude Code with Claude Opus 5.5. One Windows Terminal window contains one tab with two equal columns. Foreman is the user's normal conversational interface. The Coder pane provides visibility; the user should not need to manage the Coder directly. Both panes remain real interactive CLI sessions, with direct intervention available for diagnosis or recovery. No log-viewer replacement for the agent TUI.

The future eight-pane layout is two rows by four columns. Pane capacity does not dictate active agent count. Multiple coders are allowed later. The first release proves the lifecycle and communication path with one coder before adding coordination complexity.

## Decisions and proposals

Owner decisions: Nightshift is its own project under prefix NGH; Foreman leads the shift; initial roles are Foreman and Coder; both use Opus 5.5; Windows Terminal runs at home and work; the initial layout is one row with two columns; the human normally interacts only with Foreman; worktrees and slow builds must be handled explicitly. Additional responsibilities are Investigator, Coder, Reviewer, Tester, DevOps and Controller. An optional Advisor should use a frontier model, with two independently consulted frontier models a possible later extension.

Proposed engineering choices, subject to review: a small local runtime owns the registry and durable mailbox; Windows named pipes expose its local command interface; SQLite stores shift state; agents-cli provides observation and controlled session operations; an adapter delivers bus messages to the interactive Claude session. These choices must be proven by a delivery spike before building the whole system.

## Roles and authority

| Role | Responsibility | Initial status |
| --- | --- | --- |
| Foreman | Interpret the brief, assign bounded jobs, track progress, resolve blockers, assess evidence and report to the user | Active |
| Coder | Implement assigned changes in its worktree, run permitted checks and return evidence | Active |
| Investigator | Explore code and dependencies and propose implementation paths | Later |
| Reviewer | Review a fixed revision against requirements and correctness | Later |
| Tester | Exercise a fixed revision and report reproducible results | Later |
| DevOps | Maintain build recipes, environments, packaging and deployment preparation | Later |
| Controller | Analyse measured usage, spend, build waits and resource pressure | Later |
| Advisor | Critique plans and difficult decisions independently, without dispatch or implementation authority | Optional consultation |

Role, member name, model, CLI and account are separate fields. A model change does not change identity or authority. Foreman is not duplicated by a second manager above it. In the first release, Foreman performs review using the diff and recorded checks; it must not describe this as independent reviewer acceptance.

Only Foreman requests peering into other shift members and asks the runtime to steer or stop them. Workers initially send messages only to Foreman. Later profiles can permit peer questions or bounded delegation separately. Model quality can inform the selected profile, but cannot itself grant authority. The bus enforces routing and assignment transitions. Agents with unrestricted shell access under one OS account can bypass workflow restrictions; strong isolation is outside the initial scope and must not be claimed.

## Shift definition and session registry

A declarative manifest defines the roster, role briefs, model, repository, local profile, pane slots and concurrency limits. An illustrative schema follows; command and field names are proposals.

```yaml
name: example-change
profile: home
repository: example-repository
brief: brief.md
defaults:
  cli: claude
  model: claude-opus-5-5
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

The runtime resolves this into a unique shift ID and a registry mapping each stable member ID to its agent session ID, process identity including start time, workspace, terminal identity where available, adapter and current assignment. Display titles and fuzzy process-name matches are not authoritative addresses. Replaced processes get a new incarnation ID; old acknowledgements cannot advance a new incarnation's work accidentally.

Startup context combines the shift brief, role instructions, member identity, allowed message routes, workspace boundary, acceptance criteria and any saved handover. Foreman receives the overall objective; Coder receives a bounded assignment. Existing repository instructions remain applicable. Startup must confirm model/session identity and readiness; a spawned process alone is not a ready member.

## Runtime components

1. A shift runtime supervises only its registered processes, persists state and serialises mutations.
2. A command client exposes start/status/hold/down/resume and message/build operations to humans and agents.
3. A terminal launcher creates the two interactive panes and records their association with members.
4. A Claude adapter observes readiness and delivers one message through one selected channel.
5. A build runner executes configured commands and records revision-specific results.

Keep these within the existing Nightshift package where practical. Do not start a second product or replace the existing owner dashboard. Existing web deployment must continue to leave unattended dispatch disabled unless explicitly requested. No changes to unrelated dirty files are part of this work.

## Interprocess communication

Agents call the Nightshift command client to submit messages. The client talks to a per-shift local named pipe. The runtime commits messages to SQLite before acknowledging receipt to the sender. The mailbox, not terminal text, is the authoritative record. Only the runtime writes the database. The named pipe is a transport and wake-up path, not durable storage. Use a current-user access policy and scope every operation to a registered shift member; do not expose a network listener in the first release.

Message envelope: message ID, shift ID, sender and recipient member IDs, sender incarnation, assignment ID if applicable, kind, body or artifact reference, creation time, correlation ID and delivery state. Kinds initially include assignment, acknowledgement, progress, question, result, correction and lifecycle request. The runtime must not accept an arbitrary sender label as sufficient identity; define how the launched process receives its member credentials before implementation.

States distinguish stored, delivery attempted, acknowledged and handled. A successful console write proves only a delivery attempt. An explicit agent acknowledgement references the message ID. Retrying an uncertain delivery can duplicate text, so agents and runtime must deduplicate assignment acceptance by ID. Exactly-once execution is not promised. Completed assignments are never blindly replayed after restart.

First-release routing is a star: Foreman to Coder and Coder to Foreman. Foreman receives progress and completion events without needing the user to poll. Idle crew members must be awakened through the tested delivery mechanism. They should not consume model turns endlessly polling an empty inbox. Peer consultation later still goes through the bus and is visible in the shift record; broadcast is disabled initially.

## Interactive delivery and agents-cli

The installed agents-cli currently reports a Go build. The Rust repository targets interface compatibility; both sources contain AttachConsole/CONOUT$ observation and CONIN$/WriteConsoleInputW delivery. Both guard Claude-to-Claude injection and recommend native SendMessage. These are useful existing components, not proof that individual Windows Terminal panes work correctly.

Prefer native Claude messaging for delivery if the spike proves it can address these independently launched interactive sessions, wake an idle recipient, preserve normal human interaction and provide usable acknowledgements. An existing mention of SendMessage does not establish that arbitrary sessions join the same mesh. If unavailable, evaluate a controlled hook/adapter path. Console injection is a fallback only after tests demonstrate prompt detection, draft preservation and busy-state behaviour. Do not silently use two channels or bypass the current Claude-to-Claude guard to make a demo appear to work.

The first implementation must explicitly choose and document one working delivery path. Do not replace interactive sessions with print-mode workers merely because stream-json is easier to automate. Foreman may use agents-cli screen snapshots for diagnosis; screen stability, an idle composer and process exit do not establish acceptance. All control is bound to exact registered identities, not all agents on the workstation.

## Assignments and worktrees

An assignment has an ID, objective, acceptance criteria, base commit, expected change scope, owner, dependencies and evidence. Its states are queued, assigned, working, blocked, submitted, checking, accepted or cancelled. State changes are persisted and validated by the runtime. A result message is a submission, not automatic acceptance.

Each coding assignment owns a branch and worktree with one writer at a time. A replacement Coder resumes the assignment's workspace; it does not get a fresh empty worktree by default. Foreman inspects but does not edit that worktree. The initial Coder completes one assignment before starting another. Dirty existing repositories and user edits are detected and preserved; never reset or clean them to establish a baseline.

Later parallel coders get separate assignment worktrees. Foreman identifies conflicting scope and dependencies before dispatch. Worktrees prevent file-write collisions but cannot prevent semantic conflicts. Contract changes must be agreed first or serialised. One integration writer combines accepted candidate changes into a shift branch. Final delivery is tied to the tested integrated revision. Publishing, main-branch integration and deployment require authority stated in the shift brief.

## Builds and verification

The first release has one heavy-build slot and a durable queue. Coder can run bounded lightweight checks; expensive repository builds and suites go through the runner. Build jobs bind to the exact source revision, build profile and relevant environment inputs. The runner verifies that its workspace matches those inputs before running. Tests on mutable uncommitted code cannot count as commit-specific acceptance evidence.

A reusable build workspace preserves valid incremental outputs and dependency caches. Only one job occupies it at a time, and it cannot switch revisions during a build. Toolchain-specific cache validity, restore behaviour and cleanup rules belong in repository build recipes. Do not share mutable output directories between parallel jobs. Use existing provisioned tools; dependency installation is a separately authorised operation.

Requests for identical inputs can share a valid result. A newer commit can supersede an obsolete queued job, but running builds are not killed merely because a new request appears. Record queued/running/passed/failed/cancelled/interrupted separately, with logs, artifact paths, start/end time and exit status. Cancellation must account for child processes. A crash during a build produces interrupted/unknown status until reconciled, never an inferred pass.

Review examines a fixed diff; checks run against that same candidate. Combined verification runs after integration where the resulting revision differs. In the two-agent MVP Foreman accepts based on explicit evidence and reports that independent review is not yet staffed. More coders should be added only after measuring queue wait, build time, correction time and delivered throughput.

## Shift lifecycle

Proposed commands: `nightshift up <manifest>`, `status <shift>`, `join <shift>`, `hold <shift>`, `down <shift>` and `resume <shift>`.

Up validates prerequisites and manifest, allocates state, creates or attaches the required workspaces, opens the terminal sheet and waits for readiness. Partial startup is recorded and shown; it must not leave an untracked worker. Repeating up for an active shift must attach or report it, not duplicate it.

Join restores visibility without creating another agent incarnation. Closing a pane or the terminal window is not proof of deliberate shift shutdown; detect the actual processes and report what survived. Whether a CLI survives terminal closure is a platform capability to test, not a requirement to fake.

Hold stops new dispatch and requests checkpoints. Busy agents remain busy until they acknowledge. A permission prompt is awaiting human action, not idle or safe to inject into. Timeouts escalate to the user through Foreman; do not silently answer approvals.

Down requests a handover, drains or explicitly cancels outstanding jobs according to policy, persists state and stops only owned processes. Unresponsive members are reported; forced termination is explicit. Branches, worktrees, uncommitted changes and evidence remain. Cleanup is a separate operation after recoverability is checked.

Resume reconciles persisted state with live process identities and workspaces, then resumes the exact Claude session where supported or creates a replacement from the handover. It reports the difference. Outstanding messages are reconciled by ID and accepted work is not repeated. Runtime restarts require a single-instance lock and discovery of already-owned children before launching replacements.

## Advisors

Advisor is a bounded consultation role, not a permanent extra manager. Foreman supplies a brief, concrete question, constraints and relevant artifacts. The Advisor returns significant risks, missing decisions, simpler alternatives and specific changes. It cannot assign work, control peers or rewrite implementation by default.

A frontier model is the intended default for Advisor. Two advisors using different frontier models are a later option: give them the same evidence independently, obtain their critiques before sharing conclusions, then have Foreman synthesise agreement and disagreements. Two similar answers do not establish correctness; validation still rests on evidence and tests. Bound calls, time and spend per consultation, and escalate genuine policy/product choices to the user.

This design receives one Fable consultation. Fable should preserve an independent critique and propose an improved revision. No recursive review rounds or implementation agents are authorised by that consultation.

## Portability and packaging

Deliver a versioned Nightshift package with the shift CLI/runtime, Claude adapter, terminal launcher, role briefs, sample manifest, work/home profile examples, repository build recipe schema and acceptance procedure. Authentication remains in the approved installed CLI configuration; no credentials in manifests, prompts or logs. Model IDs are explicit, with no silent downgrade. Later adapters add Copilot, Codex accounts and Vibe/Qwen without changing the role or message schema.

No installer is required for the first spike. Reuse installed tools, record prerequisites and fail clearly if one is missing. Do not mutate package environments. Existing launch scripts and agents-cli are reusable candidates; changes should be minimal and separately verified rather than presuming the Rust port is already deployed.

## Acceptance sequence

1. Open one new Windows Terminal window/tab with equal Foreman and Coder panes; preserve existing windows, font preferences and global settings. Verify both panes are independently addressable.
2. Start two real interactive Claude Opus 5.5 sessions with recorded role and session IDs. The user speaks to Foreman only.
3. Foreman submits a bounded assignment through the bus; Coder acknowledges its ID, changes a harmless fixture repository in a worktree, requests a build and submits the resulting commit and evidence.
4. Foreman inspects the diff and evidence and reports acceptance or sends one correction through the bus. No direct user message to Coder is required.
5. Test delivery while idle, while busy, while a draft is present and while an approval prompt is open. No draft damage or accidental approval is acceptable.
6. Retry an uncertain delivery and prove one assignment acceptance. Restart the runtime and prove it does not launch a duplicate coder or repeat accepted work.
7. Stop and resume the shift with an unfinished assignment and a preserved dirty worktree. Reconcile an interrupted build without reporting success.
8. Demonstrate a queued heavy build, revision-specific evidence and reuse of a compatible warm workspace. Record baseline timings rather than promising a speedup.
9. Read back the durable handover and show assignments, revisions, results, blockers, usage observations and next actions. Unknown usage is labelled unknown; token estimates are not subscription invoices.

## Known blockers and next implementation decisions

On 15 October a delegated layout trial created C:/scripts/_oneshot_nightshift_terminal_grid.py for an eight-pane sheet. wt.exe and the packaged executable returned zero without a visible new window. A pre-existing Terminal process was left intact; access to its thread desktop was denied. The activation cause remains unconfirmed. The next layout probe must use the reduced two-pane requirement and independently verify the visible window. A successful launcher exit code is insufficient.

The delivery spike must establish native Claude peer addressing and acknowledgement, or select a tested alternative while preserving the interactive TUI. Windows Terminal pane peering is another open capability test. These are go/no-go dependencies for the first live shift, not reasons to build a large bus before testing them.

Decide the minimal runtime implementation language after inspecting reusable Nightshift components and deployed tooling. Define member authentication to the local pipe, concrete prompt/busy guards, restart ownership and build child-process cleanup before implementing the happy path. The repository currently has unrelated modified and untracked files; preserve them and avoid broad staging or commits.

## Reference points

- Existing source: C:/git/nightshift, C:/git/agents-cli and C:/git/agents-cli-rs.
- Existing launcher: C:/scripts/launch.ps1. Use its actual current flags; older skill examples reference a retired scripts path.
- Gas Town role and lifecycle concepts: https://github.com/gastownhall/gastown/blob/main/docs/overview.md.
- Claude model identifier reference: https://platform.claude.com/docs/en/models/opus-5-5/whats-new-opus-5-5.
- Windows Terminal command interface: https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments.
- Existing related tickets: TECH-208 packaging/public release, TECH-209 budget awareness, TECH-222 context handling. TECH-013 and TECH-204 are historical records, not current runtime specifications.

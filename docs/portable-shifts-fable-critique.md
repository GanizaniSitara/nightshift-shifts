# Fable critique of the Nightshift portable shifts design

Review of `docs/portable-shifts-design.md` (owner decisions of 15 October 2026). One pass, no
implementation. Written 15 October 2026 from session NGH-001.

**Model attestation.** This session's harness declares the model as Claude Fable 5.1
(`claude-fable-5-1`) running under Claude Code 2.1.280 (`AI_AGENT=claude-code_2-1-280_agent`).
There is no API-level receipt available from inside a session, so the attestation is the harness
declaration and nothing stronger. No substitution was observed.

**How to read this.** Every claim is tagged **[verified]** (read from source, a running process, a
file on disk, or a tool contract in this session, with the location named) or **[inference]**
(design reasoning or platform knowledge not re-tested here). Where something could not be verified,
an executable acceptance test is given instead of a guess.

---

## 0. Verified facts the critique rests on

| # | Fact | Source |
| --- | --- | --- |
| F1 | A native Claude-to-Claude peer mechanism exists on this box today. Each live session writes `%USERPROFILE%\.claude\sessions\<agent-pid>.json` containing `sessionId`, `peerProtocol: 1`, `peerFeatures: [notify_idle, artifact_yield]`, `kind: interactive|bg`, `name`, `nameSource`, `status`, `pidDomain: win32:<host>` and `messagingSocketPath: \\.\pipe\LOCAL\cc-msg-<hash>`. The process also carries `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`, `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN` in its environment, inherited by anything its Bash tool runs. | `.claude/sessions/*.json` read 15 Oct 12:2x; `env` in this session |
| F2 | Registration in that mesh is **not reliable in practice**. At review time `agents-cli ls` saw seven live Claude consoles; `ListAgents` from this session saw two peers (M1, M3). The launcher-started sessions LAP-042 (started 07:17 today) and PROJ-025 (13 Oct) have a `<pid>.key` file but **no `<pid>.json`**, are invisible to `ListAgents`, and their `launch-info.json` has `SessionId: ""` because `launch.ps1` resolves the session id from that same missing file. Same binary path, same launcher, same day as this session, which did register. Cause unknown. | `agents-cli ls`; `ListAgents`; `tasks/in-progress/*.launch-info.json`; `C:/scripts/launch.ps1` `Set-InputBarName` |
| F3 | `SendMessage` contract (this session's tool definition): the address is the session **name** from `ListAgents`; duplicate names resolve "latest wins"; a successful send means the message reached the session, not that it was read; messages drain at the receiver's next tool round; **a receiver running in a different permission mode holds cross-session messages for its human's approval and may let them expire**; `notify_when_idle: true` gives a one-shot idle/exit notice; `@path` attaches nothing on the receiving side; a subagent's send goes out under its parent session. | `SendMessage` tool schema loaded in this session |
| F4 | Installed `agents-cli` is the Go build (`go1.23.3`, commit "unknown"). The Rust port was verified only under wine on a Linux host on 2026-10-09; it is not deployed. `send` refuses Claude-to-Claude without `--force`; `--via native` is implemented for Codex only; `can_native` is false for every Claude session. Injection is `AttachConsole` + `CONIN$` + `WriteConsoleInputW`, Enter sent after a length-scaled delay, submission confirmed by screen regexes. | `agents-cli version`; `C:/git/agents-cli-rs/VERIFICATION.md`; `cmd/agents-cli/commands.go`, `console.go`, `sessions.go` |
| F5 | `agents-cli`'s state heuristic returns **`unknown` for every idle Claude session right now** (LAP-042, PROJ-025, M1, M2); only the busy one classifies. `idleRe` expects a bare `>`/`›`/`❯` line that the current Claude composer does not render. So the busy/idle/prompt guard the design wants for injection does not currently work for Claude. | `agents-cli ls` state column; `sessions.go` `guessState`, `idleRe`, `promptRe` |
| F6 | Two prior field notes: a console whose title starts with `Select ` silently drops injected input; `agents-cli send` can leave text unsent in the composer with `"enter": true` reported. | memory notes 2026-10-06 and 2026-10-08 |
| F7 | The Nightshift supervisor service `PythonNightshiftSupervisor` runs as **LocalSystem** (Session 0). The one running `WindowsTerminal.exe` (pid 14640) is in **Session 1**. `shift.py` already documents that `AttachConsole` cannot reach a worker from a scheduled-task process, and `supervisor.desktop_idle_seconds` explicitly refuses to read Session 0's input clock. The default console host is conhost (`HKCU\Console\%%Startup\DelegationConsole` = null GUID), which is why `launch.ps1`'s cmd windows are reachable by `AttachConsole`. | `sc qc`; `Get-Process WindowsTerminal`; `orchestration/shift.py`; `supervisor.py`; registry |
| F8 | The layout trial script enumerates visible top-level windows by title prefix and raises when none is found; it ran `wt.exe -w <fresh-name> --pos 0,0 --maximized new-tab ...`. The design records "access to its thread desktop was denied" for the pre-existing Terminal. | `C:/scripts/_oneshot_nightshift_terminal_grid.py`; design doc |
| F9 | Reusable Nightshift pieces exist: `orchestration/worktree.py` (lease/release, raises `WorktreeConflict` when a branch is checked out elsewhere, keeps dirty worktrees on kill), `locking.py` (kernel file lock, released on death), `shift_state.py` (JSON per goal, landed increments never re-run), `dispatch/dispatcher.py` (launches via `launch.ps1 ... -Nightshift`, stamps launch-info, harvest contract). State today is JSON files, not SQLite. | source files named |
| F10 | `shift.py`, `goal_shift.py`, `dispatcher.py` and `watchdog.py` still default `mcp_url` to `http://127.0.0.1:8876/mcp`, the tasks MCP retired on 2026-07-30. Notes written through those paths silently fail (`except Exception: pass`). `workbench.py` and `backlog.py` already use the tasks-cli path (`TasksMcpClient("")`). | grep `8876` in `src/nightshift` |
| F11 | `claude --help` exposes `--name`, `--resume <session-id>`, `--fork-session`, `--bg`, `--model`, `--permission-mode`, `--agent`. `launch.ps1` sets the input-bar name by injecting `/rename` because `--name` alone did not label the composer in the builds it was written against. | `claude --help`; `launch.ps1` comments |
| F12 | Repository guardrails: deploying the web surface must leave `NightshiftWorker` disabled; the owner-workbench action contract is blocking. The working tree is dirty with unrelated modified and untracked files; nothing here touches them. | `C:/git/nightshift/AGENTS.md`; `git status` |

---

## 1. Ranked findings

Severity: **Blocker** = the first live shift cannot be declared working without it. **High** = will
produce a wrong or duplicated outcome in normal use. **Medium** = design debt that bites at the
next step. **Low** = scope or taste.

### C1. Blocker: the native peer mechanism is the right channel, but registration is unreliable and permission-mode parity is a hard requirement

**Verified.** F1 shows the mechanism the design hopes for already exists: per-session named pipe,
name-addressed `SendMessage`, one-shot `notify_when_idle`, delivery to an idle session without
polling. It covers "wake an idle recipient", "do not poll an empty inbox" and "usable acknowledgement"
(an ack is just a reply message plus a mailbox write). F2 shows it cannot be assumed: three of the six
other live sessions on this machine are not in the mesh, including two started by the same launcher
the design intends to reuse. F3 shows a second trap: if Foreman and Coder run under different
permission modes, every bus message to the Coder is **held for human approval in the Coder pane**,
which the owner has said they will not operate. That silently converts "Foreman manages Coder" into
"human manages Coder".

**Failure mode.** Foreman calls `SendMessage` to `coder-1`; the name does not resolve (no
`<pid>.json`), or resolves to a stale same-named session from a previous shift ("latest wins"), or
delivers into a held-for-approval queue nobody drains. The assignment sits; the shift looks alive.

**Smallest correction.**
1. Make mesh registration a **readiness gate**, not an assumption: a member is ready only when
   `.claude/sessions/<agent-pid>.json` exists, carries `messagingSocketPath`, `sessionId`, and the
   expected `name`, and the peer appears in the other member's `ListAgents`. Missing after 60 s is a
   failed start, reported, never papered over by injection.
2. Shift-unique names: `ngh-<shift-id>-foreman`, `ngh-<shift-id>-coder-1`. Never a bare `foreman`.
3. Both members start with the **same `--permission-mode`**. Which mode is an owner choice (see
   reviewed design); the parity is not.
4. Record the member's `sessionId` from that file into the registry; it is also what `--resume` needs.

**Can the native mechanism support this arrangement?** Yes for the two-member star, conditional on
the spike below. Routing restrictions (Coder may only talk to Foreman) are **not** enforced by the
mesh; any registered session can message any other. Enforcement lives in the role brief and in the
mailbox CLI's refusal to record an unauthorised route, which is advisory under one OS account (the
design already concedes this).

**Spike that resolves it (G1/G2 in the reviewed design), all steps executable today:**
1. From an interactive Session-1 shell, start two `claude` processes with `--name ngh-spike-a` and
   `--name ngh-spike-b`, same model, same permission mode, in two conhost windows (WT is a separate
   gate).
2. Within 60 s assert both `.claude/sessions/<pid>.json` files exist with `messagingSocketPath` and
   the given names; record `sessionId`.
3. In A, run `ListAgents`; assert `ngh-spike-b` is listed. Repeat from B.
4. From A, `SendMessage(to: "ngh-spike-b", message: "NGH msg=m1 ... reply with ack m1",
   notify_when_idle: true)`. Assert B's transcript shows `<cross-session-message from="ngh-spike-a">`,
   B replies, A receives the reply and exactly one `[Cross-session idle notice]`.
5. Repeat step 4 while B is mid-turn (busy), while B has unsent draft text in its composer, and while
   B is sitting on a permission prompt. Pass criteria: message arrives after the turn / prompt is
   resolved by a human; draft text unchanged; nothing approved.
6. Start a third session with the **same** name `ngh-spike-b`; send again; record which one
   receives it. This fixes the naming rule.
7. Start B under a different `--permission-mode` than A; send; observe the hold-for-approval notice.
   This fixes the parity rule.
8. Kill B, `claude --resume <sessionId> --name ngh-spike-b`; assert a new `<pid>.json` with the same
   `sessionId`, and that A can address it again.
9. Repeat steps 1 to 4 three times across fresh launches. Any run where a `<pid>.json` fails to appear
   reproduces F2 and becomes a ticket with the launcher command line attached.

Bounded uncertainty: why LAP-042 and PROJ-025 did not register. Candidate causes are a client-version
difference at launch time, a write failure in the launcher race, or a mode the sessions were started
in. The spike's step 9 is the test; the design should not depend on the answer.

### C2. Blocker: the terminal activation failure is most likely a session/desktop mismatch, not a Windows Terminal defect

**Verified.** F7 and F8. The only Terminal process lives in Session 1. "Access to its thread desktop
was denied" is what `GetThreadDesktop`/`AttachThreadInput` returns from a different logon session or
window station. The Nightshift supervisor is LocalSystem in Session 0, and the repository already
knows Session 0 cannot touch the interactive console. Anything the service, or a worker the service
launched, starts with `wt.exe` will be created on an invisible desktop or fail activation, with a zero
exit code because `wt.exe` is a thin launcher that hands off and returns.

**Inference.** `wt.exe -w <new-name>` creates a window inside the *existing* Terminal instance; the
zero exit is expected regardless of whether a window appeared. The script's window enumeration is the
right verification and already fails loudly.

**Failure mode.** The sheet is opened by the wrong principal; nothing visible; members start in a
desktop the human cannot see; the launcher reports success.

**Smallest correction.** The component that opens the sheet must run in the human's interactive
session: `ProcessIdToSessionId(self) == WTSGetActiveConsoleSessionId()` and the thread desktop is
`Default`. Print both at the top of every launcher run and refuse otherwise. The LocalSystem supervisor
is not allowed to be the sheet launcher; `nightshift up` is run by the human (or by a per-user
logon-session task), and the supervisor at most observes.

**Spike (G0):**
1. From a cmd window the human opened: run the existing trial reduced to one `new-tab` plus one
   `split-pane -V -s 0.5`, both panes running the placeholder. Expect a visible window and two
   handles.
2. Run the same command from `psexec -s`/the service context. Expect the F8 failure. This confirms
   the cause with one positive and one negative result.
3. In the visible panes, run the placeholder's status prompt to confirm both are interactive, then
   `agents-cli tail <placeholder-pid>` from a third window to confirm `AttachConsole` reads a WT pane's
   pseudoconsole buffer. If tail returns the pane text, diagnosis-by-snapshot survives the move from
   conhost to Windows Terminal; if not, Foreman diagnosis falls back to transcripts, which the
   repository already does for stall detection.

### C3. Blocker: pane and member identity must be bound by process lineage at launch, not discovered afterwards

**Verified.** Windows Terminal exposes no pane identifier to the processes inside it; F8's script only
knows the window by title. `agents-cli` resolves a session to its console-owning root by walking the
parent chain (`procs.go` `rootOf`); under WT the root is the pane's shell, whose parent is
`WindowsTerminal.exe`. `launch.ps1` finds the Claude pid by walking descendants of the cmd it started
(F11). Titles are mutable (`CLAUDE_CODE_DISABLE_TERMINAL_TITLE` is set here; the launcher's spinner
glyph already breaks exact title matches elsewhere).

**Smallest correction.** Each pane's command line is a **bootstrap stub**, not `claude` directly:
`cmd /k nightshift member-start --shift <id> --member coder-1`. The stub records its own pid and start
time against the member, sets `NIGHTSHIFT_SHIFT`, `NIGHTSHIFT_MEMBER` and a per-member token in its
environment, then execs `claude --name ngh-<shift>-coder-1 --model claude-opus-5-5 ...`. Readiness is
then: a descendant of that pid has a `.claude/sessions/<pid>.json`. No title matching anywhere. The
env block also answers the design's open question "how does the launched process receive its member
credentials" (see C5).

### C4. High: the fallback ladder is wrong; injection cannot be the fallback for Claude today

**Verified.** F4, F5, F6. The design says console injection is "a fallback only after tests
demonstrate prompt detection, draft preservation and busy-state behaviour". Those tests would fail
today: the installed heuristic cannot tell an idle Claude from an unknown screen, `send` can report
success with text left unsent, and Select mode eats input silently. `send` also refuses
Claude-to-Claude by design.

**In plain terms (added 15 October after owner question).** Injection means `agents-cli` typing
keystrokes into the Coder's console input buffer, the same mechanism `launch.ps1` uses to send
`/rename`. It is only safe when the sender knows the Coder is idle: not mid-turn, not holding
unsent draft text, not sitting on a y/n approval prompt. `agents-cli` decides that by running regexes
over the screen text, and today those regexes return `unknown` for every idle Claude session on this
machine, because the current Claude prompt box does not render the bare `>` line they look for. So the
tool cannot distinguish "idle" from "waiting on Approve?", and an injected Enter could approve
something. Together with the two recorded quirks (text left unsent while `send` reports success;
Select mode silently swallowing input) this makes injection blind typing, not a fallback. It remains a
useful tool in the human's own hands for diagnosis.

**Failure mode.** A "retry" through injection types an assignment into a composer holding a draft, or
presses Enter on a permission prompt.

**Smallest correction.** Two channels only: native `SendMessage` for delivery; **hold and escalate**
when it fails (Foreman tells the human which member is unreachable and why). Injection stays a
human-invoked diagnostic (`agents-cli` from the human's own shell), never a runtime path. Delete the
"evaluate a hook/adapter path" branch until the spike has failed; it is speculative machinery.

### C5. High: member authentication to the bus is cheap if the launcher is the runtime

**Verified.** F1: every process under a Claude session inherits `CLAUDE_CODE_SESSION_ID` and
`CLAUDE_PID`. **Inference.** A per-member random token placed in the pane's environment by the stub
(C3) is inherited by the Claude process and by every Bash-tool subprocess it spawns, so `nightshift
msg send` can present `(member, token, CLAUDE_CODE_SESSION_ID)` and the mailbox can check all three
against the registry. Same OS account means a determined peer can read another pane's environment;
the design already states strong isolation is out of scope. This closes the "arbitrary sender label"
hole for the honest-but-confused case, which is the one that actually happens.

### C6. High: duplicate effects need a message header and an idempotent ack, not more runtime states

**Verified.** F3: a resend through `SendMessage` is a second message; nothing deduplicates on the
receiving side. **Inference.** The design's four delivery states are fine as mailbox columns, but the
actual protection is textual: every bus message body begins with one machine-readable line
`NGH shift=<id> msg=<id> kind=<kind> asg=<id> inc=<n>`, and the Coder's role brief says: on receipt,
run `nightshift ack <msg-id>`; if the CLI reports "already acknowledged", stop. Acceptance is keyed by
`(assignment-id, incarnation)`; a result from an older incarnation is stored and flagged, never
accepted. The acceptance test in the design (step 6) stays.

### C7. High: closing the Windows Terminal window will terminate the members; design `join` and `resume` for that, not against it

**Inference, test required.** Closing a WT pane closes its pseudoconsole; the Win32 contract for
`ClosePseudoConsole` is that attached clients are terminated. Expect Claude to die with the pane.
`join` as "restore visibility without a new incarnation" is therefore only possible while the original
pane is alive; a dead pane cannot be re-attached to a new one on Windows.

**Smallest correction.** Define `join` as "open a read-only status pane plus focus the existing
window" and `resume` as "new panes running `claude --resume <sessionId>` per member, new incarnation,
same session id, reconciled from the mailbox". Test: close the window during an assignment, observe
process death, run `resume`, confirm the Coder's first action is `nightshift inbox` rather than
trusting its memory.

### C8. Medium: the runtime is over-built for two members on one box

**Verified.** F9: state is JSON today; one kernel lock helper exists; no pipe server exists. The design
proposes five components, a resident runtime, a named pipe, SQLite, seven message kinds and a build
runner before one assignment has crossed the bus.

**What is unnecessary for the MVP.**
- The **named pipe** and the **resident runtime process**: the sender's own session performs
  delivery (`SendMessage`); the mailbox only needs a serialised store. SQLite in WAL mode with a
  short busy timeout serialises concurrent CLI writers without a daemon. Keep the pipe as a later
  option for Copilot/Codex adapters that cannot call a CLI.
- **Seven kinds**: four suffice (`assignment`, `ack`, `result`, `note`); `question`, `correction`
  and `lifecycle` are notes with a flag until a consumer needs them.
- **`hold`** and **`join`** commands: `up`, `status`, `down`, `resume` carry the first release.
- **Build runner as a component**: one heavy-build slot is a lock around a recipe command plus a
  result row keyed by revision (C10). Fifty lines, not a service.

**Owner response, 15 October:** agreed, no resident runtime. Added constraint: **nothing can be
installed as a Windows service at work**, which also rules out any design element that leans on the
home `PythonNightshiftSupervisor` service or an installer-registered task.

**Smallest runtime that satisfies the owner's decisions:** `nightshift` CLI + SQLite file per shift +
launcher stub + role briefs. Mutations go through the CLI; the CLI takes `locking.exclusive` only for
launch and resume (process-creating operations), and lets SQLite serialise the rest.

### C9. Medium: worktree ownership should reuse the lease model and pin the base commit

**Verified.** F9: `worktree.lease` already enforces one checkout per branch and refuses to serve the
wrong tree. **Inference.** The design's "replacement Coder resumes the same workspace" and "never reset
the user's checkout" map directly onto `lease`/`release`. Two additions: the assignment records
`base_commit` and the worktree is created from that commit (`git worktree add -b ngh/<shift>/<asg>
<path> <base>`), never from whatever the dirty shared checkout has; Foreman reads the lease with
`git -C <lease> diff <base>..HEAD` and `git status --porcelain`, and never runs tools that write there.

### C10. Medium: build isolation is right in principle; make cache validity a recipe declaration and make cancellation a Job object

**Inference.** The design asks for revision-bound evidence and a warm workspace. Concretely: one
dedicated build worktree per repository (`<worktrees_dir>/_build`), occupied under a lock; the runner
asserts the tree is clean, `git checkout --detach <sha>`, records `(sha, recipe-hash, toolchain
version)` and shares results on that triple. Whether incremental outputs survive a revision switch is
toolchain-specific; the recipe declares `cache: keep | clean` and the runner does nothing cleverer.
Cancellation: start the recipe in a Windows Job object with `KILL_ON_JOB_CLOSE`; closing the handle
kills the whole tree, which answers "cancellation must account for child processes" without a process
walk. A `running` row with pid and start time that no longer matches a live process becomes
`interrupted` on reconciliation, never `passed`.

### C11. Medium: `resume` depends on `--resume`, which is verified to exist, and on name persistence, which is not

**Verified.** F11: `--resume <session-id>` and `--fork-session` exist; F1 shows `name` is stored in the
per-pid registry with `nameSource: user`. **Unverified:** whether a resumed session keeps its mesh name
without `--name`/`/rename`. Test in C1 step 8. If names do not persist, the stub always passes
`--name` and the readiness gate checks it.

### C12. Medium: packaging must not inherit the dead tasks-MCP default or the workstation-only tools

**Verified.** F10: four modules default to the retired `:8876` endpoint and swallow the failure. F4:
`agents-cli` is a Go binary with no recorded commit; F12: the package must keep unattended dispatch
disabled on deploy. **Inference.** A portable package that reuses `shift.py` or `dispatcher.py`
carries a silent dead dependency to PIMCO. Ticket notes go through `tasks-cli` only, or the setting is
removed; a missing writer must fail loudly in `nightshift up --check`. `agents-cli`, `launch.ps1`,
`tasks-cli` are **workstation conveniences**, not package dependencies: the core loop needs `claude`,
`git`, `wt.exe` and the package itself. `wt.exe` is an app-execution alias that a locked-down build
may not expose; `up --check` resolves it and fails with the path it tried.

### C13. Low: authority is correctly modest; keep the two sentences that matter

**Inference.** "Foreman accepts on evidence and reports that independent review is not staffed" and
"model quality cannot grant authority" are the load-bearing sentences. The CLI refuses `assign`,
`steer` and `stop` from a non-Foreman token and logs the attempt; that is advisory enforcement under
one account and should be labelled as such in the handover, as the design already does.

### C14. Low: MVP scope, reordered as gates

The nine-step acceptance sequence mixes go/no-go capability tests with product behaviour. Order them
as gates so a failure stops spend early: G0 terminal (C2), G1 identity and registration (C1, C3), G2
delivery semantics (C1 steps 4 to 8), G3 one assignment through the mailbox with ack and result (C6),
G4 build slot and evidence (C10), G5 stop/resume and handover (C7, C11). Steps 7 to 9 of the original
sequence are G4/G5 and can follow a first live shift that only reaches G3.

### C15. Low: the Advisor role is worth having; two independent advisors are worth having only on a trigger

**Inference.** A single frontier Advisor earns its spend when the question is a design fork with
artefacts attached and a bounded ask, as this consultation was. A second independent advisor adds
information only when the two disagree, and the design's protocol (same evidence, critiques before
sharing, Foreman adjudicates on evidence) is the right way to harvest that disagreement. As a default
it doubles cost and produces agreement that proves nothing, which the design itself notes. Recommend:
one Advisor by default; a second only when (a) the first Advisor and Foreman disagree on a decision
that is hard to reverse (schema, publish, deploy, authority), or (b) the owner asks. Advisors receive
artefacts and a question, never the live conversation, and never dispatch authority. Keep the per-call
spend and time bounds.

---

## 2. Unnecessary machinery, in one list

Named pipe server; resident runtime process; `hold`; `join` as re-attach; seven message kinds;
hook/adapter delivery branch; build runner as a component; Controller and DevOps as anything other
than briefs for later; the eight-pane layout before two panes work; the Rust `agents-cli` port as a
dependency (it is unverified on Windows).

## 3. What the design gets right and should keep verbatim

Mailbox is authoritative, terminal text is not. Exactly-once is not promised. Member, role, model,
CLI and account are separate fields. Foreman is not duplicated by a manager above it. Dirty
repositories are preserved, never reset. A spawned process is not a ready member. A permission prompt
is awaiting a human, never injected into. Forced termination is explicit. Unknown usage is labelled
unknown. One Fable consultation, no recursive rounds.

## 4. Bounded uncertainties and the test for each

| Uncertainty | Test |
| --- | --- |
| Why some launcher-started sessions never write `<pid>.json` (F2) | C1 step 9, three fresh launches with the command line captured |
| Whether a cross-session message to an idle session starts a turn without human input | C1 step 4 with B idle |
| Whether `--resume` preserves the mesh name | C1 step 8 |
| Whether `AttachConsole` reads a WT pane's pseudoconsole | C2 step 3 |
| Whether closing the WT window terminates the members | C7 test |
| Whether `wt.exe` is reachable at PIMCO | `nightshift up --check` at PIMCO, reported, not assumed |

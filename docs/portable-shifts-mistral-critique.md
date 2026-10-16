# Mistral Large 4 critique of the Nightshift portable shifts reviewed revision

Second-model review of `docs/portable-shifts-design-reviewed.md` (the Fable revision of
`docs/portable-shifts-design.md`), written 15 October 2026 from session NGH-003. One pass, no
implementation. This is a review of an existing revision, not a blinded independent comparison:
where Fable's critique (`docs/portable-shifts-fable-critique.md`, items C1 to C15) already
established a fact, I re-verified what I could from source and live state rather than
re-deriving it, and I say so.

**Model attestation.** This session's harness declares the model as Mistral Large 4
(`mistral/mistral-large-4`) running under OpenCode. `agents-cli ls` lists this session
(pid 30740, agent pid 30192) as `tool: opencode`, name
`NGH-003-review-portable-shift-design-with-mistral-large-4`, state `busy`. No `AI_AGENT`
environment variable is set in the shell and there is no API-level receipt available from
inside a session, so the attestation is the harness declaration plus the session listing,
nothing stronger. No substitution was observed.

**How to read this.** Every claim is tagged **[VERIFIED-ME]** (read from source, a running
process, a file on disk, or a tool contract in this session, with the location named),
**[FABLE CLAIM]** (asserted by the Fable critique; consistent with my checks where I could
make them, but not re-derived by me), or **[INFERENCE]** (design reasoning or platform
knowledge not re-tested here). The `SendMessage`/`ListAgents` tool contract is **not
available in this session**, so Fable's F3 is treated throughout as a previous-review claim;
nothing below claims that contract was personally tested. Where something could not be
verified, an executable acceptance test is given instead of a guess.

---

## 0. Facts verified in this session

| # | Fact | Source |
| --- | --- | --- |
| V1 | The per-session mesh registry exists and has the claimed shape. `%USERPROFILE%\.claude\sessions\9744.json` contains `pid`, `sessionId`, `peerProtocol: 1`, `peerFeatures: ["notify_idle","artifact_yield"]`, `kind`, `pidDomain: "win32:<host>"`, `messagingSocketPath: "\\.\pipe\LOCAL\cc-msg-<hash>"`, `name`, `status`. One nit against F1: the field is `nameSince`, not `nameSource`; the file also carries `version`, `startedAt`, `procStart`, `cwd`, `entrypoint`, `jobId`, `updatedAt`. The readiness gate should check `name` (present) and not gate on `nameSource`. | `.claude/sessions/9744.json` read 15 Oct |
| V2 | Registration is unreliable in the way F2 claims. The sessions directory holds `.json` files for pids 12692, 17516, 9744 but only `.key` files for 16344, 17492, 29392. `agents-cli ls` shows LAP-042 with `agent_pid` 29392 and PROJ-025 with `agent_pid` 17492 — both key-only, both `alive: true`, both started by the launcher path F2 describes. | `.claude/sessions` listing; `agents-cli ls` |
| V3 | `agents-cli ls` reports `state: "unknown"` for **every** Claude session right now (LAP-042, PROJ-025, M1, M2, M3, and the bg session 9744); only the non-Claude sessions classify (`idle`/`busy`). `can_native` is `false` for every session. This re-confirms F5 and the `can_native` half of F4. | `agents-cli ls` |
| V4 | `PythonNightshiftSupervisor` exists, `START_TYPE: AUTO_START`, `SERVICE_START_NAME: LocalSystem`. `PythonNightshiftWatchdog` does **not** exist as an installed service — consistent with `daemon.py`'s docstring that the old watchdog service is retired in favour of the in-session daemon. Neither `supervisor.py` nor `supervisor_service.py` references `wt.exe`, `WindowsTerminal`, `new-tab` or `split-pane`: the LocalSystem supervisor does not open terminal sheets today, so the reviewed design's rule ("the LocalSystem supervisor service never opens the sheet") matches current reality and should be kept as a rule. | `sc qc PythonNightshiftSupervisor`; `sc qc PythonNightshiftWatchdog`; grep |
| V5 | Exactly one `WindowsTerminal.exe` (pid 14640) is running, in Session 1, Console session, user `DESKTOP-OQNDSJ5\admin`. Consistent with F7. | `tasklist /v /fi "imagename eq windowsterminal.exe"` |
| V6 | The layout trial script exists as F8 describes: `C:/scripts/_oneshot_nightshift_terminal_grid.py` builds `wt.exe -w <name> --pos 0,0 --maximized new-tab ...`, runs it with `check=True`, then enumerates visible top-level windows by the `Nightshift |` title prefix and raises `SystemExit` when none is found. A zero exit code from `wt.exe` is therefore expected even when no window appears. | script read 15 Oct |
| V7 | `claude --help` exposes `-n, --name <name>` ("shown in the prompt box, /resume picker, and terminal title"), `-r, --resume [value]`, `--fork-session`, `--bg`, `--model`, `--permission-mode <mode>` with choices `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan`, `--agent`, `-w, --worktree`. Note: **`default` is not a `--permission-mode` choice** — the default mode is what you get by omitting the flag. `claude auth status` exists as a subcommand. | `claude --help`; `claude auth --help` |
| V8 | The retired tasks-MCP default `http://127.0.0.1:8876/mcp` is still present in `orchestration/shift.py` (lines 125, 162), `orchestration/goal_shift.py` (line 158), `dispatch/dispatcher.py` (line 323) and `watchdog/watchdog.py` (line 51), and the note/demote paths in `shift.py` swallow the failure with `except Exception: pass`. **Fable's F10 missed a sixth instance:** `console/usage.py` line 14 hardcodes `URL = 'http://<lan-host>:8876/usage'` — a personal LAN address in package source, which is both a dead endpoint and a portability/leakage problem. | grep `8876` in `src/nightshift` |
| V9 | `workbench.py` and `backlog.py` already use the tasks-cli path (`TasksMcpClient("")`), and `workbench.py` carries the blocking `owner_actions` policy plus `owner_workbench_contract_problems` (`dispatch-fast`/`dispatch-slow` must exist for `unassessed` and `ready`) — the contract `C:/git/nightshift/AGENTS.md` declares blocking. | grep; `workbench.py` lines 51-85 |
| V10 | `orchestration/worktree.py` `lease(repo, branch, dest)` adds an **existing** branch: `git worktree add <dest> <branch>`. It has no `-b` and no base-commit parameter, so it cannot create `ngh/<shift>/<asg>` from a recorded base commit as the reviewed design requires. The conflict semantics (one checkout per branch, `WorktreeConflict` naming the offending path, dirty leases kept on kill via `release` never being called on kill/retry) are exactly as F9/C9 describe. | `worktree.py` read 15 Oct |
| V11 | `locking.py` `exclusive(path)` is a **non-blocking** kernel lock (`msvcrt.LK_NBLCK` on Windows, yields `False` on contention) that the OS releases on process death. It does not queue waiters. | `locking.py` read 15 Oct |
| V12 | `orchestration/shift_state.py` is reusable as claimed for resume semantics: `LANDED = {verified, done}` are never re-run, `HALTING` states block the sequence, `next_actionable` never leaps a running/halting increment. State is one JSON file per goal. | `shift_state.py` read 15 Oct |
| V13 | The package is a src-layout Python package: `pyproject.toml` `name = "nightshift"`, `requires-python = ">=3.11"`, core `dependencies = []` (`psutil` and `pywin32` are optional extras). A Python CLI inside the package adds no third-party dependency; `sqlite3` is stdlib. | `pyproject.toml` read 15 Oct |
| V14 | `launch.ps1` resolves the session id from `%USERPROFILE%\.claude\sessions` (line 927) and writes `SessionId = ''` into launch-info when it cannot (lines 384, 1349, 1381) — the F2 mechanism for launcher-started sessions with no `<pid>.json`. Its own comment records that "reboot wipes the volatile .claude/sessions/*.json files", so the shift's own registry must carry `sessionId` across reboots (the reviewed design does record it). | `C:/scripts/launch.ps1` grep/read |
| V15 | `daemon.py`'s docstring documents that console revival (`AttachConsole`) and worker-window launch cannot cross session 0, and that the daemon must run in-session. `orchestration/shift.py` comments agree ("AttachConsole can't reach the worker from a scheduled-task process"). | `daemon.py` lines 1-22; `shift.py` lines 96-98, 298-315 |
| V16 | `dispatch/dispatcher.py` launches through a configurable `launcher_path` via a single atomic `powershell -File` call, waits for `<slug>.launch-info.json`, post-stamps it managed (`stamp_managed`), and records the harvest contract — the F9 description, with the launcher configurable rather than hardwired to `launch.ps1`. | `dispatcher.py` lines 1-12, 75-97, 153-161, 297-330 |
| V17 | `wt.exe` resolves to `%USERPROFILE%\AppData\Local\Microsoft\WindowsApps\wt.exe` — an app-execution alias, exactly the kind of path a locked-down work machine may not expose. The design's `up --check` resolve-and-fail-loudly rule is the right answer. | `where wt` |
| V18 | The Fable quirks are already filed in memory store (wing `tasks`, room `tech`, source `NGH-001-review`, 2026-10-15): agents-cli `guessState` returns `unknown` for every idle Claude session; the supervisor is LocalSystem in Session 0 while the only Terminal is in Session 1. My V3/V4/V5 re-verifications are consistent with that filed record. | `memory store_search` 15 Oct |

---

## 1. Ranked findings

Severity: **Blocker** = the first live shift cannot be declared working without it. **High** =
will produce a wrong, duplicated or silently stalled outcome in normal use. **Medium** = design
debt that bites at the next step. **Low** = scope or taste.

### M1. High: removing the resident runtime removed two liveness functions; nothing replaces them yet

**Verified-ME (V11) + INFERENCE.** With no resident process, the design's two liveness
mechanisms are both member-driven: the sender's own session delivers immediately after the
mailbox write, and the build slot is a lock plus a wrapper. That is the right shape for the
owner's no-service constraint — but two failure windows have no recovery owner:

1. **Stored-not-sent messages.** The send path is: mailbox commit (`stored`), then
   `SendMessage`, then record `sent`. If the sender's session dies between commit and send —
   or `SendMessage` fails and the sender never retries — the message sits `stored` forever.
   The reviewed design's recovery is recipient-side (`resume` runs `nightshift inbox`, which
   trusts the mailbox), and the failure ladder covers *recipient* unreachable. It does not
   say who re-delivers a message whose *sender* died. With nobody polling an inbox and no
   daemon, the Coder never learns the assignment exists; the shift looks alive and idle.
2. **Queued builds.** "One heavy-build slot" with `locking.exclusive` (non-blocking, V11)
   means a second build request gets `False` immediately. The design records queued jobs and
   says a newer commit may supersede a queued one, but with no resident process nothing starts
   the queued job when the slot frees — unless a member happens to run a CLI command.

**Failure mode.** Foreman commits an assignment to the mailbox and dies (pane closed, rate
limit, host sleep) before `SendMessage` returns. The Coder sits idle; `status` from the
human's shell shows the shift up with an assignment nobody is working. Separately, a second
heavy build queued behind a running one never starts after the first finishes.

**Smallest correction.** Keep the no-resident-runtime decision (it is an owner constraint,
not a gap) and make liveness a property of CLI invocations:

1. `nightshift inbox` reconciles in **both** directions: list messages addressed to me not
   yet acknowledged, **and** re-attempt native delivery of my own `stored`-not-`sent`
   messages (the mailbox row already exists, so re-delivery needs no new auth decision).
   Every member runs it first on start and resume, as the revision already specifies.
2. `nightshift status` prints the stored-not-sent set explicitly, so the human's shell sees
   the stall without opening SQLite.
3. The build wrapper, on completion, **pumps the queue**: before exiting, it attempts to start
   the next due queued job if the slot is free. Liveness then comes from member activity,
   not from a daemon.

**Agreement with Fable.** Fable's C8 (no resident runtime) is correct and I would not
restore a daemon. This finding is the gap the removal leaves, not a reason to reverse it.
**Evidence that settles it.** G5 with a staged stored-not-sent message (write via CLI,
suppress delivery, resume, observe re-delivery); G4 with two queued builds and one running.

### M2. High: Foreman-only routing is advisory; make a violation detectable, not just refused

**FABLE CLAIM (F1, F3) + INFERENCE.** The native mesh does not restrict routes: any
registered session can `SendMessage` any other registered session by name. The reviewed
design concedes this and relies on the role brief plus the mailbox CLI refusing to *record*
an unauthorised route. But native delivery bypasses the mailbox entirely: a Coder that
messages another peer directly (or a stray same-named session that resolves "latest wins")
never touches the mailbox, so the violation is invisible in the shift record.

**Failure mode.** A misbehaving or confused Coder sends a fabricated `assignment` straight
to another member (or to a stale same-named session from a previous shift). The recipient
acts on it; the mailbox has no row; the shift record shows nothing; Foreman's tracking is
blind to the work.

**Smallest correction.** The header line already carries `msg=<id>`. Turn it into a
recipient-side check: on receipt, the member runs `nightshift ack <msg-id>` **before acting**;
the CLI answers `unknown message` for any id with no mailbox row, and the role brief says:
an unknown id means the message did not come through the mailbox — do not act, send a
`note` to Foreman reporting the protocol violation. This converts "advisory enforcement"
into "detectable enforcement": the violation surfaces in the mailbox instead of passing
silently. It is still not strong isolation (one OS account, shell access), and the handover
should keep saying so.

**Agreement with Fable.** Fable's C13 labels the enforcement advisory and keeps two
load-bearing sentences; I agree and add one mechanism that makes violations visible without
adding isolation machinery. **Evidence that settles it.** G2 addition: `SendMessage` with a
fabricated header (`msg=<nonexistent>`) to the Coder; pass criterion is the Coder does not
act and the violation note exists in the mailbox.

### M3. Medium: "asserts the tree is clean" contradicts a warm build workspace

**INFERENCE from the reviewed design text + V10/V11.** The build wrapper "asserts the tree
is clean, checks out the exact commit detached". A warm workspace kept for incremental
outputs (`cache: keep`) is by definition not clean: untracked `bin/`/`obj/` outputs make
`git status --porcelain` non-empty. So either the assert fires on every warm reuse (defeating
the cache), or someone weakens it ad hoc (defeating the evidence). The design means "no
*tracked* modifications from a previous revision", which is a different and checkable
condition.

**Failure mode.** First build on a fresh worktree passes; the second build on the warm
worktree fails the cleanliness assert (or, if the assert is loosened to "no output", a
half-applied previous revision silently contaminates the next build's evidence).

**Smallest correction.** Pin the semantics in the recipe schema and the wrapper:

- The wrapper asserts **no tracked modifications**: `git diff --quiet` and
  `git diff --cached --quiet` (plus `git status --porcelain` restricted to tracked files).
  Untracked build outputs are allowed when `cache: keep`.
- When `cache: clean`, the wrapper runs the recipe-declared clean step (or `git clean -fdx`
  on the build worktree) before checkout.
- The result row records which semantics ran, alongside the `(commit, recipe hash,
  toolchain version)` triple.

**Agreement with Fable.** Fable's C10 (recipe-declared cache validity, "the runner does
nothing cleverer") is the right principle; this pins what "clean" means so the wrapper stays
dumb. **Evidence that settles it.** G4's warm-workspace reuse step, run twice in a row.

### M4. Medium: `worktree.lease` cannot create a branch from a base commit as the design assumes

**Verified-ME (V10).** The reviewed design says the assignment worktree is "created from the
recorded base commit" (`git worktree add -b ngh/<shift>/<asg> <path> <base>`) and that "the
existing `orchestration/worktree.py` lease semantics apply". The semantics apply; the
function does not: `lease(repo, branch, dest)` runs `git worktree add <dest> <branch>` for
an **existing** branch and has no base parameter. Used as-is, it would add a branch that
does not exist yet and fail, or worse, serve a stale same-named branch.

**Failure mode.** First assignment dispatch fails with a git error, or — if someone
"simplifies" by creating the branch in the shared checkout first — the lease collides with
the dirty user checkout the design promises to preserve.

**Smallest correction.** Extend `lease` with an optional `base` parameter:
`git worktree add -b <branch> <dest> <base>` when `base` is given, keeping the existing
conflict reconciliation (`WorktreeConflict` naming the offending path) unchanged. Small,
local, and it keeps the "never from the dirty shared checkout" guarantee.

**Agreement with Fable.** C9 is right about the semantics and the reuse; this is a signature
gap, not a design disagreement. **Evidence that settles it.** None needed beyond V10; G3
exercises it end to end.

### M5. Medium: the dependency list omits the CLI's own interpreter, and path resolution is not authentication

**Verified-ME (V13, V17) + INFERENCE.** The reviewed design's core loop is "`claude`, `git`,
`wt.exe`, the package". But the default CLI is Python inside the package (V13:
`requires-python = ">=3.11"`, core `dependencies = []`), so a fifth dependency exists:
**a Python 3.11+ interpreter** on the work machine. On a locked-down machine an unmanaged
Python may not be on PATH. Separately, "resolved to a path" is necessary but not
sufficient: `claude` can resolve while unauthenticated, and the first failure would then
appear mid-`up`.

**Failure mode.** `up --check` passes at PIMCO (all four paths resolve), `up` opens the
sheet, and the stub's `claude` fails on first API call for want of credentials — after the
window exists and the shift is half-up.

**Smallest correction.** `up --check` additionally:

1. resolves the package interpreter (`python` >= 3.11) when the CLI is Python — or the
   spike ships a frozen binary and this line disappears (the G1 language decision already
   owns that fork);
2. runs `claude auth status` (V7: the subcommand exists) and fails loudly when it reports
   unauthenticated;
3. resolves `wt.exe` to a path and reports the alias path it tried (already specified —
   V17 confirms it is an app-execution alias).

**Agreement with Fable.** C12's dependency reduction is right; the list was just incomplete.
**Evidence that settles it.** `nightshift up --check` run at PIMCO (a G0 prerequisite).

### M6. Low: the manifest's `permission_mode` needs an explicit `default` value that maps to omitting the flag

**Verified-ME (V7).** `--permission-mode` choices are `acceptEdits, auto,
bypassPermissions, manual, dontAsk, plan`. `default` is not a choice; the default mode is
what a session gets with no flag. The reviewed design makes the manifest field mandatory
and `up --check` refuses a mismatch — correct — but the manifest example shows
`permission_mode: <owner choice>` without naming the values.

**Failure mode.** The stub passes `--permission-mode default` verbatim; the CLI rejects the
value; both members fail to start; or worse, one member starts and the other does not,
which is exactly the parity violation the design exists to prevent.

**Smallest correction.** Manifest enum: `default | acceptEdits | auto | bypassPermissions |
manual | dontAsk | plan`. The stub omits `--permission-mode` for `default`. Parity check
unchanged. **Evidence that settles it.** V7 (`claude --help`); G1 exercises both members
under one value.

### M7. Low: the dead-endpoint hygiene extends past F10 — `console/usage.py` hardcodes a personal LAN address

**Verified-ME (V8).** F10 lists four modules defaulting to the retired
`http://127.0.0.1:8876/mcp`. There is a sixth instance Fable missed:
`console/usage.py` line 14, `URL = 'http://<lan-host>:8876/usage'` — a hardcoded personal
LAN address in package source. It is a cached server-side usage feed (a workstation
convenience), so it is not a core-loop dependency, but it is both a dead endpoint and a
portability/leakage problem if the package travels to work.

**Failure mode.** The package ships to PIMCO with a home-LAN address baked in; the usage
feed silently fails there (same silent-failure shape as F10), and the source carries a
personal network address.

**Smallest correction.** Same rule as C12: the portable package must not inherit it —
behind config with no default, or deleted from the packaged surface. **Evidence that
settles it.** V8 grep; packaging review at G0.

### M8. Low: G5 proves accepted work is not repeated; it should also prove acked-but-unfinished work is re-dispatched

**INFERENCE.** The reviewed design's incarnation fencing keys acceptance by `(assignment,
incarnation)` and says accepted work is never replayed. The complement is untested: an
assignment the Coder **acked** but had not submitted when it died. On resume, the
assignment sits in `assigned`/`working` under a dead incarnation. The design says a
replacement Coder resumes the same lease and reads its inbox first — which is the right
mechanism — but no gate proves the assignment is actually re-dispatched to the new
incarnation rather than stranded.

**Failure mode.** Coder acks, starts work, pane dies. Resume brings up a new Coder
incarnation, which reads its inbox, sees the assignment row, and (per a reasonable role
brief) waits for a dispatch that never comes because Foreman believes the assignment is
already assigned. The shift stalls with everyone alive.

**Smallest correction.** Add to G5: kill the Coder after ack, before submission; resume;
pass criterion is the assignment re-dispatches to the new incarnation (new assignment
message with `inc=<n+1>`, Coder continues the same lease, no duplicate acceptance).
**Evidence that settles it.** G5 as extended.

---

## 2. Agreement and disagreement with Fable, in one view

**Agreements (adopted unchanged):** C1 (native `SendMessage` as the only delivery channel;
registration as a readiness gate; shift-unique names; permission-mode parity as a hard
requirement), C2 (launch principal is the human's interactive session; window verified by
enumeration, not exit code), C3 (pane/member binding by process lineage via the stub, never
titles), C4 (no injection in the runtime; hold-and-escalate), C6 (header line + idempotent
ack + acceptance keyed by assignment and incarnation), C7 (`join` as a status view; `resume`
assumes members die with the window), C8 (no resident runtime, no named pipe, four message
kinds, `up`/`status`/`down`/`resume`), C10 (Job-object cancellation; crash reconciles to
`interrupted`, never `passed`), C11 (`--resume` verified to exist, name persistence tested
not assumed), C14 (gates G0-G5, first live shift after G3), C15 (one Advisor default; second
on a disagreement trigger or owner request).

**Nuances and additions (this review):**

| Fable item | This review |
| --- | --- |
| C8 no resident runtime | Agreed; but the removal leaves two liveness functions (delivery retry, queue pumping) that must be re-homed onto CLI invocations — M1 |
| C9 lease semantics | Agreed on semantics; `lease()` needs a base-commit parameter — M4 |
| C10 build slot | Agreed on principle; "tree is clean" must mean "no tracked modifications" for warm caches — M3 |
| C12 dependencies | Agreed; the list omits the CLI's own Python interpreter, and path resolution is not authentication — M5; plus `console/usage.py` hardcodes a personal LAN address — M7 |
| C13 advisory enforcement | Agreed it stays advisory; added a recipient-side unknown-id check so violations are detectable in the mailbox — M2 |
| C14 gates | Agreed; G2 gains the fabricated-header test, G4 pins cleanliness semantics, G5 gains the acked-but-unfinished re-dispatch test — M2, M3, M8 |
| F1 field names | The registry file uses `nameSince`, not `nameSource`; gate on `name` — V1 |

**Disagreements:** none that reverse a Fable decision. The closest is M1, which could be
read as "C8 was incomplete" — I read it as "C8 was right, and the revision did not yet
specify what replaces the daemon's liveness".

---

## 3. Go/no-go and first executable spike

**Recommendation: GO for the spike sequence, with M1 and M2 folded in first.** The revised
architecture is the right shape for the owner's constraints: native delivery, a mailbox as
the authoritative record, lineage-bound identity, no resident runtime, no services, gates
before spend. The genuine blockers are empirical, not architectural — mesh registration
reliability (V2), idle-wake behaviour, terminal activation causality (V4/V5/V6) — and the
gates G0-G2 are the correct instrument to settle them. **NO-GO for declaring a first live
shift** until G2 passes and the two owner choices are made: the permission mode for both
members (M6 makes the manifest values explicit), and whether Foreman may read
`agents-cli tail` snapshots of the Coder pane at all (V3 shows the state heuristic is
useless for Claude today, so snapshot diagnosis is screen-text only, and only if G0 shows
`AttachConsole` reads a WT pane's pseudoconsole).

**First executable spike (G0, reduced to the two-pane requirement), all steps runnable
today from an interactive Session-1 shell:**

1. From a cmd window the human opened, run the existing trial reduced to one `new-tab`
   plus one `split-pane -H -s 0.5`, both panes running
   `python C:/scripts/_oneshot_nightshift_terminal_grid.py --pane <role>`:
   `wt.exe -w ngh-g0 --pos 0,0 --maximized new-tab --title "Nightshift | Foreman" --suppressApplicationTitle -d <fixture> python C:/scripts/_oneshot_nightshift_terminal_grid.py --pane Foreman ; split-pane -H -s 0.5 --title "Nightshift | Coder" --suppressApplicationTitle -d <fixture> python C:/scripts/_oneshot_nightshift_terminal_grid.py --pane Coder`.
   The script already enumerates visible windows by title prefix and raises when none is
   found (V6) — a zero `wt.exe` exit is not evidence.
2. In the visible panes, run the placeholder's status prompt to confirm both are
   interactive, then from a third window run `agents-cli tail <pane-pid>` to record whether
   `AttachConsole` reads a WT pane's pseudoconsole buffer (settles the Foreman-diagnosis
   question; V3 already says the state column will not help).
3. Negative control: run the same command from the service context (e.g. `psexec -s` or the
   supervisor's own context) and expect the recorded "thread desktop denied" / no visible
   window. One positive and one negative result together confirm the C2 causal claim
   (V4/V5 make it the leading hypothesis; this control makes it evidence). Home-only —
   there is no service at work to run the negative control from.

**Bounded uncertainties and the test for each:**

| Uncertainty | Status after this review | Test |
| --- | --- | --- |
| Why some launcher-started sessions never write `<pid>.json` (F2) | Re-confirmed live (V2); cause still unknown | G1, three fresh launches with command lines captured |
| Whether a cross-session message to an idle session starts a turn without human input | Untested here (no `SendMessage` in this session) — FABLE CLAIM | G2 with the recipient idle |
| Whether `--resume` preserves the mesh name | Untested here | G2 kill + `--resume` step |
| Whether `AttachConsole` reads a WT pane's pseudoconsole | Untested here | G0 step 2 |
| Whether closing the WT window terminates the members | Untested here | G5 |
| Whether `wt.exe` (an app-execution alias, V17) and Python 3.11 exist at PIMCO | Untested; M5 adds both to `up --check` | `nightshift up --check` at PIMCO |
| Whether stored-not-sent messages re-deliver on resume (M1) | Design gap, correction specified | G5 with a staged stored-not-sent message |

---

## 4. What the revision gets right and should keep verbatim

Same list as Fable's section 3, all re-confirmed against the revision: the mailbox is
authoritative and terminal text is not; exactly-once is not promised; member, role, model,
CLI and account are separate fields; Foreman is not duplicated by a manager above it; dirty
repositories are preserved, never reset; a spawned process is not a ready member; a
permission prompt is awaiting a human, never injected into; forced termination is explicit;
unknown usage is labelled unknown; one review round, no recursion. Plus the revision's own
good decisions: native `SendMessage` as the single delivery channel with registration as a
readiness gate; the stub binding identity to process lineage; the failure ladder with no
injection; the gates G0-G5 in place of the mixed acceptance sequence.

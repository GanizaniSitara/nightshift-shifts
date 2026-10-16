# Gemini 4 Argon (High) gaps review of the Nightshift portable shifts design

Sequential third-model review of `docs/portable-shifts-design-mistral-reviewed.md` against `docs/portable-shifts-design.md`, `docs/portable-shifts-fable-critique.md`, `docs/portable-shifts-design-reviewed.md`, `docs/portable-shifts-mistral-critique.md`, and current repository/workstation source (`C:/git/nightshift`, `C:/git/agents-cli`, `C:/scripts/_oneshot_nightshift_terminal_grid.py`, `C:/scripts/launch.ps1`). Written 15 October 2026 from session `NGH-004`. One pass only; no source or configuration changes.

**Model and effort attestation.** Session metadata in Antigravity (`agy`) confirms model selection `Gemini 4 Argon (High)` with `EFFORT LEVEL: 1.00`. No model or effort substitution occurred.

**Evidence tags.**
- **[VERIFIED-ARGON]** — directly verified in this session from repository source or read-only local files, with exact path and line numbers.
- **[INHERITED CLAIM]** — asserted by Fable (`NGH-001`) or Mistral (`NGH-003`) from session-specific state or tool schemas (`SendMessage`/`ListAgents`) not present in `agy`, carried as a claim to be settled by gates rather than re-labelled as verified here.
- **[INFERENCE]** — architectural or Win32/git deduction from the verified code and design text.

---

## 1. Material uncovered gaps (ranked by severity)

Severity scale: **Blocker** = breaks core messaging, build, or lifecycle execution in the MVP as specified; **High** = causes a silent stall, dropped recovery, or git failure on normal retry/resume paths; **Medium** = causes contamination of build evidence or startup failure on a clean work machine.

### A1. Blocker: Daemonless CLI commands (`inbox`, `down`, and the build-queue pump) assume the `nightshift` CLI can invoke session-native `SendMessage` or wake idle agents

- **Exact passage or omission:**
  - `docs/portable-shifts-design-mistral-reviewed.md` lines 47, 49, 181–195 (`Mailbox and delivery` / `Reconciliation (M1)`):
    - Line 49: *"Native `SendMessage` is the delivery channel. The mailbox is the record. No injection path in the runtime."*
    - Line 181: *"The sender's own session delivers with `SendMessage(to: <recipient mesh name>, message: <header line + body>, notify_when_idle: true)` immediately after the mailbox write, then records `sent`."*
    - Lines 189–191: *"`nightshift inbox` reconciles in **both** directions: it lists messages addressed to the caller not yet acknowledged, and it **re-attempts native delivery of the caller's own `stored`-not-`sent` messages**."*
  - Lines 266–269 (`Queue pumping (M1)`): *"When a build finishes, the wrapper pumps the queue before exiting: if the slot is free, it starts the next due queued job."*
  - Lines 317–320 (`Down`): *"**Down** sends a `note` requesting a handover, drains or explicitly cancels outstanding work per policy, persists state, stops only owned processes..."*
  - Omissions: (1) No CLI command is specified for the sender to transition a mailbox row from `stored` to `sent` after the Claude model's `SendMessage` tool call returns; (2) lines 209–212 treat `SendMessage` failing to start a turn on an idle recipient as a role-brief workaround (`"adds a bounded notify_when_idle wait on Foreman's side"`) rather than a hard gate failure.
- **Concrete failure scenarios:**
  1. **CLI subprocesses cannot call `SendMessage` (`inbox` and `down`):** `SendMessage` is an in-process Claude Code model tool **[INHERITED CLAIM, Fable F3]**, not a CLI command or public IPC endpoint (`C:/git/agents-cli/cmd/agents-cli/sessions.go:180` and `commands.go:121` confirm `can_native` is `false` for all Claude sessions **[VERIFIED-ARGON]**).
     - When `nightshift inbox` runs as a Python CLI subprocess inside a Bash tool call, it cannot invoke the parent Claude session's `SendMessage` tool to re-deliver `stored`-not-`sent` messages.
     - Worse, because sending spans three distinct steps — (a) CLI `nightshift msg send` writing `stored` to SQLite, (b) the Claude model calling its `SendMessage` tool, and (c) an unspecified second CLI call to mark the row `sent` — a crash between (b) and (c) leaves a delivered message marked `stored` in SQLite.
     - When the human runs `nightshift down <shift>` from their own shell (line 285), `down` is an external CLI process outside the Claude mesh and console injection is banned (line 215). Writing a `note` row to SQLite cannot wake an idle Foreman or Coder (`"nobody polls an inbox"`, line 209), so `down` hangs waiting for a handover until it times out and declares healthy idle members "unresponsive".
  2. **Pumped build-queue jobs cannot wake the waiting requester and risk Bash timeout kill:** When Build 1's CLI wrapper finishes and starts a queued Build 2 before exiting (line 267), Build 2 executes inside Caller 1's synchronous Bash tool call (risking Claude Code's Bash tool timeout closing the wrapper handle and triggering `KILL_ON_JOB_CLOSE` mid-build). Even when Build 2 completes, the CLI wrapper has no `SendMessage` tool to wake the idle session that queued Build 2 **[INFERENCE]**.
  3. **Double-idle stall when `notify_when_idle` fires before submission or idle wake fails:** `notify_when_idle: true` is a one-shot notice **[INHERITED CLAIM, Fable F3]**. If Coder's turn ends after `nightshift ack` (e.g. tool error, rate limit, or multi-turn split) before submitting `result`, the single idle notice fires early; if Coder later stops after writing `stored` to SQLite without calling `SendMessage`, both Foreman and Coder are idle with no remaining wake-up trigger. Separately, if G2 finds `SendMessage` to an idle session does *not* start a turn without human input (lines 209–212), adding a `notify_when_idle` wait on Foreman does nothing to start an idle Coder's turn or wake an idle Foreman when Coder finishes **[INFERENCE]**.
- **Why Fable and Mistral have not already resolved it:**
  - Fable (C4, C8) removed the resident runtime and injection adapter, delegating delivery to the sender's session via `SendMessage`, but left `down`'s handover-note flow unchanged and omitted the post-`SendMessage` CLI transition to `sent`.
  - Mistral (M1) identified the `stored`-not-`sent` and build-queue liveness gaps created by removing the daemon, but placed both recovery mechanisms inside the `nightshift` CLI (`inbox` re-attempting native delivery and the build wrapper pumping queued builds on exit), overlooking that a CLI subprocess cannot invoke a Claude session's `SendMessage` tool.
- **Smallest correction:**
  1. **Explicit two-step outbound CLI contract:** `nightshift msg send` commits `stored` and prints JSON `{msg_id, to, payload}`; the calling Claude model invokes `SendMessage(to, payload, notify_when_idle=True)` and then runs `nightshift msg sent <msg-id>`. Permit the recipient's `nightshift ack <msg-id>` to transition a row directly from `stored` or `sent` to `acknowledged` so a sender crash between `SendMessage` and `nightshift msg sent` self-heals.
  2. **`inbox` emits outbound actions to the model:** `nightshift inbox` never attempts transport delivery itself; it outputs structured JSON with `inbound_unacked` and `outbound_unsent` (`state = 'stored'`), and the role brief instructs the calling agent to call `SendMessage` + `nightshift msg sent <id>` for each entry in `outbound_unsent`.
  3. **Blocking slot wait inside the requester's `nightshift build` call:** Instead of exiting immediately on contention and relying on another caller's wrapper exit to pump the queue, `nightshift build` registers the `queued` row and waits in a bounded poll loop on `locking.exclusive` inside the **requesting agent's own CLI call** (aborting early if a newer commit on the same assignment supersedes it). When the lock frees, it runs the build in its own process and returns the result directly as stdout to the requesting agent.
  4. **Self-contained `nightshift down` handover:** `nightshift down <shift>` marks the shift `shutting_down` in SQLite, synthesises the durable handover directly from authoritative SQLite rows + git worktree state (including Foreman's latest recorded summary from `nightshift handover` if present), cancels any running build Job object, and stops owned processes by `(pid, start_time)` without waiting on an un-deliverable `note`.
  5. **Hard G2 gate on idle wake:** Make automatic turn start on `SendMessage` to an idle recipient a mandatory pass criterion of G2 (if it fails, the architecture requires a minimal console-Enter poke or hook on idle, not a role-brief wait).
- **Falsifiable test:**
  - **G5 outbound recovery test:** Insert a `stored` message row via `nightshift msg send` without calling `SendMessage` or `nightshift msg sent`; run `nightshift inbox` in the sender session and assert stdout JSON contains `outbound_unsent: [{msg_id, to, payload}]`, after which the sender calls `SendMessage` + `nightshift msg sent <id>` and the row reaches `sent`/`acknowledged`.
  - **G5 idle `down` test:** With both Foreman and Coder idle at their prompts, run `nightshift down <shift>` from the human shell; assert it writes the handover file from SQLite/git state and exits `0` within 5 s without hanging.

---

### A2. High: Incarnation fencing conflates `sender_incarnation` with `owner_incarnation`, and `nightshift inbox` / `ack` hide acked-but-unfinished work on `resume` (breaking M2 and M8)

- **Exact passage or omission:**
  - `docs/portable-shifts-design-mistral-reviewed.md` lines 82–91 (`Recipient-side protocol check (M2)`), lines 170–174 (`Mailbox`), lines 189–192 (`Reconciliation (M1)`), lines 199–203 (`Header line (C6)`), lines 228–230 (`Assignments and worktrees (M8)`), and lines 323–330 (`Resume`):
    - Line 171: *"Message row: message ID, shift ID, sender and recipient member IDs, **sender incarnation**, assignment ID, kind..."*
    - Lines 83–88: *"On receipt, before acting, the member runs `nightshift ack <msg-id>`. Three answers, three behaviours: `acknowledged` — continue; `already acknowledged` — stop; `unknown message` — ... Do not act."*
    - Line 189: *"`nightshift inbox` ... lists messages addressed to the caller **not yet acknowledged**"*
    - Line 201: *"Acceptance of an assignment is keyed by `(assignment ID, incarnation)`"*
    - Lines 228–230: *"an assignment acked by an incarnation that died before submitting is re-dispatched to the replacement incarnation on resume (M8, tested in G5)."*
- **Concrete failure scenarios:**
  1. **Acked-but-unfinished assignments are invisible to `nightshift inbox` after `resume` (breaks M8):**
     - Coder (`inc=1`) receives `assignment` `asg-1` (`msg-1`), runs `nightshift ack msg-1` (setting `msg-1` to `acknowledged` and `asg-1` to `working`), makes partial edits in the lease, and dies when its pane closes.
     - On `nightshift resume`, Coder (`inc=2`) and/or Foreman run `nightshift inbox` as their first instruction (line 326).
     - Because `nightshift inbox` only returns messages addressed to the caller **not yet acknowledged** (line 189) plus `stored`-not-`sent` rows (line 190), and `msg-1` is already `acknowledged` while all of Foreman's outbound messages are already `acknowledged`, `nightshift inbox` returns **empty** to both Coder (`inc=2`) and Foreman. Nothing tells Foreman to re-dispatch `asg-1` with `inc=2`, and nothing tells Coder (`inc=2`) about `asg-1`.
  2. **`sender_incarnation` is the wrong column to fence assignment ownership:**
     - The message schema (line 171) only stores `sender_incarnation`. When Foreman (`inc=1`) sends an `assignment` to a restarted Coder (`inc=2`), `sender_incarnation` is `1` (Foreman's incarnation), not `2` (Coder's incarnation). Keying assignment acceptance or re-dispatch on `sender_incarnation` fails whenever Coder restarts more often than Foreman **[INFERENCE]**.
  3. **`nightshift ack <msg-id>` does not verify recipient/incarnation or return the authoritative SQLite body (weakens M2):**
     - Under M2 (lines 83–91), `nightshift ack <msg-id>` takes only `<msg-id>` and returns a bare status string (`acknowledged` / `already acknowledged` / `unknown message`), leaving the recipient to act on the unverified body text inside `SendMessage`. If a spoofed or stale message reuses an existing `msg=<id>` (e.g. `msg=1` from an earlier note, or a message addressed to `inc=1` re-read after `--resume`), `ack` either halts `inc=2` with `already acknowledged` or returns `acknowledged` for a message that belonged to another recipient/incarnation or carried a different body in SQLite **[INFERENCE]**.
- **Why Fable and Mistral have not already resolved it:**
  - Fable (C6) introduced `inc=<n>` and idempotent `ack` to prevent duplicate `SendMessage` execution.
  - Mistral (M2, M8) extended `ack` into a protocol-violation check and added G5's acked-but-unfinished re-dispatch requirement, without updating `nightshift inbox`'s filter (which excludes `acknowledged` rows), adding `recipient_incarnation` / `owner_incarnation` to the schema, or having `ack` return the authoritative row from SQLite.
- **Smallest correction:**
  1. Record `sender_incarnation` **and** `recipient_incarnation` on message rows, and `owner_incarnation` on assignment rows. In the header `NGH shift=<id> msg=<id> kind=<kind> asg=<id> inc=<n>`, `inc` is `recipient_incarnation` for `assignment` messages and `sender_incarnation` for `result` messages (both equal to the Coder's `owner_incarnation`).
  2. During `nightshift resume`, when a member's incarnation advances to `n+1`, any assignment owned by that member in `{assigned, working}` with `owner_incarnation < n+1` is automatically transitioned to `redispatch_required` (or re-bound to `owner_incarnation = n+1` with a new `stored` assignment message queued for delivery), and `nightshift inbox` includes an `active_assignments` / `stale_assignments` section in its output.
  3. `nightshift ack <msg-id>` must validate `row.recipient == NIGHTSHIFT_MEMBER` and `row.recipient_incarnation == current_incarnation`, and on `acknowledged` must **print the authoritative message row (`kind`, `asg`, `inc`, `body`, `lease_path`) from SQLite**; the role brief states that members act only on the payload printed by `nightshift ack` or `nightshift inbox`, never on raw `SendMessage` text.
- **Falsifiable test:**
  - **G5 M8 test:** Dispatch `asg-1` from Foreman (`inc=1`) to Coder (`inc=1`); have Coder run `nightshift ack`, edit one file, then kill only Coder's process tree and run `nightshift resume` (Foreman stays `inc=1`, Coder becomes `inc=2`). Assert `nightshift inbox` surfaces `asg-1` for re-dispatch/continuation under `owner_incarnation = 2` and rejects a late `result` stamped with `inc=1`.
  - **G2 M2 replay test:** Send a `SendMessage` whose header reuses a real existing `msg=<id>` (e.g. a previous `note` row) but whose body claims to be a new `assignment`; assert `nightshift ack <msg-id>` rejects or prints the true SQLite `note` row rather than allowing the forged body to execute.

---

### A3. High: `Up` tries to lease the first assignment's worktree before Foreman cuts any assignment, and `git worktree add -b` (`M4`) fails when re-attaching an existing assignment branch

- **Exact passage or omission:**
  - `docs/portable-shifts-design-mistral-reviewed.md` lines 95–118 (`Shift definition`), line 165 (`Startup context`), lines 232–240 (`Worktrees (C9, M4)`), and lines 290–295 (`Up`):
    - Line 291: *"Up validates the manifest and prerequisites, allocates the shift ID and SQLite file under `locking.exclusive`, **leases the first assignment's worktree from its base commit**, opens the two-pane sheet..."*
    - Lines 235–236: *"the existing `worktree.py` lease semantics apply ... extended with a base parameter: `git worktree add -b <branch> <dest> <base>`"*
- **Concrete failure scenarios:**
  1. **No assignment exists when `nightshift up` runs:** The manifest schema (lines 95–118) contains `repository` and `brief: brief.md`, with **no assignment list and no `base_commit`**. Cutting bounded assignments from `brief.md` is Foreman's job *after* the shift is up (lines 65, 393). If `up` attempts to lease "the first assignment's worktree from its base commit" before launching Foreman, `up` has no assignment ID or base SHA to lease. Conversely, if `up` launches the Coder pane with `-d <repository>` (the user's dirty shared checkout) before an assignment lease exists, Coder's interactive session starts inside the shared checkout that the design promises never to touch **[INFERENCE]**.
  2. **`git worktree add -b` fails when the assignment branch ref already exists (`C:/git/nightshift/src/nightshift/orchestration/worktree.py:84–118`, [VERIFIED-ARGON]):**
     - In `worktree.py:84–89`, `branch_worktree(repo, branch)` only detects a worktree if `git worktree list --porcelain` reports `branch refs/heads/<branch>`.
     - If an assignment worktree was previously removed, or if `dest` is currently in detached HEAD state (so `list_worktrees` has no `branch` field for `dest`), `branch_worktree` returns `None` even though `refs/heads/ngh/<shift>/<asg>` **already exists** in `repo`.
     - Running Mistral M4's `git worktree add -b ngh/<shift>/<asg> <dest> <base>` then fails with `fatal: a branch named 'ngh/<shift>/<asg>' already exists` (or `fatal: '<dest>' already exists`).
- **Why Fable and Mistral have not already resolved it:**
  - Fable (C9) inserted "leases the first assignment's worktree" into `Up` while keeping assignment creation inside Foreman's interactive workflow (G3).
  - Mistral (V10, M4) verified that `worktree.lease()` lacked `-b <branch> <base>` and proposed `git worktree add -b <branch> <dest> <base>`, without catching the `Up` ordering contradiction or the `git worktree add -b` failure when `refs/heads/<branch>` already exists.
- **Smallest correction:**
  1. **Lease on `nightshift assign`, not `nightshift up`:** `nightshift up` creates an empty per-member working directory (`<state_dir>/shifts/<shift>/members/coder-1`) as the Coder pane's initial `-d` directory (never the shared checkout). When Foreman cuts an assignment via `nightshift assign --base <sha> ...`, the CLI creates the worktree lease `<worktrees_dir>/<shift>/<asg>` and returns its path in the assignment record and in `nightshift ack`.
  2. **Branch-existence check in `worktree.lease(repo, branch, dest, base=None)`:**
     - First check if any entry in `list_worktrees(repo)` has `_same_path(wt["path"], dest)` (detecting a detached worktree at `dest` and raising `WorktreeConflict` or re-attaching `branch`).
     - Next check `git -C <repo> show-ref --verify --quiet refs/heads/<branch>`: if exit code is `0` (branch already exists), run `git worktree add <dest> <branch>` so existing commits on the assignment branch are preserved; only run `git worktree add -b <branch> <dest> <base>` when the branch ref does not yet exist.
- **Falsifiable test:**
  - Create assignment lease `ngh/s1/a1` from `<base>`, commit a change in `<dest>`, remove the worktree directory with `git worktree remove <dest>`, and call `lease(repo, "ngh/s1/a1", dest, base=<base>)`: assert it returns `"created"` and `git -C <dest> rev-parse HEAD` equals the committed change rather than failing with `branch already exists` or resetting to `<base>`.

---

### A4. Medium: Warm build cleanliness (`M3`) permits unignored untracked source files (`??`) to contaminate commit-specific evidence or break `git checkout --detach`, and fails after recipes that dirty tracked files

- **Exact passage or omission:**
  - `docs/portable-shifts-design-mistral-reviewed.md` lines 259–265 (`Cleanliness semantics (M3)`):
    > *"'The tree is clean' means **no tracked modifications**: the wrapper asserts `git diff --quiet` and `git diff --cached --quiet`. Untracked build outputs are allowed when the recipe declares `cache: keep`; when the recipe declares `cache: clean`, the wrapper runs the recipe-declared clean step (or `git clean -fdx` on the build worktree) before checkout."*
- **Concrete failure scenarios:**
  1. **Unignored untracked files (`??`) contaminate subsequent builds:** `git diff --quiet` and `git diff --cached --quiet` ignore *all* untracked files — not only `.gitignore`d build caches (`!!`, e.g. `target/`, `bin/`, `obj/`) but also **unignored untracked source or test files** (`??`, e.g. a generated `src/foo_gen.py` or `tests/test_extra.py`). Under `cache: keep`, an unignored untracked file created during Build 1 survives into Build 2 for a different commit `<sha2>`, invalidating commit-specific build evidence (line 271) **[INFERENCE]**.
  2. **Untracked/ignored file collision on `git checkout --detach <sha2>`:** If Build 1 leaves an untracked or ignored file at a path that `<sha2>` tracks as a committed file, `git checkout --detach <sha2>` aborts with `error: The following untracked working tree files would be overwritten by checkout` **[INFERENCE]**.
  3. **Post-build tracked modifications break the next warm build:** If Build 1's recipe modifies a tracked file in-tree (e.g. lockfile, version stamp, generated header), Build 1 succeeds and exits, and Build 2 then fails the pre-checkout `git diff --quiet` check before it even checks out `<sha2>` **[INFERENCE]**.
- **Why Fable and Mistral have not already resolved it:**
  - Fable (C10) specified "asserts the tree is clean" without distinguishing tracked files, unignored untracked files, and `.gitignore`d caches.
  - Mistral (M3) noticed `git status --porcelain` fails when build artifacts are present and relaxed the check to `git diff --quiet` + `git diff --cached --quiet`, which threw out the check against unignored untracked (`??`) files instead of using git's built-in distinction between untracked (`-fd`) and ignored (`-fdx`) files.
- **Smallest correction:**
  1. **Post-build verification on the current build:** After the recipe exits, run `git status --porcelain` (which reports modified tracked files and unignored untracked `??` files, while omitting `.gitignore`d `!!` caches). If non-empty, record `worktree_dirtied_by_build = true` on the build result (and fail the verification if the recipe requires a pure build).
  2. **Pre-checkout preparation on `<worktrees_dir>/_build`:**
     - For `cache: keep`: run `git reset --hard`, then **`git clean -fd` (without `-x`)** to remove all unignored untracked files/directories (`??`) while preserving `.gitignore`d caches (`!!`), then `git checkout --overwrite-ignore --detach <sha>`.
     - For `cache: clean`: run `git reset --hard`, then `git clean -fdx` (or the recipe clean command), then `git checkout --detach <sha>`.
- **Falsifiable test:**
  - In G4 (`cache: keep`), create both an unignored untracked file `src/stray.py` and a `.gitignore`d cache file `build/cache.bin` in `<worktrees_dir>/_build`, then run a warm build on `<sha2>` (where `<sha2>` also adds a tracked file at a previously ignored path): assert `src/stray.py` is deleted before the recipe runs, `build/cache.bin` survives, and checkout succeeds without collision.

---

### A5. Medium: Zero-dependency Python packaging claim (`V13`/`M5`) contradicts the YAML manifest and `psutil`-dependent process checks in the repository, and passes multi-line startup context on the Windows command line

- **Exact passage or omission:**
  - `docs/portable-shifts-design-mistral-reviewed.md` lines 55–60 (`Engineering choices`), lines 95–118 (`Shift definition` YAML manifest), lines 138–143 (`Pane bootstrap`), lines 297–307 (`up --check` prerequisites), and lines 440–441 (`Reference points`):
    - Lines 57–58: *"the package core has no third-party dependencies and `sqlite3` is stdlib [VERIFIED-ME, V13]."*
    - Lines 95–118: Declarative manifest specified in **YAML**.
    - Lines 141–142: `claude --name ngh-<shift>-<member> --model claude-opus-5-5 [--permission-mode <mode>] <startup context>`
- **Concrete failure scenarios:**
  1. **Missing `PyYAML` and `psutil` under work restrictions (`dependencies = []`):**
     - `C:/git/nightshift/pyproject.toml:11–17` **[VERIFIED-ARGON]** sets `dependencies = []`, placing `psutil>=5.9` and `pywin32>=306` in optional extras and omitting `PyYAML` entirely.
     - Python 3.11 stdlib includes `tomllib` and `json`, **not `yaml`**. On a work machine where package environments cannot be modified (line 307), a stdlib-only Python installation cannot parse the YAML manifest in lines 95–118.
     - Furthermore, existing process helpers in `C:/git/nightshift/src/nightshift/watchdog/watchdog.py:280–281` (`if psutil is None: return ProcessCheck(False, "psutil-not-installed")`) and `src/nightshift/orchestration/shift.py:185–188` (`_close_worker` silently returns when `import psutil` fails) **[VERIFIED-ARGON]** require `psutil`. Reusing them without `psutil` causes `status`/`resume` to report every live process as dead (`psutil-not-installed`) and `down` to silently skip process termination.
  2. **Command-line mangling of `<startup context>`:** Passing the combined shift brief, role brief, allowed routes, header rules, protocol check, and handover (lines 163–166) as an inline positional argument `<startup context>` through `cmd /k nightshift member-start ...` -> `claude.cmd` on Windows breaks on embedded newlines, quotes, `%ENV%` expansions, and shell metacharacters (`&`, `|`, `<`, `>`). (`C:/scripts/launch.ps1:1293` **[VERIFIED-ARGON]** avoids this by writing context to disk and passing a one-line `"Read the task file at <path>..."` prompt.)
- **Why Fable and Mistral have not already resolved it:**
  - Fable (C8, C12) proposed reusing Nightshift's Python modules for the CLI.
  - Mistral (V13, M5) verified `dependencies = []` in `pyproject.toml` and added `python >= 3.11` to `up --check`, without noticing that stdlib lacks YAML and that `watchdog.py`/`shift.py` fail when `psutil is None`, or that `<startup context>` is passed inline on the command line.
- **Smallest correction:**
  1. **Use TOML (`shift.toml` via Python 3.11 stdlib `tomllib`) or JSON for the shift manifest** (or explicitly require and check `import yaml` in `up --check`).
  2. **Stdlib `ctypes` Win32 process/Job helper:** Implement `(pid, start_time)` checks (`OpenProcess` + `GetProcessTimes`), child-tree discovery (`CreateToolhelp32Snapshot`), and build Job objects (`CreateJobObjectW` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) in a self-contained `ctypes.windll.kernel32` module inside `nightshift`, never calling `watchdog.check_process` or `shift._close_worker` when `psutil` is absent.
  3. **File-backed startup context:** `member-start` writes the assembled startup context to `<shift_dir>/bootstrap-<member>-inc<n>.md` and invokes `claude` with a short one-line positional prompt: `"Read <path> and run nightshift inbox."`
- **Falsifiable test:**
  - Run `nightshift up --check` and the unit test suite for manifest loading, `(pid, start_time)` verification, descendant discovery, and Job-object tree termination with `psutil` and `yaml` blocked (`-P` / `sys.modules['psutil'] = None; sys.modules['yaml'] = None`): assert all pass using Python 3.11 stdlib only.

---

## 2. Unsupported or wrong factual claims in previous reviews

1. **Mistral's G0 `wt.exe` example command (`docs/portable-shifts-mistral-critique.md:313–318`) uses the wrong split flag (`-H` instead of `-V`), omits focus restoration, and falsely claims `--pane` runs the `EnumWindows` check:**
   - **Claim:** Mistral prescribes running:
     `wt.exe -w ngh-g0 --pos 0,0 --maximized new-tab --title "Nightshift | Foreman" --suppressApplicationTitle -d <fixture> python C:/scripts/_oneshot_nightshift_terminal_grid.py --pane Foreman ; split-pane -H -s 0.5 --title "Nightshift | Coder" --suppressApplicationTitle -d <fixture> python C:/scripts/_oneshot_nightshift_terminal_grid.py --pane Coder`
     and states: *"The script already enumerates visible windows by title prefix and raises when none is found (V6) — a zero `wt.exe` exit is not evidence."*
   - **Verification (`C:/scripts/_oneshot_nightshift_terminal_grid.py:44–90`, [VERIFIED-ARGON]):**
     - **Wrong split orientation:** In `wt.exe split-pane`, `-H` (`--horizontal`) splits across a horizontal divider (**top and bottom rows**, 2×1), whereas `-V` (`--vertical`) splits across a vertical divider (**side-by-side columns**, 1×2). `_oneshot_nightshift_terminal_grid.py` uses `split-pane -H -s 0.5` at line 60 for the bottom row (`Reserved-5`) and `split-pane -V` at line 64 for the side-by-side `Coder` column. Fable C2 (`docs/portable-shifts-fable-critique.md:125`) correctly wrote `split-pane -V -s 0.5`; Mistral changed `-V` to `-H`, which stacks Foreman and Coder vertically instead of side by side.
     - **`EnumWindows` is never called when `--pane` is passed:** At `_oneshot_nightshift_terminal_grid.py:44–46`, `if args.pane: pane(args.pane); return` runs the interactive prompt inside the pane and returns. The `EnumWindows` check at lines 71–90 only executes when the script is invoked *without* `--pane` as the parent launcher. Running Mistral's `wt.exe ... --pane Foreman ; split-pane ... --pane Coder` command directly from a shell skips lines 71–90 completely.
     - **Focus lands on Coder instead of Foreman:** Both Fable's (`portable-shifts-fable-critique.md:125`) and Mistral's (`portable-shifts-mistral-critique.md:316`) two-pane commands drop the trailing `move-focus` subcommand (`_oneshot_nightshift_terminal_grid.py:69`). Because `wt.exe split-pane` moves focus to the newly created right-hand pane, the window opens with keyboard focus inside the **Coder** pane rather than **Foreman**, violating "Foreman is the user's sole normal contact". The launcher must append `; move-focus left` (or `; focus-pane -t 0`) so focus starts in Pane 0 (Foreman) and the top-level window title reflects Foreman.

2. **Both Fable (`portable-shifts-fable-critique.md:130`, C2 step 3) and Mistral (`portable-shifts-mistral-critique.md:320`; `portable-shifts-design-mistral-reviewed.md:376`, G0) prescribe `agents-cli tail <placeholder-pid>`, which fails before `AttachConsole` is ever called:**
   - **Claim:** Running `agents-cli tail <placeholder-pid>` on the G0 placeholder pane (`python _oneshot_nightshift_terminal_grid.py --pane Foreman`) tests whether `AttachConsole` reads a Windows Terminal pane's pseudoconsole buffer.
   - **Verification (`C:/git/agents-cli/cmd/agents-cli/commands.go:49–78`, `sessions.go:44–160, 233–241`, `procs.go:246–273`, [VERIFIED-ARGON]):**
     `agents-cli tail <ref>` calls `lookup(ref)` -> `discover()`, which only includes processes matched by `classify(p)` (`claude`, `codex`, `agy`, `qwen`, `copilot`, `opencode`, `gemini`, `vibe`), `.launch-info.json`, or `adopted`. A plain `python.exe _oneshot_nightshift_terminal_grid.py --pane Foreman` has `classify(p) == ""`, so `agents-cli tail <placeholder-pid>` fails immediately with `no agent session with pid <N> (try agents-cli ls --all, or adopt)` **without ever calling `AttachConsole`**.
   - **Correction for G0:** Use `agents-cli _attach peek <pid> 0` (`main.go:59–60`, `console.go:157–173`, which attaches and reads `CONOUT$` directly for any PID) or run `agents-cli adopt <pid> --tool claude --name g0-foreman` before `agents-cli tail <pid>`.

3. **Fable's `[Verified]` causal claim (`portable-shifts-fable-critique.md:101–121`, C2; `portable-shifts-design-reviewed.md:313–314`) that the 15 October Windows Terminal failure was caused by the `LocalSystem` (Session 0) supervisor service:**
   - **Claim:** `portable-shifts-design-reviewed.md:313–314` marks as `[VERIFIED]` that the leading cause of the 15 October activation failure was launch from the `LocalSystem` supervisor in Session 0 (`portable-shifts-design-mistral-reviewed.md:378, 414–417` softens this to a hypothesis tested by a Session 0 service negative control in G0).
   - **Verification (`C:/git/nightshift/src/nightshift/supervisor.py`, `daemon.py`, and `docs/portable-shifts-design.md:156`, [VERIFIED-ARGON]):**
     - `docs/portable-shifts-design.md:156` states the 15 October trial was a **"delegated layout trial"** (an agent/subagent invocation), and `supervisor.py` contains no `wt.exe` or layout code (and unattended Nightshift worker dispatch is disabled per `C:/git/nightshift/AGENTS.md:21–26`).
     - In Windows Session 1, a subprocess spawned inside an agent's sandboxed execution environment or alternate desktop/job (e.g. a restricted token or non-`Winsta0\Default` desktop within Session 1) still reports `ProcessIdToSessionId == WTSGetActiveConsoleSessionId()`, yet fails `GetThreadDesktop` access to the interactive `WindowsTerminal.exe` monarch or sees zero visible windows under `EnumWindows` **[INFERENCE]**.
   - **Material impact:** `nightshift up`'s launch-principal check (line 281) must verify not only `ProcessIdToSessionId(self) == WTSGetActiveConsoleSessionId()` and thread desktop name `Default`, but also that `EnumWindows` can see existing visible desktop windows prior to launching `wt.exe` (catching Session-1 agent sandboxes), and G0 does not need an elevated Session-0 service negative control that cannot run at work.

---

## 3. Compact proposed amendment list (against `portable-shifts-design-mistral-reviewed.md`)

1. **Outbound delivery & `inbox` contract (`Mailbox and delivery`, lines 181–198; `Resume`, lines 323–330):**
   - Add `nightshift msg sent <msg-id>` to record `sent` after the calling Claude session's `SendMessage` tool returns. Allow `nightshift ack <msg-id>` to transition a row from either `stored` or `sent` to `acknowledged`.
   - Replace *"re-attempts native delivery of the caller's own `stored`-not-`sent` messages"* in `nightshift inbox` with: *"`nightshift inbox` returns structured JSON containing `inbound_unacked`, `active_assignments`, and `outbound_unsent`; the calling Claude session invokes `SendMessage` + `nightshift msg sent <id>` for each item in `outbound_unsent`."*
   - In `G2` (lines 209–212, 386–392), make automatic turn start upon `SendMessage` to an idle session a hard go/no-go criterion; remove the ineffective `"bounded notify_when_idle wait on Foreman's side"` fallback.
2. **Self-contained `down` (`Shift lifecycle`, lines 317–321):**
   - Replace *"`Down` sends a `note` requesting a handover"* with: *"`Down` sets shift state to `shutting_down` in SQLite, generates the durable handover directly from SQLite registry/assignment/mailbox/build records and git worktree status (incorporating Foreman's latest `nightshift handover` summary if recorded), closes any active build Job object, and terminates owned member processes by `(pid, start_time)`."*
3. **Synchronous build-slot wait (`Builds and verification`, lines 266–269; `G4`, lines 398–402):**
   - Replace the exit-time queue pump with a bounded poll wait on `locking.exclusive` inside the requesting caller's `nightshift build` invocation (aborting if superseded by a newer commit on the same assignment), so the build runs in the requester's own CLI call and returns its result directly to the requester without needing cross-session `SendMessage` from a CLI wrapper.
4. **Incarnation columns & authoritative `ack` (`Roles and authority`, lines 82–91; `Mailbox`, lines 170–174; `Assignments`, lines 225–230):**
   - Store `sender_incarnation` and `recipient_incarnation` on message rows, and `owner_incarnation` on assignment rows.
   - `nightshift ack <msg-id>` checks `recipient == NIGHTSHIFT_MEMBER` and `recipient_incarnation == current_incarnation`, and prints the authoritative SQLite row (`kind`, `asg`, `inc`, `body`, `lease_path`); members act only on the SQLite payload returned by `ack`/`inbox`, never on raw `SendMessage` text.
   - On `nightshift resume`, any assignment in `{assigned, working}` owned by a previous incarnation is surfaced in `nightshift inbox` (`active_assignments` / `redispatch_required`) even though its original assignment message was already `acknowledged`.
5. **Worktree lease timing & branch recovery (`Assignments and worktrees`, lines 232–240; `Up`, line 291):**
   - Remove *"leases the first assignment's worktree from its base commit"* from `Up` (line 291); start Coder's pane in `<state_dir>/shifts/<shift>/members/coder-1` and create the assignment worktree lease in `nightshift assign --base <sha>`.
   - In `worktree.lease(repo, branch, dest, base=None)`, check `_same_path(wt["path"], dest)` across all worktrees first, and check `git show-ref --verify --quiet refs/heads/<branch>`: use `git worktree add <dest> <branch>` when the branch already exists and `git worktree add -b <branch> <dest> <base>` only when creating a new branch.
6. **Warm build cleanliness (`Builds and verification`, lines 259–265):**
   - Under `cache: keep`, run `git reset --hard`, `git clean -fd` (no `-x`, removing unignored untracked `??` files while keeping `.gitignore`d `!!` caches), and `git checkout --overwrite-ignore --detach <sha>`. After the build, check `git status --porcelain` to detect recipes that mutate tracked files or leave unignored untracked files.
7. **Stdlib-only portability & G0 commands (`Shift definition`, lines 95–118; `Pane bootstrap`, lines 138–143; `G0`, lines 374–380):**
   - Use TOML (`shift.toml` via stdlib `tomllib`) or JSON for the manifest, implement Win32 `(pid, start_time)` / child-walk / Job-object helpers via stdlib `ctypes` (not `psutil`), and pass `<startup context>` via a file on disk (`"Read <path> and run nightshift inbox"`).
   - In `G0`, use `split-pane -V -s 0.5` followed by `; move-focus left`, run the parent script (not bare `wt.exe ... --pane`) so `EnumWindows` executes, and use `agents-cli _attach peek <pid> 0` (or `agents-cli adopt` first) instead of bare `agents-cli tail <placeholder-pid>`.

---

## 4. Verdict on proceeding to the two-pane / messaging spike

**Verdict: PROCEED immediately to the `G0`–`G2` two-pane and native-messaging spike**, with the command corrections in Section 2 applied before running `G0` and the protocol amendments in Section 3 folded in before building the `G3`–`G5` CLI/SQLite layer.

**What must precede each step:**
1. **Before running `G0` (today, zero architectural work):**
   - Fix the `G0` probe command to use `split-pane -V -s 0.5` (side-by-side columns, not Mistral's `-H`), append `; move-focus left` (so focus starts in Foreman, not Coder), execute the `EnumWindows` visibility check in the parent launcher process (not bypassed via `--pane`), and use `agents-cli _attach peek <pid> 0` (or `agents-cli adopt`) to test `AttachConsole` against the placeholder panes.
2. **Before running `G1`–`G2`:**
   - Resolve the owner choice on `permission_mode` (`default` vs `bypassPermissions`, identical for both panes) and treat automatic turn wake-up on `SendMessage` to an idle recipient as a hard pass/fail gate in `G2`.
3. **Before implementing `G3`–`G5` (`nightshift` CLI + SQLite):**
   - Apply Amendments 1–7 above so the CLI never pretends a subprocess can invoke `SendMessage`, `ack`/`inbox` carry `recipient_incarnation` / `owner_incarnation` and return authoritative SQLite payloads, `worktree.lease` leases at `assign` time and handles pre-existing branches, `cache: keep` cleans unignored `??` files with `git clean -fd`, and the CLI uses stdlib `tomllib`/`json` + `ctypes` + file-backed startup prompts with zero `psutil`/`PyYAML` dependency.

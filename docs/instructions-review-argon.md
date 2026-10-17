# Review of `docs/instructions-review.md` (Gemini 4 Argon)

**Verdict:** Approve the architecture—moving protocol ceremony into CLI primitives and cutting briefs to one conditional page is the right fix—but fix two flaws before implementation: the §5 Foreman brief drops the clean-HEAD verification step required by `land`, and §7's three-tier prose knob (`terse | standard | guided`) will recreate template drift and bloat cheap panes.

---

### 1. Is the diagnosis right?

Yes. Seven of the nine problems in §2 are real root causes: **#1** (patch-log accretion into 300-word paragraphs), **#2** (phantom roles like Apprentice taught to crews without one), **#4** (stale slot numbers and `/usage` instructions), **#5** (three-command send and four-command non-Claude ceremony), **#6** (`agents-cli tail` polling burning context), **#7** (no slot map), and **#9** (`common.md` teaching Claude tools first and `-cli.md` telling non-Claude models to ignore what they just read).

**Overstated:**
- **#3 (Triple repetition)** and **#8 (Persona/protocol entanglement)** are the same problem: `personas/*.yaml` is a redundant 500-word preamble. Repeating a core invariant ("never edit repo files") in both identity and a final `NEVER` list actually helps compliance; only repeating *procedural mechanics* (the READY gate and uplift flags) hurts.

**Missing from §2:**
1. **Direct self-contradiction in the rendered Foreman brief:** `personas/foreman.yaml` says *"Then end the shift yourself"* (`Foreman.brief.md` L17, L41), while `briefs/foreman.md` says *"Do not close the shift yourself unless the human asks; the warden closes an idle shift"* (`Foreman.brief.md` L60).
2. **Irrelevant teardown internals:** `foreman.md` spends ~300 words explaining how `down --close`, `recovery.json`, `floor.json`, and `cleaner` behave on failure—machinery the Foreman neither chooses nor runs during a shift.
3. **Scattered ticket-note rules:** `tasks-cli note` is mandated four times across `foreman.md` and the task footer, then disappears from §5.

---

### 2. Is the proposed Foreman brief in §5 enough for a strong model?

Almost, but a strong model given only §5 will fail at commit and landing time.

**What a Foreman would get wrong with only §5:**
1. **Dirty-tree vs. clean-HEAD verification:** `land` requires `TESTED-PASS` on the exact clean committed `HEAD` (`dirty: false` in `builds/<n>.json`). §5 step 4 says *"Every finished change goes to Tester... Commit only a TESTED-PASS"*, and step 6 jumps straight to `nightshift land`. If Tester verifies the dirty working tree and Foreman commits afterward, `HEAD` changes and `land` refuses it.
2. **Diff review skipped:** §5 routes Coder's report straight to Tester without Foreman inspecting `git diff` against the assignment.
3. **Startup readiness:** Step 2 assigns immediately without waiting for `READY` (unless `nightshift assign` blocks until the target pane is ready).
4. **Queue termination:** Step 6 ends with `nightshift queue next` but omits what to do when the queue is empty (stop and wait; do not run `down`).

**Single addition that prevents the most damage:**
Rewrite step 4 to make the review-commit-verify sequence explicit (or move commit creation into `nightshift land`):
> `4. Review git diff. Send to Tester: nightshift assign <shift> Tester "verify <asg>: <what changed>" --coding-asg <asg>. On TESTED-PASS, git commit the change and have Tester run a clean-HEAD build before landing.`

---

### 3. Is "the tool enforces, the brief orients" right, and are the six commands in §6 the right six?

The principle is spot-on. Every prose rule replaced by a CLI preflight check or a one-line stderr recovery hint removes a failure mode permanently.

- **Matters most:** `nightshift wait` (with `assign`/`report`). Replacing `agents-cli tail` polling and manual `ack` with a blocking event primitive eliminates the main source of context rot and dropped messages.
- **Unnecessary:** `nightshift slots`. If the brief renders the live Crew/Slot table (§5) and `nightshift spawn <shift> --role Coder2` automatically picks the next free non-reserved, non-Ops slot when `--slot` is omitted, the Foreman never needs a slot-inspection command.
- **What I would do instead:** Drop `nightshift slots` (keep slot state in `nightshift status` for humans, and make `spawn` auto-pick free slots while refusing Ops/reserved slots). Replace it in §6 with **`nightshift result <shift> done|blocked --stage <rung> --summary "..."`**—§5 step 6 already calls `nightshift result`, and generating `result.json` (auto-attaching `bus.jsonl`, `builds/<n>.json`, `uplift_counts`, and Copilot credit usage) in Go eliminates schema errors.

---

### 4. Tuning the brief to the model (§7)

**Disagree with the three-level `terse | standard | guided` knob.** Maintaining three prose tiers per role reintroduces the `-cli.md` maintenance trap, and making `guided` *longer* for Flash/Luna panes contradicts `context_words`: cheap panes on a 30-credit cap are hurt by input token overhead on every turn.

**What I would do instead:** Generate every brief from a single Go struct of shift facts (role, active crew table, worktree/build command, enabled capabilities) with at most **two rendering modes** (`compact` for constrained/small models, `standard` for frontier models), both under 250 words.

**How to brief Gemini Flash (30 credits) vs. Opus:**
- **Gemini Flash:** Zero open-ended discretion and no prose explanation. Give a 4-step checklist with pre-filled commands (`nightshift claim`, `nightshift report`), scope edits strictly to files named in the assignment (forbid repo-wide search), include a 3-line literal report template, and set a hard circuit breaker: *"If an unlisted file needs edits or a command fails twice, run `nightshift report ... "BLOCKED: <reason>"` and stop."*
- **Opus:** Give the ticket, crew table, architectural boundaries, and verification contract; leave decomposition, investigation, and recovery judgement to the model, with command syntax in `nightshift help`.

---

### 5. How Gemini follows long instructions

Even with a 1M+ token window, Gemini's **active instruction compliance** drops noticeably past **~800–1,200 words** of dense system rules, and degrades sharply past **~2,500 words** when conditional branches nest (*"when X, unless Y, except on tool Z"*) or when tool history exceeds 30k–50k tokens.

- **What survives:**
  1. **Primacy and recency slots:** The first ~150 words (identity + crew table) and last ~150 words (the task + a short `NEVER` block adjacent to it).
  2. **Numbered state machines (5–7 steps)** with inline literal commands.
  3. **Markdown tables** for structured mappings (slots, roles, exit codes).
  4. **One literal 3-line template** for structured outputs (verdicts/reports).
- **What dies first:** Rules buried mid-paragraph after semicolons (`foreman.md` L17/L19), retroactive negations (*"the line above does not apply to you"*—the model still primes on the negated tool names), and rules for absent roles.

---

### 6. Anything in §3 that is wrong or out of date

1. **"Nobody publishes a foreman/coder/tester prompt set" is wrong.** MetaGPT, ChatDev, CrewAI, and Microsoft's Magentic-One all publish explicit role-split prompt sets. What is missing in the literature is controlled length-ablation benchmarks on those prompts, not the prompts themselves.
2. **The `CLAUDE.md` 60–200 line rule is conflated with system prompts.** HumanLayer's guidance targets *repo context files* layered atop Claude Code's built-in ~3,000-word system prompt. That strengthens §4's conclusion: because Claude Code, Copilot, Codex, and `agy` already inject large native system prompts, Nightshift's role brief competes for attention from line 1 and must stay under ~250 words.

---

## Follow-up: repository and site layers

1. **Is the four-layer split right?**
   The ownership split (Shift facts → Role page → Repository → Site) is right, but **Site is mostly machine config, not prose**. Nearly every example in §7b's Site row (*"which tools exist, which model names work here, the proxy, never push to main, only GitHub is reachable"*) belongs in `<state root>/site.yaml` enforced by `start`, `spawn`, `land`, and `crewEnv`, leaving at most ~50 words of site prose. Also, the §7b table says layers are "rendered in this order" (putting Site last), contradicting the bullet below it that **Hard rules** render last.

2. **Are the budgets (150 words repo prose, 100 site, facts free) workable?**
   Yes, with three adjustments:
   - **Make 150 words the combined cap (`instructions.md` + `<role>.md`) per role**, and steer projects toward role-specific files (`tester.md`, `coder.md`) so shared prose does not crowd out the Tester's acceptance checklist.
   - **Do not strip all repo/site rules from `compact` mode:** Expecting the Foreman to copy repo conventions into every `assign` call is fragile. Allow a **30-word checklist** from `<role>.md` in compact panes.
   - **Guard "free" facts:** Restrict `config.yaml` facts (`build`, `test_one`, `lint`, `typecheck`, `protected_paths`) to single-line values, and have `nightshift claim` enforce `protected_paths` with exit code 4.

3. **Where should repository prose render relative to the loop and hard rules?**
   Render `# This repository` (and site rules) **before** `How a shift runs`, not between the loop and the hard rules:
   `Shift & Repo Facts → # This repository / site → How a shift runs → Hard rules`.
   Models treat numbered steps as an immediate execution sequence; placing repo rules *above* the loop primes the Foreman and Tester before they scope or verify, while **Hard rules** still win on recency at the bottom.

4. **What goes wrong if "only GitHub is reachable" is just a site-file sentence?**
   - **Compact panes never see it:** Because §7b drops prose in compact mode, a compact Coder or Tester will run `pip`, `cargo`, `npm`, or web fetches against blocked hosts and hang until the 900s `asg-timeout`.
   - **Standard panes still try network calls when builds fail:** Models reflexively reach for package managers or web search on missing-import errors despite prose rules.
   - **Enforce it in the tool:** Declare site constraints in `<state root>/site.yaml` so `crewEnv` sets offline/proxy variables (`PIP_NO_INDEX=1`, `CARGO_NET_OFFLINE=true`, `GOPROXY=off`, `HTTPS_PROXY`), CLI flags disable web search, and `doctor`/`start` validate allowed tools and models upfront.

# Review: what the crew is told, and why it struggles

Owner request, 17 Oct 2026: the Foreman's instructions came out as a wall of text; review everyone's instructions and how the whole thing works, compare with what others are doing, and make the brief depend on the model running it.

## 1. What each member receives today

Every pane gets one text, built by `roleBrief`: persona (`personas/<role>.yaml`, rendered as identity + three bullet lists), then `briefs/common.md`, then the role brief, then any repository instructions, then (Foreman only) the task. Claude gets it as a system-prompt file; Codex, Copilot and agy get it as the first prompt in argv.

| Role | Persona | common.md | Brief | Total words | Pages at 400 words |
|---|---|---|---|---|---|
| Foreman (Claude) | 493 | 552 | 2,652 | ~3,700 + ticket | 9+ |
| Foreman (Codex/Copilot) | 493 | 552 | 2,021 | ~3,100 + ticket | 8 |
| Coder (Claude) | 510 | 552 | 422 | ~1,500 | 4 |
| Coder (CLI) | 510 | 552 | 513 | ~1,600 | 4 |
| Tester | 430 | 552 | 544–630 | ~1,550 | 4 |
| Apprentice | 364 | 552 | 504 | ~1,400 | 3.5 |
| Advisor | 390 | 552 | 299–317 | ~1,250 | 3 |

The human's task comes **last**, after nine pages, and the opening message says "the task text is at the end of your role instructions". The most important content is in the worst place.

## 2. What is wrong

1. **It is a patch log, not a brief.** Every incident since 15 Oct added a clause (NGH-025, NGH-027, NGH-040, NGH-050, NGH-056, uplift, reserved Coder, worktrees, credits, compaction…). Paragraphs are 150–300 words long with no headings. Nothing was ever removed.
2. **One text for every crew.** The Foreman is told how to route to the Apprentice, summon a reserved Coder, read Copilot credits and drive agy panes, whether or not the crew has any of those. The work crew has no Apprentice, yet "route ordinary coding to Apprentice first" is its first rule. A model reading rules about a member that does not exist either ignores rules or invents the member.
3. **The same rule is stated three times.** The startup gate (one READY, then idle) is in `common.md`, in every role brief, and again in every persona's `how_you_work`. "Never edit repository files" appears in the Foreman persona, the Foreman brief and `common.md`.
4. **Hard-coded facts that are now wrong.** `apprentice.md`: "in slot 5 of the container" (the home and work crews put it in slot 3 or 4). `common.md`: the state root is `C:/nightshift/` (now per drive) and still describes a legacy root. The Foreman brief says to read Copilot credits by sending `/usage` (there is a usage file now).
5. **Ceremony around every message.** Sending one message is three commands (`msg send`, deliver, `msg sent`); receiving one is `ack` plus "act only on the stored body". A non-Claude member is driven through `agents-cli send`, `agents-cli wait --match`, `agents-cli tail`, then `ack`. The Copilot panes in today's shift spent their first minutes failing shell permission prompts on exactly these commands. The protocol is sound; the number of steps a model must get right in order is not.
6. **The Foreman polls.** It is told to "glance at every member every couple of minutes with `agents-cli tail`" and to `agents-cli wait` with timeouts. Nothing blocks until something happens on the bus, so the Foreman burns context watching panes, and members wait on the Foreman noticing them.
7. **No map of the window.** The Foreman is told about reserved slots, free slots, the Ops pane and `spawn --slot`, but nothing prints the slot map. Today's Foreman could not count the slots and put the Decompiler in the Ops pane.
8. **Protocol and persona are entangled.** Personas were meant to be "who you are" and briefs "the protocol", but personas now carry uplift rules, 900-second timeouts and `--coding-asg` flags.
9. **It contradicts itself.** The Foreman persona says "then end the shift yourself"; the Foreman brief says "do not close the shift yourself unless the human asks; the warden closes an idle shift". Three hundred words of the brief explain `down`, `recovery.json`, `floor.json` and `cleaner` failure handling that the Foreman never runs during a shift. `tasks-cli note` is mandated four separate times. (Argon review, 17 Oct.)
10. **Non-Claude members get the Claude text first.** `common.md` opens with "Use native ListAgents and SendMessage" and each `-cli` brief then says "the line above does not apply to you". The Copilot Foreman brief still contains Claude-only compaction advice.

## 3. What others have found

Searched 17 Oct 2026. Reddit cannot be fetched from here (it blocks the crawler), so Reddit experience reaches this only through the write-ups that quote it; the rest is first-hand posts and papers.

- **Shorter wins, and the drop-off is steep.** One team cut a 2,000-line persona to 300 lines after finding it took ~80% of the context; they report 75% fewer tokens and much faster turns. What they removed: examples, inline explanation, "how"; what they kept: identity, the job, success criteria. Community guidance for `CLAUDE.md` converges on under 200 lines, with the better teams near 60, and reports compliance falling past ~300 lines. The pruning test: "would removing this line cause a mistake? If not, cut it." ([niksacdev](https://github.com/niksacdev/multi-agent-system/blob/main/docs/agent-based-development.md), [HumanLayer](https://www.humanlayer.dev/blog/writing-a-good-claude-md), [memx](https://memx.app/blog/claude-md-best-practices/), [buildthisnow](https://www.buildthisnow.com/blog/tools/claude-md-best-practices))
- **The orchestrator must not do the work.** Repeated independently: the system only stabilised once the lead was forbidden from writing code; when it starts implementing, tell it to delegate or wait. ([dev.to lessons](https://dev.to/jenueldev/your-ai-agents-are-not-a-team-yet-7-orchestration-lessons-from-multi-agent-failures-2lf3), [30 tips](https://getpushtoprod.substack.com/p/30-tips-for-claude-code-agent-teams), [Builder.io](https://www.builder.io/blog/claude-agent-teams-explained-what-it-is-and-how-to-actually-use-it))
- **Delegation briefs have a fixed shape.** Anthropic's lead-agent lesson: each subagent brief states the objective, the output format, the tools to use, and where the task ends; vague briefs caused duplicated work and gaps, and over-long updates between agents distracted them. Teammates start with nothing but the spawn prompt, so the context they need goes in the assignment, not in a shared history. ([Anthropic](https://www.anthropic.com/engineering/multi-agent-research-system), [30 tips](https://getpushtoprod.substack.com/p/30-tips-for-claude-code-agent-teams))
- **Tell the lead how much effort the job deserves.** Early versions spawned 50 subagents for simple questions; explicit effort rules in the prompt fixed it. ([Anthropic](https://www.anthropic.com/engineering/multi-agent-research-system))
- **Rules that can be code should be code.** Using model judgement for what an `if` can decide makes cost and behaviour unpredictable; a ten-agent system without deterministic control is less dependable than one agent in a strict loop. Verification must sit outside the agent's own confidence ("implemented successfully" appears without evidence). ([dev.to lessons](https://dev.to/jenueldev/your-ai-agents-are-not-a-team-yet-7-orchestration-lessons-from-multi-agent-failures-2lf3))
- **Reports should be structured.** Task, artifact, evidence, commands run, unresolved risks, confidence. The orchestrator consumes those, not prose. ([AgentMesh / literature](https://arxiv.org/pdf/2507.19902))
- **Same-model reviewers agree with the author.** 18 of 30 identical agents picked the same branch name; a reviewer on the same model tends to approve the same misunderstanding. A Tester on a different model is a feature. ([dev.to lessons](https://dev.to/jenueldev/your-ai-agents-are-not-a-team-yet-7-orchestration-lessons-from-multi-agent-failures-2lf3))
- **Loops need a detector.** Agents repeated the same failed fix for hours; the fix was a human stepping in, and the proposed guard is "track attempted fixes, flag a repeat, escalate". ([niksacdev](https://github.com/niksacdev/multi-agent-system/blob/main/docs/agent-based-development.md))
- **Native teams push events, not polls.** Claude Code agent teams deliver messages and completion notices to the lead without polling; the lead is told when a task finishes. That is the model to copy for the bus. Keep members in separate files or directories; same-file edits and heavy dependencies cost more than the parallelism gains. More than three or four teammates feels like overkill. ([30 tips](https://getpushtoprod.substack.com/p/30-tips-for-claude-code-agent-teams), [Claude Code docs](https://code.claude.com/docs/en/agent-teams))
- **Role-split prompt sets are published; length is not benchmarked.** MetaGPT, ChatDev, CrewAI and Microsoft's Magentic-One all publish orchestrator/coder/reviewer/tester prompts. What is missing is a controlled comparison of prompt length against outcome; the length figures above are one team's anecdote. Prompt wording is known to be high-variance, so changes should be tested, not assumed.
- **The `CLAUDE.md` figures are about repo context files, not system prompts.** They sit on top of Claude Code's own ~3,000-word system prompt. Every tool Nightshift drives (Claude Code, Copilot, Codex, agy) already injects a large native prompt, so the role brief competes for attention from its first line; that argues for the 250-word target, not against it.

Everything Nightshift got wrong is on that list: too long, orchestrator rules in prose, polling, no loop detector, and verification described rather than enforced. The things Nightshift already has right are also there: a separate Tester, file claims, evidence-bound builds, structured verdicts.

## 4. Principles for the rewrite

- **The tool enforces; the brief orients.** If the binary can make a wrong move impossible (claims already do for files) or print the right next step when a command fails (`land` exit 4 should say "assign the conflict to Coder"), the rule leaves the brief.
- **One page per role.** Identity in three lines, the loop in numbered steps, hard rules in a short list. Target: Foreman ≈ 250 words plus the task; members ≈ 120.
- **Say only what applies.** The launcher knows the crew. Each optional member or feature (Apprentice, reserved Coder, Advisor, Copilot credits, non-Claude panes, worktree, queue) adds one or two lines to the Foreman brief only when present.
- **Facts come from the launcher.** A short "Crew" table at the top: slot, role, tool, who is asleep, which pane is the human's; worktree, target branch, build command. No slot numbers or paths in prose.
- **Task first.**
- **Reference, not recital.** Exact syntax and edge cases live in `nightshift help <command>` and in each command's failure output, not in the brief.
- **Personas are identity only.** Three or four lines. All rules move to the brief.

## 5. The Foreman brief, as it should read

```
# Foreman: shift <name>, ticket <id>

Task:
<ticket text>

Crew (slot, role, tool):
  1  Foreman   you (Claude)
  3  Coder     Copilot, Gemini Flash
  4  Advisor   Claude, asleep until you consult it
  5  Tester    Copilot, Gemini Flash
  6  Ops pane  the human's, leave it
Worktree <dir>, branch shift/<id>, lands on <branch>. Build: <cmd>.

How a shift runs:
1. Read the ticket and the code. Split the work into assignments, each with named files and what done looks like.
2. nightshift assign <shift> Coder "<assignment>", one per assignment; they run in parallel.
3. nightshift wait <shift> returns when a member reports, asks, times out, or dies. Read it, decide, repeat. Between events keep investigating and scoping the next assignment; never fill the time by touching the code.
4. Read the diff against the assignment. Then nightshift assign <shift> Tester "verify <asg>: <what changed>". A TESTED-FAIL goes back with nightshift note <shift> Coder "<the verdict>". On TESTED-PASS, nightshift commit <shift> <asg> makes the commit and has Tester run the full build on it (land refuses anything else).
5. Stuck? nightshift consult <shift> "<question>". More hands? nightshift spawn <shift> --role Coder2 (it picks a free slot); dismiss it when done.
6. nightshift land <shift>, then nightshift result <shift> done --stage tested, then nightshift queue next. An empty queue means stop and wait; do not run down.

Rules: never edit repository files or run the suite yourself; never close the ticket; ask the human only when the ticket is wrong or a decision can't be undone; one line to the human per milestone.
Every command says what to do next when it fails. nightshift help <command> has the detail.
```

A member's brief is half that: who you are and who leads, `nightshift ready`, wait for an assignment, claim before you edit, `nightshift report` when done (first line the verdict or the summary, then files, evidence, what was left out), never commit.

## 6. Commands that make the short brief possible

The prose cannot shrink until these exist, so they come first.

| Command | Replaces | Does |
|---|---|---|
| `nightshift assign SHIFT ROLE "text" [--asg ID]` | `msg send` + SendMessage / `agents-cli send` + `msg sent` | Records, delivers by whatever the member's tool needs, marks sent, prints the asg. |
| `nightshift report SHIFT ROLE "text" [--asg ID]` | the same three steps from the member's side | One call; the first line is the verdict/summary. |
| `nightshift ready SHIFT ROLE` | the READY paragraph | Records READY; nothing else to get right. |
| `nightshift wait SHIFT [--for ROLE] [--timeout]` | `agents-cli tail` polling and `agents-cli wait --match` | Blocks until the next bus event that matters to the caller (ready, report, question, member died, build finished) and prints it with the stored body. `assign` waits for the member's READY itself, so the Foreman never dispatches into a pane that is still starting. |
| `nightshift note SHIFT ROLE "text"` | `msg send --kind note` plus typed delivery | The Foreman-to-member path `assign` does not cover: a Tester verdict relayed to the Coder, an answer to a question. (Mistral review.) |
| `nightshift uplift SHIFT ASG --reason R` | the ~150-word uplift paragraph | Checks the trigger evidence and released claims, records, starts the reserved Coder if needed and delivers, in one call; today every step is model-executed. (Mistral review.) |
| `nightshift commit SHIFT ASG` | `git commit` in the brief | Commits exactly the Tester-passed revision and refuses a tree that differs from it; a raw git command has no place on the one page. (Mistral review.) |
| `nightshift result SHIFT done\|blocked --stage RUNG --summary "..."` | the result.json schema paragraph | Writes result.json itself, attaching the bus, build records, uplift counts and Copilot usage, so the Foreman never hand-writes JSON. |
| `nightshift status` slot map (humans) | the slot paragraphs | `spawn` already picks a free, non-reserved, non-Ops slot, so the Foreman needs no slot command; the map is for people. |
| Failure output on `land`, `build`, `claim`, `spawn` | the exit-code paragraphs | Each says what to do next in one line. |

`ack` stays but becomes implicit: `wait` prints the stored body, which is what `ack` existed to guarantee.

**Delivery becomes pull, not push.** Today the body of a message is typed into the other pane, which races with the model's own typing, permission prompts and scrollback, and non-Claude members are read back by scraping their screen (`agents-cli wait --match`, `tail`). Instead every member blocks in the same `nightshift wait` the Foreman uses, and `wait` prints the stored body when a message arrives. What gets typed into a pane is a one-line nudge whose only job is to interrupt; the content always comes from SQLite. Panes become displays, the bus is the only transport, the screen-scraping and the old-marker bug class go away, and a member blocked in `wait` is trivially respawnable. (Mistral review.)

**Failures are events the tool raises; the brief says what to do about each, in one line.** The tool detects what can be counted or timed: `wait` reports `member-died <role>` (the launcher sees the pane exit) and `asg-timeout <asg>` (the 900-second rule becomes a bus event instead of a clock the Foreman watches); `build` refuses a fourth consecutive failing build on one assignment with the same error and prints `loop detected: escalate or report BLOCKED`; `nightshift respawn SHIFT ROLE` restarts the slot with the active assignment as the first prompt; `nightshift handover SHIFT` writes handover.md from live state before a member dies of context. The brief then needs four lines: on member-died, respawn and note it; on loop-detected, stop and report BLOCKED with the evidence, never retry the same fix; on asg-timeout, release claims and re-dispatch with the prior reports; on context exhaustion, run handover and stop. (Mistral review.)

**`status` stays out of the Foreman's vocabulary.** A command the brief names gets used; a model told "status shows who is running" polls it instead of blocking in `wait`, and cheap models most of all. (Mistral review.)

**Missing tools are the human's problem, loudly.** A specialist that needs an external tool (the Decompiler needs Ghidra; a future role might need a specific SDK) checks for it first and reports `<ROLE>-BLOCKED: missing tool <name>` before doing anything else; it never substitutes, scripts around it, or installs. Better still, the role declares `tools: [ghidra]` in its crew entry and `spawn` refuses to start it when the tool is not on PATH, so the gap is reported before a pane is spent on it (owner, 17 Oct).

## 7. Tuning the brief to the model

The same brief is wrong for Opus with a 1M context and for a Flash pane on a 30-credit budget. The brief is generated from one set of shift facts (role, crew table, worktree and build command, which features are on) in one of two modes, chosen per role in the crew YAML and defaulted from the model name; both stay under 250 words. (Three prose tiers were proposed first; Argon's review pointed out that would recreate the `-cli.md` maintenance trap and that a *longer* brief for a cheap pane costs it on every turn.)

- **standard**, for frontier models (Opus, Sol, Argon): the page above. Ticket, crew table, the loop, the verification contract; decomposition and recovery are left to the model, syntax to `nightshift help`.
- **compact**, for small or cheap models (Flash, Luna, Terra on low; any Copilot pane on a tight credit share): no discretion and no explanation. A four-step checklist with the commands filled in, edits confined to the files named in the assignment, a three-line literal report template, and a circuit breaker: "if an unlisted file needs editing or a command fails twice, `nightshift report ... "BLOCKED: <reason>"` and stop."

Context size decides only whether reference material is inlined or left behind `nightshift help`, and caps the ticket text the Foreman passes into an assignment.

Two corrections from the Mistral review. The mode table has to be concrete and shipped with the code, not "defaulted from the model name": compact for Flash, Luna, Terra on low, any Copilot pane on a credit share, and Mistral or small Codex panes; standard for Opus, Sonnet, Sol, Astra, Argon; the crew YAML can override per role. And compact should be held to about 150 words, not 250: a credit-capped pane pays for the brief on every turn, and past ~150 words of checklist a small model's step ordering drifts. If a step does not fit in 150 words, the answer is a command that does it, not a longer brief. Compact mode phrases every rule as an action ("your only action before the first assignment is `nightshift ready`"), never as a prohibition, and carries the inbound checklist literally: nudge arrives, `wait` prints the body, act on the printed body only.

Also noted by Mistral: the renderer today appends the task *last* (`roleBrief` in `launcher_crew.go`), so "task first" is a change to the code, not only to the prose.

Effort scaling belongs here too: the Foreman brief states how many members this ticket deserves (the ticket's size field or the owner's `--effort` flag), so a one-file fix does not get a five-pane crew.

## 7b. Repository and site layers (owner, 17 Oct)

Generating the brief in Go covers what is the same for every project. It must not stop a project saying how it is built, tested and reviewed, and it must not stop a site (the work machine) adding rules that hold for every project there. Today `roleBrief` already appends a `# Repository` section from `<repo>/.nightshift/config.yaml` (the build command), `instructions.md` (all roles) and `<role>.md` (one role). The redesign keeps that and gives it a shape and a budget.

Four layers, rendered in this order, each with a word budget the renderer enforces:

| Layer | Where | Who writes it | What belongs there | Budget |
|---|---|---|---|---|
| Shift facts | generated | the launcher | task, crew table, worktree, branch, identities | fixed |
| Role page | built in (`briefs/`) | Nightshift | the loop, hard rules | 250 / 150 |
| Repository | `<repo>/.nightshift/` | the project | `config.yaml`: build, test, lint and typecheck commands, target branch, test file pattern, protected paths. `instructions.md`: conventions that differ from the obvious (how to run one test, fixture rules, review checklist, what never to touch). `<role>.md`: a rule for one role (the Tester's acceptance checklist, the Coder's style rules). | 150 words of prose per role; facts are free |
| Site | `<state root>/site.yaml` (settings the tool enforces) and `<state root>/instructions.md` (prose) | the machine's owner | settings: reachable hosts, proxy, offline package managers, allowed tools and model names, protected branches. Prose: the few rules that cannot be a setting. | settings free; 50 words of prose |

Rules for the layers (the last three are from Argon's follow-up review):

- **Facts go in the table, prose goes under its own heading.** The repository's commands land in the "Your shift" table next to the worktree (`Build:`, `Test one:`, `Lint:`), where every mode sees them, including compact. Facts are single-line values; `protected_paths` is enforced by `claim` (exit 4), not read as advice.
- **Order on the page: facts, then `# This repository` and site prose, then the loop, then the hard rules.** Models treat numbered steps as the thing to start doing, so project rules go above the loop where they prime scoping and verification; the hard rules stay last and win on recency.
- **The site layer is mostly settings, not prose.** "Only GitHub is reachable" as a sentence fails exactly where it matters: a compact pane never sees it, and a standard pane still reaches for `pip` or a web search when a build fails. So `site.yaml` makes the tool do it: `crewEnv` sets `GOPROXY=off`, `PIP_NO_INDEX=1`, `CARGO_NET_OFFLINE=true` and the proxy variables, the CLI flags turn web search off, and `doctor`/`start` reject tools and model names the site does not allow, before a pane is spent. The prose file keeps the two or three rules that cannot be a setting.
- **Compact mode gets the facts plus a 30-word role checklist.** Expecting the Foreman to copy project conventions into every assignment is fragile; a compact Tester gets the project's acceptance checklist from `.nightshift/tester.md`, cut to 30 words, and nothing else of the prose.
- **The 150 words are a combined cap** (`instructions.md` plus `<role>.md`) per role, so shared prose cannot crowd out the Tester's checklist; projects are steered to the role files.
- **Long material is a file the assignment points at, not brief text.** A twenty-line testing guide lives at `.nightshift/testing.md`; the Foreman's assignment says "read .nightshift/testing.md first" when it matters. The budget makes this the path of least resistance.
- **Lower layers cannot override the hard rules.** A repository may add rules; it cannot remove "never commit", "claim before you edit" or "act on the stored body". The renderer puts the hard rules last, after the repository section, so they also win on recency.
- **Over budget is a warning you see before the shift starts.** `nightshift doctor --cwd <repo>` renders every role's brief for that repository and reports the word count per layer; `start` prints the same warning and continues, so a bloated `.nightshift/` is caught the first time, not after a bad shift.

This is also where the work-versus-home difference lives: the same binary, the same role pages, and a site file on each machine that says what is different there.

## 8. How to check it

1. Render every crew's briefs with `start --dry-run` and read them as a human would; the goldens record them.
2. One small real ticket per crew shape: all-Claude, Claude Foreman with Copilot members, Codex crew. Compare bus length, time to first dispatch, number of failed shell calls, and whether the result reached the tested rung.
3. Keep the old briefs under `briefs/legacy/` for one release so a crew can be started with them if the new ones regress.

## 9. Reviews

Two second opinions were taken on 17 Oct and folded in above: Gemini 4 Argon (`instructions-review-argon.md`) and Mistral Large 4 (`instructions-review-mistral.md`). Both approved the direction. Argon fixed the verification sequence and the brief modes; Mistral added the pull-based delivery, the failure events, the `note`, `uplift` and `commit` commands, the idle-time rule and the 150-word compact limit. Both reviewers, asked how their own models follow long instructions, gave the same answer: numbered steps with the literal command inline, short "never" lists next to the task, tables and literal templates survive; rules buried mid-paragraph, retroactive negations ("the line above does not apply to you"), nested conditionals and rules about absent roles die, and both put the fall-off well under 1,500 words.

## 10. Decisions for the owner

- Commands first (§6), then the briefs: agreed 17 Oct.
- Retire `foreman-agy-coder.md` and the `-cli` brief variants in favour of per-tool lines generated by the launcher: done in NGH-063.
- Acceptance test: the work crew, on the work machine, as part of the rollout (§12).
- Detail tiers in §7: two modes, standard and compact (Argon review).
- Warden inside the shift, and it is the Accountant: agreed 18 Oct (§11).

## 11. The warden runs inside the shift, and it is the Accountant (owner, 18 Oct)

What happened on 17-18 Oct decides this: a Coder's report sat unacked for nine hours because one nudge was lost and nothing looked; a Tester's agy pool ran to 2% unseen; a gate run stalled on blockers nobody raised. Every one of those is a clock or a counter, and clocks and counters belong to a program, not to the Foreman and not to a separate floor scheduler that may not exist on a colleague's machine.

**One patrol process per shift.** `start` launches it as a hidden child of the launcher (or as the Ops pane's own loop) and it dies with the shift. It knows the shift because it has `crew.json`: members, pids, tools, budget. Its loop, every 30 seconds:

1. **Liveness.** A member whose pid is gone raises `member-died <role>` (already built in NGH-061; the warden becomes the thing that runs that check rather than the Foreman's `wait`).
2. **Stalls.** An assignment delivered 15 minutes ago with no report raises `asg-timeout` (NGH-061). An inbound row unacked for 10 minutes whose recipient is alive gets a fresh nudge and a `nudge-retry` line on the bus; the third retry raises `member-stalled`, which goes to the owner as well as the Foreman (NGH-065).
3. **Costs.** It peers into each pane with the same screen read `agents-cli tail` uses, and parses the pane's own footer: Copilot prints "Session: N AIC used", Claude at work prints the running cost in its status line, agy and Codex print context left. Where a file is better than the screen it uses the file: Copilot's `--usage-output-file` at exit, Claude's transcript usage, Codex's rollout counts (NGH-048). Each sample goes to `<state_dir>/<Role>-usage.json` with the time, the figure, and where it came from; the shift total is the sum.
4. **Budget.** Confirmed at work 18 Oct: the Claude status line shows a dollar figure under Bedrock billing, and Copilot shows AI credits, which are not dollars and do not convert. So a crew carries two counters in two units, `budget_usd` for Claude/Bedrock/API panes and `max_credits` for Copilot panes (tokens for a subscription pane with no price), the warden keeps each total in its own unit, and crossing either one trips the warning and the stop. At 80% the warden raises `budget-warning` to the Foreman; at 100% it does what the NGH-034 decision said: interrupt in-flight turns, run `handover`, write `paused` to the bus and `result.json`, and leave the shift resumable. No model judgement anywhere in that path.

**Peering is the primary source at work, by design.** The work machine shows cost in the Claude status line and Copilot shows credits in its footer, so the warden reads what a person would read. Nightshift's own status line (`nightshift statusline`, already installed for agy and Copilot panes) is extended to Claude so a colleague without a custom status line still shows cost; it is handed to each pane through a shift-local settings file, never by editing anyone's `~/.claude/settings.json`. The one thing to test at work first: that the Claude status-line JSON carries a cost figure under Bedrock billing, not only token counts. If it does not, the warden falls back to tokens and the budget unit for that crew is tokens.

**What the Foreman sees.** Nothing new to do: `budget-warning`, `budget-stop`, `member-stalled` arrive through `wait` like any other event, each with its one-line rule in the brief. The Foreman never reads a cost itself.

**What this replaces.** The floor-level `warden` subcommand run by hand or on a schedule, the Foreman's "glance at every member every couple of minutes", and the separate Accountant role of NGH-034, which was a design for a pane that watches other panes; the warden is that watcher, as code.

## 12. Rollout at work (owner, 18 Oct)

Order of work after NGH-065: the warden (§11); then at work: `scripts\update.cmd`, `nightshift init`, a `site.yaml` for that machine (GitHub-only, Bedrock model names, proxy), the Claude status-line check above, and one small real ticket on the work crew with the one-page briefs. That run is the acceptance test §8 asked for; NGH-064's home comparison is parked until then.

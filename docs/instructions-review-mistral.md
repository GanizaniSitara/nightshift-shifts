# Review of `docs/instructions-review.md` (Mistral Large 4)

Model attestation: harness declares Mistral Large 4 (`mistral/mistral-large-4`) under OpenCode.

**Verdict:** Approve, with four additions Argon missed and two pushbacks. The architecture is right — ceremony into CLI primitives, one page per role, task first, personas as identity only — but the document contradicts its own renderer on task order, §7's modes are wired to nothing, §5 has no instruction for the Foreman's idle time, and the Foreman→member note path has no command. Fix those, then build.

## 1. What Argon missed or got wrong

1. The renderer contradicts the document: `roleBrief` appends `# Task` last (`launcher_crew.go:1171`) and the shipped example shows it last, while §4 says "Task first". Argon reviewed the §5 target text, never the renderer. Move the task marker to just after the crew table — primacy/recency is Argon's own argument — or drop "Task first".
2. §7's two modes are not wired: no crew YAML has a mode field, no code selects compact vs standard, the model-name defaulting table is undefined. The Codex crew (`crews/codex.yaml`: Terra Coder/Tester, Luna Apprentice) cannot get compact mode. §7 designs a mechanism that doesn't exist.
3. §5 has no idle-time instruction. The current brief's "while assignments are out, keep doing your own work" is load-bearing; §5's step 3 is only "wait returns, read it, decide, repeat". A Foreman told only to wait will wait — or start "helping" with the code. Add: "between events, keep investigating and scoping the next assignment."
4. The Foreman→member note path has no command: `assign` and `report` don't cover relaying a TESTED-FAIL or answering a question, and §5's step 4 never says how a FAIL reaches the Coder. Today that's `msg send --kind note` plus typed delivery — the ceremony §6 exists to remove.

Pushback on Argon:

- Argon's step-4 rewrite leads with the Foreman running `git commit` — a raw git command in the one-page brief, against §4's "reference, not recital". Take Argon's own parenthesis: `land` (or a `nightshift commit`) creates the commit and refuses a tree that doesn't match the TESTED-PASS revision.
- Argon said repeating a core invariant in identity plus a NEVER list "helps compliance"; Argon's own missing-item #1 (persona: "end the shift yourself"; brief: "do not close the shift yourself") is what that repetition produced — a direct conflict. Duplication of invariants is fine; duplication of rules is how contradictions accrete.

## 2. What a Mistral or Codex crew member needs that §5/§7 don't give

1. The model→mode table, concretely: "defaulted from the model name" needs a published table (Flash, Luna, Terra → compact; frontier → standard), and a Mistral or Codex pane on a credit share must default to compact. Without it a cheap pane gets discretion it can't afford.
2. The inbound protocol as a numbered checklist, including ack: header typed into the window → `nightshift ack <shift> <identity> <id>` → act on the printed stored body, never the typed text. The `-cli` briefs state this in prose; compact mode's checklist must carry the ack step literally — acting on window text is the step cheap models skip (§2 #5).
3. A positive definition of "idle": "remain completely idle" is a prohibition, and coding models fill idle by pre-reading and planning. Compact mode needs a hard numbered rule: "before your first assignment your only action is `nightshift ready`; do not read files, plan, or explore."
4. Filled-in identity and commands, including the member's own identity (`<shift>-coder`) and the literal report template.

Most likely to break: for a member, editing outside the named files or without claiming — coding models are pulled to "just fix it", so make the compact circuit breaker the default for those models. For a Foreman, the never-edit rule: an orchestrator with a shell and a slow crew will fix the failing test itself. §5 carries the prohibition but no positive alternative activity (point 3), which is what makes it stick.

## 3. The six commands: a seventh, and one that should not exist

Add `nightshift uplift <shift> <coding-asg> --reason ...`. The uplift paragraph is the densest remaining prose in the Foreman brief (~150 words: guarded message, same-asg, stored evidence, released claims, GUIDANCE/TAKEOVER relay) and the most error-prone procedure; today uplift is a msg kind, not a command, so every step is model-executed. One command that validates trigger evidence, checks released claims, records and delivers removes the most remaining prose of anything on the list. Also missing: the note path — `assign --kind note` or `nightshift note`.

Should not exist as a model-facing command: the slot map. Argon's argument was that it's unnecessary; the sharper one is that a command in the Foreman's vocabulary invites use — a model told "`nightshift status` shows who is running" will poll it instead of blocking in `wait`, and cheap models will do it most. Keep `status` for humans; keep it out of the Foreman brief. `ready` is the thinnest of the six but stays: the launcher cannot know when a model is actually ready to receive work.

## 4. The message protocol

The core is right and load-bearing: SQLite as source of truth, act only on the stored body, ack, the bus as the complete record. That's what makes messages recoverable and kills the old-marker mismatch the Foreman brief warns about. The delivery half is wrong: typing the body into the other pane is a lossy side channel racing with the model's own typing, permission prompts and scrollback — hence that warning, and hence driving non-`claude` members through `agents-cli send`/`wait --match`/`tail`, which is screen-scraping a TUI.

Simpler reliable design: inbound pull, not push. The member blocks in `nightshift wait <shift> <role>` — the same primitive the Foreman uses — and the command prints the stored body on arrival. Delivery becomes a one-line typed nudge whose only job is to interrupt; the body always comes from SQLite. The bus is the only channel; panes become displays, not transports. The `agents-cli wait --match` screen-scrape and the old-marker bug class disappear, and a member blocked in `wait` is trivially respawnable. The one thing typed delivery buys — interrupting a member mid-work — the nudge line preserves.

## 5. Failure handling: minimum for brief and tool

Counted or timed conditions belong to the tool; judgement reactions belong to the brief, one line per tool-raised event.

Tool, always on:

1. Death: the launcher can see a pane exit. `wait` reports `member-died <role>` itself; `nightshift respawn <shift> <role>` respawns the slot and re-delivers the active assignment from the mailbox as the first prompt. Today that's a prose paragraph.
2. Loop: `builds/<n>.json` already records head/dirty/exit per build. After 3 consecutive failing builds on one asg with the same error signature, `build` refuses and prints "loop detected: escalate or report BLOCKED". §3 names this guard; the tool doesn't have it.
3. Context: `nightshift handover <shift>` writes handover.md from live state (active asgs, claims, waits); respawn reads it automatically. The never-/compact rule stays in the brief; the mechanism stops being prose.
4. Timeout: make the 900-second no-done rule a bus event (`asg-timeout <asg>`) that `wait` reports, instead of a clock the Foreman watches.

Brief, one line each: on `member-died`, respawn and note it on the ticket, don't silently re-scope; on `loop-detected`, stop and report BLOCKED with the evidence, never retry the same fix; on `asg-timeout`, release claims and re-dispatch with prior reports; on context exhaustion, run `handover`, stop, die cleanly — the replacement continues.

## 6. How Mistral models follow long operating instructions

Self-report, so anecdote — the evidence class §3's last bullet warns about. §8 must measure it.

Survives: numbered steps with literal commands inline (I run a literal command verbatim far more reliably than a described procedure — the current briefs bury commands in 150–300-word paragraphs, and that's where my errors come from); short hard "never" lists adjacent to the task; the first ~200 words (identity, crew table); literal output templates; tables; counting rules stated as numbers ("exactly one READY").

Dies: retroactive negations ("the line above does not apply to you" — I prime on the named tools and have watched myself follow the negated subject anyway; it's the worst sentence in the corpus); nested conditionals ("when X, unless Y, except Z"); rules about roles not in my crew (I invent the member); duplication in different words (I read it as emphasis and lose the distinction — which is how the persona/brief "end the shift" contradiction survives: when two authorities conflict I follow the more specific or more recent, not the more correct); prohibitions phrased as prohibitions (I follow "do X" far better than "don't do Y" — the compact circuit breaker is phrased as an action, which is why it works).

Length: I can't benchmark myself; my honest self-assessment is that active compliance drifts past roughly 1,500–2,000 words of dense rules and is unreliable past ~2,500 with nested conditionals — well before my context limit. The current Foreman brief (~3,700 words) is past reliable active recall, and the failure is structural, not random: I keep the beginning (identity) and the end (task) and drop the middle (protocol), which produces exactly the observed failures. The 250-word target is right; for compact mode 250 is generous — hold it to ~150, because a credit-capped pane pays for those tokens every turn and past ~150 words of checklist my step ordering drifts. If a step doesn't fit, the answer is a command that does it, not a longer brief.

# Review: what the crew is told, and why it struggles

Owner request, 17 Oct 2026: the Foreman's instructions came out as a wall of text; review everyone's instructions and how the whole thing works.

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
2. **One text for every crew.** The Foreman is told how to route to the Apprentice, summon a reserved Coder, read Copilot credits and drive agy panes, whether or not the crew has any of those. The work crew has no Apprentice, yet "route ordinary coding to Apprentice first" is its first rule. A model reading a rule about a member that does not exist either ignores rules or invents the member.
3. **The same rule is stated three times.** The startup gate (one READY, then idle) is in `common.md`, in every role brief, and again in every persona's `how_you_work`. "Never edit repository files" appears in the Foreman persona, the Foreman brief and `common.md`. Repetition does not make a rule stronger; it makes the text longer and the exceptions harder to find.
4. **Hard-coded facts that are now wrong.** `apprentice.md`: "in slot 5 of the container" (the home and work crews put it in slot 3 or 4). `common.md`: the state root is `C:/nightshift/` (now per drive) and still describes the legacy `C:/Data/nightshift/` root. The Foreman brief says the Foreman "reads the Copilot credits by sending /usage" (there is a usage file now).
5. **Ceremony around every message.** Sending one message is three commands (`msg send`, deliver, `msg sent`); receiving one is `ack` plus "act only on the stored body". A non-Claude member is driven through `agents-cli send`, `agents-cli wait --match`, `agents-cli tail`, then `ack`. The Copilot panes in today's shift spent their first minutes failing shell permission prompts on exactly these commands. The protocol is sound; the number of steps a model must get right in order is not.
6. **The Foreman polls.** It is told to "glance at every member every couple of minutes with `agents-cli tail`" and to `agents-cli wait` with timeouts. There is no command that blocks until something happens on the bus, so the Foreman burns context watching panes, and members wait on the Foreman noticing them (the asynchronicity complaint from this morning).
7. **No map of the window.** The Foreman is told about reserved slots, free slots, the Ops pane and `spawn --slot`, but nothing prints the slot map. Today's Foreman could not count the slots and put the Decompiler in the Ops pane.
8. **Protocol and persona are entangled.** Personas were meant to be "who you are" and briefs "the protocol", but personas now carry uplift rules, 900-second timeouts and `--coding-asg` flags. Editing one means editing both.
9. **Non-Claude members get the Claude text first.** `common.md` opens with "Use native ListAgents and SendMessage" and each `-cli` brief then says "the line above does not apply to you". The Copilot Foreman brief still contains Claude-only compaction advice.

## 3. Principles for the rewrite

- **One page per role.** Identity in three lines, the loop in numbered steps, hard rules in a short list, nothing else. Target: Foreman ≤ 700 words, members ≤ 350, plus the task.
- **Say only what applies.** The launcher knows the crew. Each optional member (Apprentice, reserved Coder, Advisor, Tester, Copilot panes, non-Claude panes, worktree) contributes its own short section to the Foreman brief only when present. Member briefs say which tool they run on and nothing about the others.
- **Facts come from the launcher, not the prose.** Slot numbers, identities, state folder, build command, target branch, credit shares and the member list are rendered as a short "Your shift" table at the top. No slot numbers or paths in the prose.
- **Reference, not recital.** Exact command syntax, exit codes, uplift rules, landing conflicts, compaction, recovery after respawn go into `<state_dir>/reference.md` (one file, headed sections). The brief says "see reference.md § Landing" at the point of need. A model opens a file when it needs it far more reliably than it recalls page six.
- **Task first.** The Foreman brief opens with the task, then the shift table, then the loop.
- **Fewer, fatter commands.** `nightshift report SHIFT ROLE --asg ID "text"` records, delivers (SendMessage or agents-cli as appropriate) and marks sent in one call; `nightshift wait SHIFT [--for ROLE] [--timeout]` blocks until the next bus event the Foreman cares about (a READY, a report, a death) instead of tail-polling; `nightshift slots SHIFT` prints the window map. Each removes a paragraph from every brief.
- **Personas are identity only.** Three or four lines of who you are and what good looks like. All rules move to the brief.

## 4. Proposed shape

**Foreman (every crew):** Task · Your shift (table) · The loop: 1 scope, 2 dispatch, 3 while assignments are out, 4 review, 5 verify, 6 commit and land, 7 report and queue next · Hard rules (8 lines) · Pointers into reference.md.
**Foreman sections added per crew:** Apprentice-first routing and uplift; reserved Coder and `summon`; Advisor and `consult`; Copilot credits; non-Claude members (how to send and read); worktree landing; live-shift queue.
**Coder / Apprentice:** Your shift · READY then wait · On an assignment: claim, edit, narrow check, report · Hard rules (5 lines).
**Tester:** Your shift · READY then wait · On a verification: read bus and diff, targeted tests via build, full build on the final commit, verdict format · Hard rules.
**Advisor / Decompiler / Researcher:** Your shift · When woken · What a good answer or report holds · Hard rules.
**common.md:** retired; its live content (identity, peers, never launch sessions, where state lives) becomes the "Your shift" table and two hard rules.

## 5. How to check it

1. Render every crew's briefs with `start --dry-run` and read them as a human would; the goldens record them.
2. One small real ticket per crew shape: all-Claude, Claude Foreman with Copilot members, Codex crew. Compare bus length, time to first dispatch, number of failed shell calls and whether the result reached the tested rung.
3. Keep the old briefs under `briefs/legacy/` for one release so a crew can be started with them if the new ones regress.

## 6. Decisions for the owner

- Agree the one-page targets and the reference-file split?
- Add the three commands (`report`, `wait`, `slots`) first, so the briefs can shrink around them, or rewrite the prose first?
- Retire `foreman-agy-coder.md` and the `-cli` brief variants in favour of per-tool sections generated by the launcher?
- Which crew shape is the acceptance test: the work crew (Claude Foreman, Copilot Coder and Tester) or the home crew?

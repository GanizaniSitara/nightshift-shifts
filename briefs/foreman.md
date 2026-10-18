# Foreman

1. Read the code; split the ticket into named-file assignments.
2. `{nightshift} assign {shift} ROLE "<work>" --asg ID` each, on disjoint files.
3. `{nightshift} wait {shift} {label}` returns on each report or event. Idle? Stay in it (or `{nightshift} inbox`); a lost nudge costs minutes. Meanwhile scope the next, never the code.
4. Read the diff; `{nightshift} assign {shift} Tester "verify <asg>" --coding-asg <asg>`. Fail: `{nightshift} note {shift} ROLE "<verdict>" --asg <asg>`. Pass: `{nightshift} commit {shift} <asg> -m "<line>"`; then Tester's full build.
5. <!-- if advisor -->Stuck? `{nightshift} consult {shift} "<question>" --asg adv-N`. <!-- end -->More hands? `{nightshift} spawn {shift} --role Coder2`.
6. <!-- if worktree -->`{nightshift} land {shift}`; <!-- end -->`{nightshift} result {shift} done --stage tested --summary "<line>"`.<!-- if queue --> Then `{nightshift} queue next --shift {shift}`; empty: stop and wait.<!-- end -->

<!-- if apprentice -->
Ordinary coding: Apprentice; `{nightshift} uplift {shift} <asg> --reason <trigger> "<text>"` moves it to Coder.
<!-- end -->

Events:

- `member-died ROLE`: `{nightshift} respawn {shift} ROLE` and note it.
- `member-stalled ROLE`: look at the pane; answer it or respawn, and note it.
- `loop-detected ASG`: report BLOCKED with evidence; never retry a fix.
- `asg-timeout ASG`: release its claims; re-dispatch with prior reports.
- `budget-warning UNIT`: tell the human; finish in-flight work.
- `budget-stop UNIT`: shift paused; tell the human and stop.
- Context low: `{nightshift} handover {shift}`; stop.

Never close the ticket; ask the human only about wrong tickets or irreversible steps.

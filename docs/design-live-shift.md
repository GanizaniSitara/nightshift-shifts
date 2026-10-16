# Design: a live shift you feed work to (draft for review)

Status: proposal, 16 Oct 2026. Not built. Reviewers: owner, AGY supervisor.

## Problem

Today a shift is one task: `nightshift start TICKET` opens a crew, the crew lands the work, the Foreman closes the
shift. To do five tasks you start five crews, and `nightshift loop` only runs a fixed list chosen up front. The owner
wants to keep one crew running on a repository and hand it work as it comes up, from one place, without juggling
windows.

## Proposal

### 1. One crew per repository, and `start` attaches

`nightshift start` run inside a repository (or with `--cwd`) looks for a live shift on that repository.

- None: start one, as today. The crew is chosen here (`--crew`, else the repo's `.nightshift/config.yaml`
  `default_crew`, else `default`) and stays fixed for the life of the shift.
- One live: do not start another. Bring its window to the front, focus the Foreman pane, and if a task was given,
  queue it (section 3). Starting twice never makes a second crew.

### 2. One window: the crew window has an Ops pane

The 2x4 container already has empty slots. Slot 8 becomes the **Ops pane**: a plain `cmd` in the shift's
worktree with `nightshift` on PATH and the shift name in its title, so `nightshift add`, `status`, `log --follow`,
`down` all work there without naming the shift. The owner therefore works in one window:

- the Foreman pane, to talk (natural language: "do TECH-230 next", "stop after this one", "what's queued?");
- the Ops pane, for exact commands.

No second terminal is needed. Another agent or a script can still use the CLI from anywhere.

### 3. The queue belongs to the shift

`<state_dir>/queue.jsonl`, append-only, one item per line: `{id, ts, from, ticket | task, state}` with state
`queued -> running -> landed | blocked | dropped`.

- `nightshift add [TICKET | "task text"] [--shift NAME]`: append an item. Without `--shift` it resolves the live
  shift for the current repository (section 1); error if none, with the `start` command to run.
- `nightshift queue [--shift NAME]`: list items and states.
- `nightshift queue drop ID` / `nightshift queue move ID --top`: change order before an item starts.
- Typing a task into the Foreman pane: the Foreman calls `nightshift add` itself, so pane and CLI feed one queue.

No crew on items: the crew belongs to the shift.

### 4. The Foreman runs the queue

When an item lands (or is blocked), the Foreman notes it on the ticket, marks the item, and takes the next queued
item instead of closing the shift. Each item gets its own branch off the target branch in the shift's worktree, so
items land one at a time, in order. Rules that already exist still apply per item: analysed tickets only (a
NOT-READY item is marked blocked with what is missing), Apprentice-first, Tester passes before land.

### 5. When the shift ends

- `nightshift down` (from the Ops pane, the Foreman on request, or anywhere with `--shift`).
- Idle timeout: queue empty and nothing added for `idle_minutes` (crew manifest, default 30): the Foreman writes the
  handover and closes. Members that are on demand (Advisor, Coder) are already not running while idle; the live
  members (Foreman, Apprentice, Tester) do nothing while waiting, so an idle crew costs nothing per turn.

### 6. What changes in code (Go)

- `start`: live-shift lookup per repository; attach (focus window, focus Foreman pane) instead of refusing.
- Layout: slot 8 is the Ops pane (title `<shift> ops`, cwd the worktree, env `NIGHTSHIFT_SHIFT=<name>`).
- New `add`, `queue` commands; `queue.jsonl` with a lock, like the mailbox.
- Foreman brief: queue loop, idle timeout, natural-language intake.
- `loop` and `gaffer` stay for unattended batches; this design does not need them.

## Open questions for review

1. One shift per repository, or allow two shifts on one repository when their file sets are disjoint?
2. Should a queued item with no ticket get a ticket created automatically (so every change is traceable)?
3. Idle timeout default: 30 minutes, or keep the shift until `down`?
4. Ops pane in slot 8, or a separate small window?

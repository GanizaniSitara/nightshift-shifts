# Design: a live shift you feed work to (draft for review)

Status: agreed design, 16 Oct 2026 (owner + AGY review). Not built yet.

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
- Idle timeout: queue empty and nothing added for `idle_minutes` (crew manifest, default 5): the Foreman writes the
  handover and closes. Members that are on demand (Advisor, Coder) are already not running while idle; the live
  members (Foreman, Apprentice, Tester) do nothing while waiting, so an idle crew costs nothing per turn.

### 6. What changes in code (Go)

- `start`: live-shift lookup per repository; attach (focus window, focus Foreman pane) instead of refusing.
- Layout: slot 8 is the Ops pane (title `<shift> ops`, cwd the worktree, env `NIGHTSHIFT_SHIFT=<name>`).
- New `add`, `queue` commands; `queue.jsonl` with a lock, like the mailbox.
- Foreman brief: queue loop, idle timeout, natural-language intake.
- `loop` and `gaffer` stay for unattended batches; this design does not need them.

## Decisions (owner with AGY review, 16 Oct 2026)

1. Strictly one live shift per repository; the warm crew works queue items one after another.
2. No ticket is created for a plain-text item; it runs straight from `queue.jsonl`.
3. Idle timeout 5 minutes (`idle_minutes: 5` default) once the queue is empty and nothing is running.
4. Ops pane in the 2x4 grid, slot 8 bottom right, with `NIGHTSHIFT_SHIFT` set.

## Implementation notes from review

a. `nightshift add` on an idle shift wakes the Foreman with a mailbox message delivered to its pane, so it notices
   the new item.
b. A deterministic step between items, `nightshift queue next` (run by the Foreman): resets the per-item records
   (result.json, claims, build evidence), checks out a fresh branch off the target branch from worktree.json, marks
   the item running and prints it.
c. The idle timeout is enforced by the warden (a timer), not by the Foreman, because an idle prompt cannot time
   itself out.

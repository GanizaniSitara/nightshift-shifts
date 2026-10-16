# Run Nightshift at work

## 1. What it is

The `claude-copilot` crew (`crews/claude-copilot.yaml`) divides work between Claude Code and GitHub Copilot:
- **Foreman**, **Advisor**, **Coder**, and **Tester** run on Claude Code with `claude-opus-5-5`. The Advisor remains dormant until Foreman asks. The Apprentice is the primary coder on Flash; the Coder is the senior buddy, used only when work is uplifted.
- **Apprentice** runs on GitHub Copilot CLI with `gemini-3.8-flash`, reasoning high, `long_context`, and a 30-credit cap.

The crew manifest's top-level `max_credits` is the shift's Copilot AI credit budget. Its share is passed to Copilot as `--max-ai-credits`, a soft cap per pane (a response can overshoot, then the next call is blocked). Change the budget in that one line of the manifest.

Claude panes keep the owner's global Claude theme and the common `<ticket> shift` title. Claude has no supported independent colour launch flag, so Nightshift starts each Claude pane without an opening prompt, accepts folder trust, sends `/color <role-colour>`, waits for Claude's `agent-color` transcript record, and only then sends the first role message. This barrier also applies to members spawned into empty slots. The hidden `--agent-color` flag is for Claude teammate sessions and is not used for ordinary crew panes.

## 2. Prerequisites

- **Claude Code CLI**: Installed and signed in.
- **GitHub Copilot CLI**: Installed globally via npm (under `%APPDATA%\npm`) and signed in (`copilot login`) with `gemini-3.8-flash` in its model catalog.
- **agents-cli**: On `PATH` (Foreman drives Copilot panes through it and the shift mailbox; no mesh).
- **nightshift.cmd**: On `PATH`. It runs a fixed Python 3.12 interpreter path configured inside `nightshift.cmd` (edit that line if your interpreter path differs).
- **Windows Terminal**: Microsoft Store package required. `start` installs the Nightshift Crew profile into `LocalState/settings.json`, saving a backup to `terminal-settings-before.json` in the shift folder (unpackaged/portable Terminal is not supported).
- **tasks-cli**: Required on `PATH` only when starting shifts with a ticket ID.
- **Target repository**: Must be a git repository (the shift provisions an isolated worktree and branch).

## 3. Starting a shift

Launch with a ticket ID (calls `tasks-cli get PROJ-123` first; requires `tasks-cli`):
```bash
nightshift start PROJ-123 --crew claude-copilot --cwd <repo>
```

Or launch with free-text task instructions (does not require `tasks-cli`):
```bash
nightshift start "<task text>" --crew claude-copilot --cwd <repo>
```

Preview with `--dry-run` first:
```bash
nightshift start PROJ-123 --crew claude-copilot --cwd <repo> --dry-run
```
Dry-run JSON prints command lines only for non-Claude panes:
- Verify the plan's top-level `max_credits` is 30 and that only `roles.Apprentice.command` contains `--max-ai-credits 30`.
- Verify top-level `model` is `claude-opus-5-5` for Claude panes (Foreman, Advisor, Coder, Tester).

## 4. What to expect

- **Layout**: One Windows Terminal window in a 2x4 grid with five crew panes and three empty slots.
- **Startup**: Every member except the dormant Advisor sends `READY` to Foreman.
- **Execution**: Foreman dispatches assignments; Tester verifies via `nightshift build`; Foreman commits and runs `nightshift land`.
- **Accounting**: Foreman records each Copilot pane's AI credits used in `result.json` (`usage` field), read from the pane's `/usage` or `<state_dir>/<label>-usage.json`.
- **Teardown**: Foreman ends with `nightshift down`.

## 5. Credit limits

Copilot's cap is a soft cap: an ongoing response can overshoot, after which further calls are blocked. If a Copilot pane hits its cap, it stops and Foreman reports it. Raise the shift budget `max_credits` in the manifest only deliberately.

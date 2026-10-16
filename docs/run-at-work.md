# Run Nightshift at work

## 1. What it is

The `work` crew (`crews/work.yaml`) is tailored for work machines where running five sessions consumes too much memory. It runs a lean 4-role crew in a 2x4 container:
- **Foreman**, **Advisor**, and **Tester** run on Claude Code with `claude-opus-5-5`. The Advisor remains dormant until Foreman consults it.
- **Coder** runs on GitHub Copilot CLI with `gemini-3.8-flash`, reasoning high, long-context tier (`context: long_context`), and permissions `skip`.
- There is no Apprentice; the Coder handles coding directly.

The crew manifest's top-level `max_credits` is the shift's Copilot AI credit budget. Since Coder is the only Copilot pane, its 30-credit share is passed to Copilot as `--max-ai-credits`, a soft cap per pane (a response can overshoot, then the next call is blocked). Change the budget via manifest or start-time override.

Claude panes keep the owner's global Claude theme and the common `<ticket> shift` title. Claude has no supported independent colour launch flag, so Nightshift starts each Claude pane without an opening prompt, accepts folder trust, sends `/color <role-colour>`, waits for Claude's `agent-color` transcript record, and only then sends the first role message. This barrier also applies to members spawned into empty slots.

## 2. Prerequisites

- **Claude Code CLI**: Installed and signed in.
- **GitHub Copilot CLI**: Installed globally via npm (under `%APPDATA%\npm`) and signed in (`copilot login`) with `gemini-3.8-flash` in its model catalog.
- **agents-cli**: On `PATH` (Foreman drives Copilot panes through it and the shift mailbox; no mesh).
- **nightshift**: On `PATH` (native binary or `nightshift.cmd`).
- **Windows Terminal**: Microsoft Store package required. `start` installs the Nightshift Crew profile into `LocalState/settings.json`, saving a backup to `terminal-settings-before.json` in the shift folder (unpackaged/portable Terminal is not supported).
- **tasks-cli**: Required on `PATH` only when starting shifts with a ticket ID.
- **Target repository**: Must be a git repository (the shift provisions an isolated worktree and branch).

## 3. Crew lookup and machine customization

When `--crew NAME` is specified, Nightshift resolves manifests using a three-tier lookup (first hit wins):
1. **Machine**: `<stateRoot>/crews/NAME.yaml` (default `C:\nightshift\crews\NAME.yaml`; honours `NIGHTSHIFT_STATE_ROOT`)
2. **Repo**: `<repoRoot>/.nightshift/crews/NAME.yaml`
3. **Built-in**: `crews/NAME.yaml` (asset directory or embedded)

### Customizing on the work machine

To adapt the crew locally without modifying repository files:
```cmd
copy crews\work.yaml C:\nightshift\crews\work.yaml
```
Edit `C:\nightshift\crews\work.yaml` to adjust tools, models, accounts, or budgets. Because machine manifests shadow repo and built-in manifests, `nightshift start ... --crew work` automatically picks your machine copy.

### Inspecting visible crews

Run `nightshift crews` to list all discovered manifests in JSON:
```bash
nightshift crews
```
For each manifest, the output shows `name`, `source` (`machine`, `repo`, or `built-in`), `path`, and `active` (`true` if this file wins `--crew NAME`, `false` if shadowed). Pass `--cwd <dir>` to inspect relative to another repository root.

### Direct manifest file (`--crew-file`)

To bypass lookup and load a manifest directly from a path:
```bash
nightshift start PROJ-123 --crew-file C:\nightshift\crews\work.yaml --cwd <repo>
```
`--crew` and `--crew-file` are mutually exclusive. Relative paths resolve against the current working directory.

## 4. Start-time overrides

You can override manifest settings on the command line at `start` time. Overrides are validated exactly like manifest settings and refused with a clear message if invalid:
- `--without ROLE` (repeatable): Drop a role from the crew (e.g. `--without Apprentice` when starting from a 5-role manifest). The `human_contact` role (Foreman) cannot be dropped.
- `--role ROLE=TOOL:MODEL[:REASONING]` (repeatable): Override a role's tool, model, and optional reasoning.
  Example: `--role Coder=copilot:gemini-3.8-flash:high`
- `--max-credits N`: Override the shift's Copilot budget.
  Example: `--max-credits 60`

When overrides or machine manifests are used, the effective configuration is recorded in `crew.json`, the bus `start` event, and dry-run plans.

## 5. Starting a shift

Launch with a ticket ID (calls `tasks-cli get PROJ-123` first; requires `tasks-cli`):
```bash
nightshift start PROJ-123 --crew work --cwd <repo>
```

Or launch with free-text task instructions (does not require `tasks-cli`):
```bash
nightshift start "<task text>" --crew work --cwd <repo>
```

Launch with overrides:
```bash
nightshift start PROJ-123 --crew work --max-credits 60 --cwd <repo>
```

Preview with `--dry-run` first:
```bash
nightshift start PROJ-123 --crew work --cwd <repo> --dry-run
```
Dry-run JSON prints command lines only for non-Claude panes:
- Verify the plan's top-level `max_credits` is 30 and that only `roles.Coder.command` contains `--max-ai-credits 30`.
- Verify top-level `model` is `claude-opus-5-5` for Claude panes (Foreman, Advisor, Tester).
- Verify any command-line overrides appear in the plan's `overrides` object.

## 6. What to expect

- **Layout**: One Windows Terminal window in a 2x4 grid with four crew panes (slots 1..4) and four empty slots (5..8) reserved for `nightshift spawn`.
- **Startup**: Every member except the dormant Advisor sends `READY` to Foreman.
- **Execution**: Foreman dispatches assignments; Coder executes; Tester verifies via `nightshift build`; Foreman commits and runs `nightshift land`.
- **Accounting**: Foreman records Copilot AI credits used in `result.json` (`usage` field), read from the pane's `/usage` or `<state_dir>/<label>-usage.json`.
- **Teardown**: Foreman ends with `nightshift down`.

## 7. Credit limits

Copilot's cap is a soft cap: an ongoing response can overshoot, after which further calls are blocked. If a Copilot pane hits its cap, it stops and Foreman reports it. Raise the shift budget `max_credits` in the manifest or with `--max-credits` only deliberately.

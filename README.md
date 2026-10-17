# Nightshift

Nightshift coordinates multi-role AI agent crews (Foreman, Advisor, Coder, Tester, Apprentice) working concurrently in Windows Terminal panes to execute development assignments within isolated Git worktrees, communicating through a durable SQLite mailbox and enforcing strict ownership claiming and verification gates.

## Prerequisites

- **Go**: Required to compile the `nightshift` binary.
- **Windows Terminal**: Microsoft Store package for running multi-pane crew layouts.
- **Agent CLIs**: Claude Code or other supported tool CLIs (GitHub Copilot CLI, Codex, Antigravity `agy`) installed and signed in.
- **agents-cli**: Must already be available on `PATH` for driving non-Claude terminal panes (not built or shipped here).
- **tasks-cli** *(optional)*: Required only when launching shifts with a ticket ID.

## Installation

1. Clone the repository to your machine.
2. Run the build script to compile the Go binary:
   ```cmd
   scripts\build.cmd
   ```
   By default, this builds into `\nightshift\bin\nightshift.exe` on the drive the repo is cloned on (for example `D:\nightshift\bin`), and the binary keeps its state in `\nightshift` on that same drive. The crew, brief and persona templates are copied beside it (`\nightshift\bin\crews` and so on); your own crew overrides go in `\nightshift\crews`, and `nightshift init` offers to refresh any that are older than the templates. You can also specify an alternate output directory:
   ```cmd
   scripts\build.cmd D:\somewhere
   ```
3. Add that `\nightshift\bin` folder (or your custom output directory) to your system `PATH`.
4. Verify your installation and inspect available crew manifests:
   ```cmd
   nightshift crews
   ```

## Repository Configuration

Each repository sets its own build and verification command in `.nightshift/config.yaml`. For example, this repository configures its build command to point at its own `verify.cmd`.

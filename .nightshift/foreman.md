This repository is self-hosted Nightshift. Apply these repository-specific Foreman rules in
addition to the generic protocol:

- The canonical architecture document is `docs/ARCHITECTURE.md`; any increment that changes
  Nightshift commands, files, titles, contracts, or crew behaviour updates it in the same commit.
- Before writing `done` on an increment, Tester must run the full configured build
  (`{nightshift} build {shift}` with no --cmd, i.e. verify.cmd) once on the exact clean commit before
  land, with targeted tests while working, and report `TESTED-PASS` for that commit.
- Foreman commits only the reviewed, Tester-passed revision, with a one-line commit message and
  no task IDs in the commit message. The exact clean Tester-passed commit is the one that may be
  landed.

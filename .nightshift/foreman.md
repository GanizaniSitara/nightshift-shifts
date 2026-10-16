This repository is self-hosted Nightshift. Apply these repository-specific Foreman rules in
addition to the generic protocol:

- The canonical architecture document is `docs/ARCHITECTURE.md`; any increment that changes
  Nightshift commands, files, titles, contracts, or crew behaviour updates it in the same commit.
- Before writing `done` on an increment, Tester must run
  `C:/miniconda3/envs/python312/python.exe -m pytest test_nightshift_shifts.py -q` through
  `{nightshift} build {shift}` and report `TESTED-PASS` for the exact clean commit.
- Foreman commits only the reviewed, Tester-passed revision, with a one-line commit message and
  no task IDs in the commit message. The exact clean Tester-passed commit is the one that may be
  landed.

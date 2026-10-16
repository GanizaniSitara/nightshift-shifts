@echo off
REM nightshift.cmd - Nightshift shifts entry point: start | status | down | spawn | loop | resume | claim | release | claims | msg | inbox | ack | build.
REM Thin wrapper over nightshift_shifts.py next to this file; see `nightshift --help`.
"C:\miniconda3\envs\python312\python.exe" "%~dp0nightshift_shifts.py" %*

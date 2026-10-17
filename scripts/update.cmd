@echo off
rem update.cmd - one-step refresh of a Nightshift install from this clone: pull the latest from GitHub,
rem build and deploy into <drive>\nightshift\bin (binary plus crew, brief and persona templates), then let
rem nightshift init refresh the state folders and any machine crew overrides that are older than the
rem templates. Ends by checking that the nightshift on PATH is the one just built.
rem   scripts\update.cmd          ask before replacing each machine crew override
rem   scripts\update.cmd --yes    replace them all without asking (old copies kept as NAME.yaml.old)
setlocal
cd /d "%~dp0.."
set "BIN=%~d0\nightshift\bin"

echo == Pulling
git pull --ff-only
if errorlevel 1 (
  echo Pull failed: fix the clone ^(local changes or a diverged branch^) and run update again.
  exit /b 1
)

echo == Building
call "%~dp0build.cmd"
if errorlevel 1 (
  echo Build failed; the installed nightshift is unchanged.
  exit /b 1
)

echo == Setting up
set "NIGHTSHIFT_TEXT=1"
"%BIN%\nightshift.exe" init %1
"%BIN%\nightshift.exe" -v

echo == Checking PATH
set "FIRST="
for /f "delims=" %%P in ('where nightshift.exe 2^>nul') do if not defined FIRST set "FIRST=%%P"
if not defined FIRST (
  echo nightshift is not on PATH: add %BIN% to PATH.
  exit /b 0
)
if /i "%FIRST%"=="%BIN%\nightshift.exe" (
  echo nightshift on PATH is %FIRST%: up to date.
) else (
  echo WARNING: the nightshift on PATH is %FIRST%, an older copy, not %BIN%\nightshift.exe.
  echo Delete or rename that file, or put %BIN% before its folder in PATH.
)
endlocal

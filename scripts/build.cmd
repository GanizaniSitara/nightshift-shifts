@echo off
setlocal
cd /d "%~dp0.."

set "OUTDIR=%~1"
set "DEPLOY="
if "%OUTDIR%"=="" (
  set "OUTDIR=%~d0\nightshift\bin"
  set "DEPLOY=1"
)
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

rem No GOEXE given: prefer a Go toolchain from the standard module cache (go.mod needs Go 1.25+), else go on PATH.
if "%GOEXE%"=="" for /d %%D in ("%USERPROFILE%\go\pkg\mod\golang.org\toolchain@v0.0.1-go1.2*.windows-amd64") do if exist "%%D\bin\go.exe" set "GOEXE=%%D\bin\go.exe"
if "%GOEXE%"=="" set "GOEXE=go"

set "GITPULL_SHA="
if exist ".gitpull.version" for /f "usebackq tokens=1" %%S in (".gitpull.version") do if not defined GITPULL_SHA set "GITPULL_SHA=%%S"

if defined GITPULL_SHA (
  "%GOEXE%" build -mod=readonly -trimpath -ldflags "-X main.buildCommit=%GITPULL_SHA%" -o "%OUTDIR%\nightshift.exe" ./cmd/nightshift
) else (
  "%GOEXE%" build -mod=readonly -trimpath -o "%OUTDIR%\nightshift.exe" ./cmd/nightshift
)
set "STATUS=%ERRORLEVEL%"
if not "%STATUS%"=="0" goto done

rem Deploy build: the templates go beside the binary as plain files (they are also embedded in it), mirrored so a
rem removed template does not linger. Machine overrides stay in <drive>\nightshift\crews; nightshift init checks them.
if defined DEPLOY for %%K in (crews briefs personas) do (
  robocopy "%%K" "%OUTDIR%\%%K" /MIR /NJH /NJS /NFL /NDL /NP >nul
  if errorlevel 8 set "STATUS=1"
)
if "%STATUS%"=="0" echo Built %OUTDIR%\nightshift.exe

:done
endlocal & exit /b %STATUS%

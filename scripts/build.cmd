@echo off
setlocal
cd /d "%~dp0.."

set "OUTDIR=%~1"
if "%OUTDIR%"=="" set "OUTDIR=C:\nightshift\bin"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

rem No GOEXE given: prefer a Go toolchain from the standard module cache (go.mod needs Go 1.25+), else go on PATH.
if "%GOEXE%"=="" for /d %%D in ("%USERPROFILE%\go\pkg\mod\golang.org\toolchain@v0.0.1-go1.2*.windows-amd64") do if exist "%%D\bin\go.exe" set "GOEXE=%%D\bin\go.exe"
if "%GOEXE%"=="" set "GOEXE=go"

"%GOEXE%" build -mod=readonly -trimpath -o "%OUTDIR%\nightshift.exe" ./cmd/nightshift
set "STATUS=%ERRORLEVEL%"
endlocal & exit /b %STATUS%

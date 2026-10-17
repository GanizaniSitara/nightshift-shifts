@echo off
setlocal
cd /d "%~dp0.."

set "OUTDIR=%~1"
if "%OUTDIR%"=="" set "OUTDIR=C:\nightshift\bin"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

if "%GOEXE%"=="" set "GOEXE=go"

"%GOEXE%" build -mod=readonly -trimpath -o "%OUTDIR%\nightshift.exe" ./cmd/nightshift
set "STATUS=%ERRORLEVEL%"
endlocal & exit /b %STATUS%

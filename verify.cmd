@echo off
setlocal
cd /d "%~dp0"

call C:\scripts\build-nightshift-go.cmd "%~dp0."
if errorlevel 1 exit /b %ERRORLEVEL%

set "GOEXE=%USERPROFILE%\go\pkg\mod\golang.org\toolchain@v0.0.1-go1.25.12.windows-amd64\bin\go.exe"
set "GOPROXY=off"
set "GOSUMDB=off"
set "GOTOOLCHAIN=local"
"%GOEXE%" test ./...
if errorlevel 1 exit /b %ERRORLEVEL%

C:/miniconda3/envs/python312/python.exe -m pytest test_go_readonly.py test_go_write_commands.py test_go_lifecycle.py test_go_launcher.py test_go_crews.py test_go_telemetry.py -q -n auto --dist loadgroup
if errorlevel 1 exit /b %ERRORLEVEL%

endlocal & exit /b 0

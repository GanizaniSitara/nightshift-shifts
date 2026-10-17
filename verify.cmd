@echo off
setlocal
cd /d "%~dp0"

call "%~dp0scripts\build.cmd" "%~dp0build"
if errorlevel 1 exit /b %ERRORLEVEL%

rem No GOEXE given: prefer a Go toolchain from the standard module cache (go.mod needs Go 1.25+), else go on PATH.
if "%GOEXE%"=="" for /d %%D in ("%USERPROFILE%\go\pkg\mod\golang.org\toolchain@v0.0.1-go1.2*.windows-amd64") do if exist "%%D\bin\go.exe" set "GOEXE=%%D\bin\go.exe"
if "%GOEXE%"=="" set "GOEXE=go"
set "GOPROXY=off"
set "GOSUMDB=off"
set "GOTOOLCHAIN=local"
"%GOEXE%" test ./...
if errorlevel 1 exit /b %ERRORLEVEL%

C:/miniconda3/envs/python312/python.exe -m pytest test_go_readonly.py test_go_write_commands.py test_go_lifecycle.py test_go_launcher.py test_go_crews.py test_go_telemetry.py -q -n auto --dist loadgroup
if errorlevel 1 exit /b %ERRORLEVEL%

endlocal & exit /b 0

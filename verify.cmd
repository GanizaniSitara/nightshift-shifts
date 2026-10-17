@echo off
setlocal
cd /d "%~dp0"

call "%~dp0scripts\build.cmd" "%~dp0build"
if errorlevel 1 exit /b %ERRORLEVEL%

if "%GOEXE%"=="" set "GOEXE=go"
set "GOPROXY=off"
set "GOSUMDB=off"
set "GOTOOLCHAIN=local"
"%GOEXE%" test ./...
if errorlevel 1 exit /b %ERRORLEVEL%

C:/miniconda3/envs/python312/python.exe -m pytest test_go_readonly.py test_go_write_commands.py test_go_lifecycle.py test_go_launcher.py test_go_crews.py test_go_telemetry.py -q -n auto --dist loadgroup
if errorlevel 1 exit /b %ERRORLEVEL%

endlocal & exit /b 0

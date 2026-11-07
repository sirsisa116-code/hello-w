@echo off
rem Run run_daily.py from Windows Task Scheduler and save the output to output\logs.
rem Japanese text is not used in this file on purpose (cmd.exe may misread it).

cd /d "%~dp0.."
if not exist output\logs mkdir output\logs

rem Write logs in UTF-8 so that symbols in messages never stop the run.
set PYTHONUTF8=1

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set STAMP=%%i
".venv\Scripts\python.exe" scripts\run_daily.py > "output\logs\run_daily_%STAMP%.log" 2>&1
exit /b %ERRORLEVEL%

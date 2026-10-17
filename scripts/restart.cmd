@echo off
rem Restart the rig web page with the newest code (scripts\restart.sh in Git Bash):
rem   scripts\restart.cmd          -> update, stop the running page, serve again in this window
rem   scripts\restart.cmd force    -> even while a run started on the page is in progress
setlocal
set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=%LocalAppData%\Programs\Git\bin\bash.exe"
if not exist "%BASH%" (
  echo Git Bash not found. Install Git for Windows: https://git-scm.com/download/win
  exit /b 1
)
if /i "%~1"=="force" set "FORCE=1"
rem Forward slashes: bash's dirname doesn't split on backslashes.
set "SCRIPT=%~dp0restart.sh"
set "SCRIPT=%SCRIPT:\=/%"
"%BASH%" "%SCRIPT%"

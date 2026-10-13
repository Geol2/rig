@echo off
rem Run scripts\auto.sh from PowerShell or cmd:  scripts\auto.cmd [MAX_COST] [RUNS]
rem   scripts\auto.cmd          -> $3 per run, 5 runs
rem   scripts\auto.cmd 5 10     -> $5 per run, 10 runs
setlocal
set "BASH=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%BASH%" set "BASH=%LocalAppData%\Programs\Git\bin\bash.exe"
if not exist "%BASH%" (
  echo Git Bash not found. Install Git for Windows: https://git-scm.com/download/win
  exit /b 1
)
if not "%~1"=="" set "MAX_COST=%~1"
if not "%~2"=="" set "RUNS=%~2"
rem Forward slashes: bash's dirname doesn't split on backslashes.
set "SCRIPT=%~dp0auto.sh"
set "SCRIPT=%SCRIPT:\=/%"
"%BASH%" "%SCRIPT%"

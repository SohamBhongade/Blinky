@echo off
REM ------------------------------------------------------------
REM  push-to-github.bat  -  double-click to push this folder
REM  Put this file INSIDE the folder you want to upload.
REM ------------------------------------------------------------
cd /d "%~dp0"
echo Folder: %cd%
echo.

REM 1. Check git is installed
where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed. Trying to install it now...
  winget install --id Git.Git -e --source winget
  echo.
  echo After the install finishes, CLOSE this window and double-click this file again.
  pause
  exit /b
)

REM 2. One-time name/email setup
for /f "delims=" %%i in ('git config --global user.name') do set GITNAME=%%i
if not defined GITNAME (
  set /p GITNAME=Your name for commits:
  call git config --global user.name "%%GITNAME%%"
)
for /f "delims=" %%i in ('git config --global user.email') do set GITEMAIL=%%i
if not defined GITEMAIL (
  set /p GITEMAIL=Your GitHub email:
  call git config --global user.email "%%GITEMAIL%%"
)

REM 3. Start a repo here if there isn't one yet
if not exist ".git" (
  echo Setting up git in this folder...
  git init -q
  git branch -M main
)

REM 4. Link to GitHub repo (asked only the first time)
git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo.
  echo Paste your GitHub repo URL  e.g. https://github.com/yourname/blinky.git
  set /p REPOURL=URL:
  call git remote add origin "%%REPOURL%%"
)
for /f "delims=" %%i in ('git remote get-url origin') do echo Remote: %%i
echo.

REM 5. Commit everything
git add -A
set MSG=
set /p MSG=Commit message (Enter = auto):
if "%MSG%"=="" set MSG=Update %date% %time:~0,5%
git diff --cached --quiet
if errorlevel 1 (
  git commit -q -m "%MSG%"
  echo Committed: %MSG%
) else (
  echo No new changes to commit.
)

REM 6. Push (if GitHub already has files, merge them in first)
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set BRANCH=%%b
echo Pushing to GitHub (%BRANCH%)...
git push -u origin %BRANCH%
if errorlevel 1 (
  echo GitHub has files you don't have locally. Merging them in...
  git pull origin %BRANCH% --allow-unrelated-histories --no-rebase --no-edit
  git push -u origin %BRANCH%
)

if errorlevel 1 (
  echo.
  echo Push failed. Check the repo URL and that you signed in to GitHub.
) else (
  echo.
  echo Done! Your files are on GitHub.
)
echo.
pause

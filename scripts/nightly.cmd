@echo off
rem Nightly ComicBOM job: Codex writes scenes ahead, then ComfyUI renders chapters until 7:45am.
rem Registered to run at 11 PM by scripts\install-nightly.ps1; safe to start by hand (one run at a time).
rem
rem Around the render, two review-site steps (see review_site\tools\nightly_sync.py). Neither can stop the night:
rem   pull    first: issues you accepted on the website become flags, so tonight's redraws include them
rem   publish last:  redrawn chapters go up to the website as WAITING versions; you still make each one live
cd /d "%~dp0.."
if not exist runs\book\nightly mkdir runs\book\nightly
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%d
set LOG=runs\book\nightly\%TODAY%.log
set PYTHONIOENCODING=utf-8

echo %TIME% review site: pulling accepted issues>> "%LOG%"
".venv\Scripts\python.exe" -u -m review_site.tools.nightly_sync pull >> "%LOG%" 2>&1

".venv\Scripts\python.exe" -u -m bom_comic.nightly --until 07:45 >> "%LOG%" 2>&1
set RESULT=%ERRORLEVEL%

echo %TIME% review site: publishing redrawn chapters>> "%LOG%"
".venv\Scripts\python.exe" -u -m review_site.tools.nightly_sync publish >> "%LOG%" 2>&1

exit /b %RESULT%

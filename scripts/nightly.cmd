@echo off
rem Nightly ComicBOM job: Codex writes scenes ahead, then ComfyUI renders chapters until 7:45am.
rem Registered to run at midnight by scripts\install-nightly.ps1; safe to start by hand (one run at a time).
cd /d "%~dp0.."
if not exist runs\book\nightly mkdir runs\book\nightly
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%d
".venv\Scripts\python.exe" -u -m bom_comic.nightly --until 07:45 >> "runs\book\nightly\%TODAY%.log" 2>&1

@echo off
rem Write (not render) the rest of the book's scenes with Codex: first re-repair chapters that still have rejected
rem scenes, then continue from the first unwritten chapter. Holds the writing lock, so the nightly job only renders
rem while this runs. Stops by itself at a Codex usage limit; run again to resume.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -u -c "from bom_comic.cli import main; import sys; sys.argv=['bom-comic','book','--repair']; main()" >> "runs\book\book.log" 2>&1

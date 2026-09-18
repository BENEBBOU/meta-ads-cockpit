@echo off
REM Launches the local dashboard app. Double-click, or run from a shortcut.
REM Settings/paths in the app are relative to this folder, exactly like the
REM run_*.py CLI scripts, so the working directory must be set here first.

cd /d "%~dp0"
streamlit run app\Home.py

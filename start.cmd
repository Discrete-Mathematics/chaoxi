@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 start.py
goto finished
:use_python
where python >nul 2>nul
if errorlevel 1 goto missing
python start.py
goto finished
:missing
echo Install Python 3.10 or newer, then open start.cmd again.
:finished
if errorlevel 1 pause

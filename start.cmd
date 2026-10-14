@echo off
rem Double-click to open NoobPainterSimulation (the installer puts a shortcut to this on the desktop).
title NoobPainterSimulation - keep this window open; close it to quit
cd /d "%~dp0"
".venv\Scripts\python.exe" -m noobpainter ui
if errorlevel 1 pause

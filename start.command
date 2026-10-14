#!/bin/bash
# Double-click to open NoobPainterSimulation (the installer puts a launcher on the desktop).
# Keep the window open while you use it; close it to quit.
cd "$(dirname "$0")" && exec .venv/bin/python -m noobpainter ui

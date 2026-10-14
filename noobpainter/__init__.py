"""NoobPainterSimulation: pictures painted, drawn and carved stroke by stroke, on video."""
import os
import sys
from pathlib import Path

# The engine's modules import each other by their plain names (import brushwork).
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

# Text in UTF-8 everywhere, Windows included: its old default code page has no emoji or
# accents (a title's emoji stopped a finished video from saving). Programs started from
# here (the music fetcher) inherit it; this process's own console output is switched too.
os.environ.setdefault("PYTHONUTF8", "1")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

__version__ = "0.1.0"

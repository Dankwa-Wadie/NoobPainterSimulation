"""NoobPainterSimulation: pictures painted, drawn and carved stroke by stroke, on video."""
import sys
from pathlib import Path

# The engine's modules import each other by their plain names (import brushwork).
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

__version__ = "0.1.0"

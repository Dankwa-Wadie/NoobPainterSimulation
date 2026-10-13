"""Progress, printed: what the painter is doing, as it does it.

The engine reports its stages and steps through these calls. Here they print one
line at a time to stderr; plug in your own by replacing `report`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_state: dict = {}


def report(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def start(account: str = "", topic: str = "", kind: str = "") -> None:
    _state.clear()
    _state.update(topic=topic, began=time.time(), stage="", running=True)
    report(f"▶ {topic}")


def active() -> bool:
    return bool(_state.get("running"))


def set_workdir(workdir: str | Path) -> None:
    _state["workdir"] = str(workdir)


def stage(key: str, detail: str = "", total: int = 0) -> None:
    _state.update(stage=key, total=total)
    report(f"  [{key}] {detail}")


def step(done: int, total: int | None = None, detail: str = "") -> None:
    total = total if total is not None else _state.get("total", 0)
    if detail:
        report(f"    {detail}" + (f"  ({done}/{total})" if total else ""))


def fraction(frac: float, detail: str = "") -> None:
    last = _state.get("last_frac", -1.0)
    if frac - last >= 0.1 or frac >= 0.98:            # every 10%, not every frame
        _state["last_frac"] = frac
        report(f"    {frac * 100:.0f}%  {detail}")


def tick(elapsed: float, expected: float = 180.0) -> None:
    pass


def set_preview(path: str | Path) -> None:
    _state["preview"] = str(path)


def finish(detail: str = "") -> None:
    report(f"✓ {detail} ({time.time() - _state.get('began', time.time()):.0f} s)")
    _state["running"] = False


def fail(error: str) -> None:
    report(f"✗ {error}")
    _state["running"] = False

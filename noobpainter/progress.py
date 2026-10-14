"""Progress: what the painter is doing, as it does it.

The engine reports its stages and steps through these calls. They print one line
at a time to stderr, and keep a small state (stage, percent, recent lines, the
latest preview picture) that the web UI (ui.py) reads while a painting is made.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_state: dict = {}
_cancel = threading.Event()

# Helper programs a painting starts that a cancel stops; these others keep running (the
# image model's server, and the downloads under Extras, aren't part of the painting).
_SPARE = ("ComfyUI", "main.py", "fetch_classical", "install_image_model")


class Cancelled(Exception):
    """The person pressed Cancel: the work stops at its next progress report."""


def cancel() -> None:
    """Stop the painting being made: flag it (checked at every stage, step and preview)
    and stop the helper programs it is running now (the paint engine, ffmpeg, Blender)."""
    _cancel.set()
    report("✗ Cancelling…")
    try:
        import psutil
        me = psutil.Process()
        for child in me.children(recursive=True):
            try:
                if any(s in " ".join(child.cmdline()) for s in _SPARE):
                    continue
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        pass                                           # the flag alone stops it, a little later


def cancelled() -> bool:
    return _cancel.is_set()


def _check() -> None:
    if _cancel.is_set():
        raise Cancelled("cancelled")

# rough share of the whole run each stage takes, for one percent bar
_STAGES = {"reference": (0, 10), "plan": (10, 30), "paint": (30, 75), "assemble": (75, 99)}


def report(line: str) -> None:
    print(line, file=sys.stderr, flush=True)
    with _lock:
        _state.setdefault("log", []).append(line.strip())
        _state["log"] = _state["log"][-60:]


def snapshot() -> dict:
    """A copy of the current state, for the UI."""
    with _lock:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in _state.items()}


def _set(**kv) -> None:
    with _lock:
        _state.update(kv)


def _percent(frac: float) -> None:
    lo, hi = _STAGES.get(_state.get("stage", ""), (0, 100))
    _set(percent=round(lo + (hi - lo) * max(0.0, min(1.0, frac)), 1))


def start(account: str = "", topic: str = "", kind: str = "") -> None:
    _cancel.clear()
    with _lock:
        _state.clear()
        _state.update(topic=topic, began=time.time(), stage="", running=True, percent=0.0,
                      status="running", log=[])
    report(f"▶ {topic}")


def active() -> bool:
    return bool(_state.get("running"))


def set_workdir(workdir: str | Path) -> None:
    _set(workdir=str(workdir))


def stage(key: str, detail: str = "", total: int = 0) -> None:
    _check()
    _set(stage=key, detail=detail, total=total)
    _percent(0.0)
    report(f"  [{key}] {detail}")


def step(done: int, total: int | None = None, detail: str = "") -> None:
    _check()
    total = total if total is not None else _state.get("total", 0)
    if total:
        _percent(done / total)
    if detail:
        _set(detail=detail)
        report(f"    {detail}" + (f"  ({done}/{total})" if total else ""))


def fraction(frac: float, detail: str = "") -> None:
    _check()
    _percent(frac)
    if detail:
        _set(detail=detail)
    last = _state.get("last_frac", -1.0)
    if frac - last >= 0.1 or frac >= 0.98:            # print every 10%, not every frame
        _set(last_frac=frac)
        report(f"    {frac * 100:.0f}%  {detail}")


def tick(elapsed: float, expected: float = 180.0) -> None:
    pass


def set_preview(path: str | Path) -> None:
    _check()
    _set(preview=str(path), preview_at=time.time())


def finish(detail: str = "") -> None:
    report(f"✓ {detail} ({time.time() - _state.get('began', time.time()):.0f} s)")
    _set(running=False, status="done", percent=100.0)


def fail(error: str) -> None:
    report(f"✗ {error}")
    _set(running=False, status="failed", error=str(error)[:400])

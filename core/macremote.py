"""Control the Mac from the phone app and the website (ticket 436ccc83): the screen as JPEG frames, and
mouse and keyboard through scripts/mac.py (the same tool the Mac worker uses, so the same permissions).

Whoever has this has the whole Mac, so appserver only serves it to a login session (never APP_TOKEN, not
even at home), and Sanjar gets a Telegram message whenever a session starts controlling it.
"""
import asyncio
import math
import re
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PYTHON = HERE / ".venv" / "bin" / "python"
MAC = HERE / "scripts" / "mac.py"
FRAME_AGE = 0.4                        # a frame this fresh is reused: two screens polling is one capture
TEXT_MAX = 5000
_KEY = re.compile(r"^((cmd|command|ctrl|control|alt|option|shift|fn)\+)*([a-z0-9]+|[=\-\]\['`;\\,./])$")

_frames: dict[int, tuple[float, bytes]] = {}
_size: tuple[float, int, int] = (0.0, 0, 0)
_screen_lock = asyncio.Lock()
_input_lock = asyncio.Lock()


def _number(body: dict, name: str, low: float = 0, high: float = 20000) -> float:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"bad {name}")
    return value


def input_command(body: dict) -> tuple[list[str], str]:
    """A request from the phone -> mac.py's arguments and what goes to its stdin. ValueError if it's not valid."""
    action = body.get("action")
    if action in ("click", "move"):
        args = [action, str(_number(body, "x")), str(_number(body, "y"))]
        if action == "click":
            args += ["--right"] * bool(body.get("right") is True) + ["--double"] * bool(body.get("double") is True)
        return args, ""
    if action == "drag":
        return ["drag", *(str(_number(body, n)) for n in ("x1", "y1", "x2", "y2"))], ""
    if action == "scroll":
        where = [str(_number(body, "x")), str(_number(body, "y"))] if "x" in body or "y" in body else []
        return ["scroll", str(int(_number(body, "dy", -100, 100))), *where], ""
    if action == "type":
        text = body.get("text")
        if not isinstance(text, str) or not text or len(text) > TEXT_MAX:
            raise ValueError("bad text")
        return ["type", "-"], text                     # by stdin: a typed password must not show in `ps`
    if action == "key":
        combo = body.get("key")
        if not isinstance(combo, str) or not _KEY.match(combo.lower()):
            raise ValueError("bad key")
        return ["key", combo.lower()], ""
    raise ValueError("unknown action")


async def _run(*cmd, stdin: str = "", timeout: float = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "It took too long."
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def do_input(body: dict) -> str:
    """Do it on the Mac; returns an error message, or "" if it worked."""
    args, stdin = input_command(body)
    async with _input_lock:
        code, out = await _run(str(PYTHON), str(MAC), *args, stdin=stdin)
    return out[-300:] or "It didn't work." if code else ""


async def wake() -> None:
    """Wake the display (it's asleep when nobody's at the Mac and the picture is black)."""
    await _run("caffeinate", "-u", "-t", "2", timeout=5)


async def screen_size() -> tuple[int, int]:
    """The main display in points: what clicks are measured in. Asked once a minute."""
    global _size
    if time.monotonic() - _size[0] > 60 or not _size[1]:
        code, out = await _run(str(PYTHON), str(MAC), "size", timeout=10)
        found = re.fullmatch(r"(\d+) (\d+)", out)
        if code or not found:
            raise RuntimeError(out[-300:] or "can't read the screen size")
        _size = (time.monotonic(), int(found[1]), int(found[2]))
    return _size[1], _size[2]


async def screen(width: int = 1280) -> tuple[bytes, int, int]:
    """One frame as JPEG, `width` pixels wide, and the screen's size in points. Frames never stay on disk."""
    width = max(480, min(int(width), 1920))
    async with _screen_lock:
        w, h = await screen_size()
        hit = _frames.get(width)
        if hit and time.monotonic() - hit[0] < FRAME_AGE:
            return hit[1], w, h
        with tempfile.TemporaryDirectory(prefix="jarvis-mac-") as tmp:
            raw, small = Path(tmp) / "raw.jpg", Path(tmp) / "small.jpg"
            code, out = await _run("screencapture", "-x", "-C", "-t", "jpg", str(raw), timeout=10)
            if code or not raw.is_file():
                raise RuntimeError(out[-300:] or "Screenshot failed: the display may be asleep.")
            code, out = await _run("sips", "--resampleWidth", str(width), "-s", "format", "jpeg", "-s", "formatOptions", "60",
                                   str(raw), "--out", str(small), timeout=15)
            if code or not small.is_file():
                raise RuntimeError(out[-300:] or "Couldn't shrink the screenshot.")
            frame = small.read_bytes()
        _frames.clear()
        _frames[width] = (time.monotonic(), frame)
        return frame, w, h

"""Control the Mac from the phone app and the website (ticket 436ccc83): the screen as JPEG frames, and
mouse and keyboard through scripts/mac.py (the same tool the Mac worker uses, so the same permissions).

Whoever has this has the whole Mac, so appserver only serves it to a login session (never APP_TOKEN, not
even at home), and Sanjar gets a Telegram message whenever a session starts controlling it.

Fast path (ticket 18cf9c7b): `mac.py serve` helpers stay running (one for frames, one for input, so a frame
never holds up the mouse), and the WebSocket /api/mac/live streams frames as fast as the phone takes them
while mouse and keys come back on the same socket: no Python start per click, no request per frame.
"""
import asyncio
import contextlib
import json
import logging
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
IDLE_STOP = 120                         # a helper nobody used for this long exits (they hold ~60 MB each)
log = logging.getLogger("jarvis.macremote")
_KEY = re.compile(r"^((cmd|command|ctrl|control|alt|option|shift|fn)\+)*([a-z0-9]+|[=\-\]\['`;\\,./])$")

_frames: dict[int, tuple[float, bytes, int, int]] = {}    # width -> (when, jpeg, screen w, screen h)
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


def input_op(body: dict) -> dict:
    """A request from the phone or website -> the helper's request. ValueError if it isn't valid.
    Besides input_command's actions: down / move / drag / up {x, y} for dragging with the mouse held."""
    action = body.get("action")
    if action in ("down", "up") or (action == "drag" and "x1" not in body):   # a whole drag at once has x1..y2
        return {"op": action, "x": _number(body, "x"), "y": _number(body, "y")}
    args, stdin = input_command(body)
    op = {"op": action}
    if action in ("click", "move"):
        op.update(x=float(args[1]), y=float(args[2]))
        if action == "click":
            op.update(right="--right" in args, double="--double" in args)
    elif action == "drag":                               # a whole drag at once (old clients)
        op = {"op": "dragline", "x1": float(args[1]), "y1": float(args[2]), "x2": float(args[3]), "y2": float(args[4])}
    elif action == "scroll":
        op.update(dy=int(args[1]), x=float(args[2]) if len(args) > 2 else None, y=float(args[3]) if len(args) > 3 else None)
    elif action == "type":
        op["text"] = stdin
    elif action == "key":
        op["key"] = args[1]
    return op


class Helper:
    """One `mac.py serve` process, started when needed and stopped when idle; one request at a time."""

    def __init__(self, name: str):
        self.name = name
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.used = 0.0
        self.reaper: asyncio.Task | None = None

    async def _start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(str(PYTHON), str(MAC), "serve", stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                         limit=8_000_000)
        if self.reaper is None or self.reaper.done():
            self.reaper = asyncio.create_task(self._reap())

    async def _reap(self) -> None:
        while self.proc and self.proc.returncode is None:
            await asyncio.sleep(15)
            if time.monotonic() - self.used > IDLE_STOP and not self.lock.locked():
                await self.stop()

    async def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 3)
            if proc.returncode is None:
                proc.kill()

    async def request(self, req: dict, timeout: float = 20) -> tuple[dict, bytes]:
        async with self.lock:
            self.used = time.monotonic()
            for attempt in (1, 2):                        # a helper that died is started again once
                if self.proc is None or self.proc.returncode is not None:
                    await self._start()
                try:
                    self.proc.stdin.write(json.dumps(req).encode() + b"\n")
                    await self.proc.stdin.drain()
                    head = json.loads(await asyncio.wait_for(self.proc.stdout.readline(), timeout))
                    body = await asyncio.wait_for(self.proc.stdout.readexactly(head["len"]), timeout) if head.get("len") else b""
                    return head, body
                except (ConnectionError, asyncio.IncompleteReadError, json.JSONDecodeError, asyncio.TimeoutError) as e:
                    log.warning("mac helper %s: %r", self.name, e)
                    await self.stop()
                    if attempt == 2:
                        return {"ok": False, "error": "The Mac helper stopped. Try again."}, b""
        return {"ok": False, "error": "unreachable"}, b""


frames = Helper("frames")
inputs = Helper("input")


async def do_input(body: dict) -> str:
    """Do it on the Mac; returns an error message, or "" if it worked."""
    op = input_op(body)
    if op["op"] == "dragline":                            # down, 20 steps, up: as mac.py drag did
        steps = [{"op": "down", "x": op["x1"], "y": op["y1"]}]
        steps += [{"op": "drag", "x": op["x1"] + (op["x2"] - op["x1"]) * i / 20, "y": op["y1"] + (op["y2"] - op["y1"]) * i / 20}
                  for i in range(1, 21)]
        steps.append({"op": "up", "x": op["x2"], "y": op["y2"]})
    else:
        steps = [op]
    for step in steps:
        head, _ = await inputs.request(step, timeout=60 if op["op"] == "type" else 20)
        if not head.get("ok"):
            return head.get("error") or "It didn't work."
    return ""


async def wake() -> None:
    """Wake the display (it's asleep when nobody's at the Mac and the picture is black)."""
    await _run("caffeinate", "-u", "-t", "2", timeout=5)


async def screen(width: int = 1280) -> tuple[bytes, int, int]:
    """One frame as JPEG, `width` pixels wide, and the screen's size in points (for the old request-per-frame clients)."""
    width = max(480, min(int(width), 1920))
    async with _screen_lock:
        hit = _frames.get(width)
        if hit and time.monotonic() - hit[0] < FRAME_AGE:
            return hit[1], hit[2], hit[3]
        head, jpeg = await frames.request({"op": "shot", "w": width, "q": 0.6})
        if not head.get("ok") or not jpeg:
            raise RuntimeError(head.get("error") or "Screenshot failed: the display may be asleep.")
        _frames.clear()
        _frames[width] = (time.monotonic(), jpeg, head["w"], head["h"])
        return jpeg, head["w"], head["h"]


# --- the live socket ------------------------------------------------------------------------------

FAST, SLOW = 0.07, 0.3                  # seconds between frames: while he's using it / when nothing happens
BUSY_FOR = 3.0                          # "using it" = input in the last few seconds


def pack(meta: dict, jpeg: bytes) -> bytes:
    """A frame message: 4 bytes of JSON length, the JSON, then the JPEG."""
    head = json.dumps(meta).encode()
    return len(head).to_bytes(4, "big") + head + jpeg


async def live(ws, width: int = 1024, quality: float = 0.5) -> None:
    """Stream frames to `ws` (only when the picture changed, at most 2 waiting for the phone) and do its input.
    Text messages from the phone: {"t": "ack"} after showing a frame, {"t": "size", "w", "q"} to change the
    picture, or an input {"action": ...} like POST /api/mac/input. Ends when the socket closes."""
    credit = asyncio.Semaphore(2)
    state = {"w": max(480, min(width, 1920)), "q": max(0.2, min(quality, 0.9)), "busy": 0.0, "held": None}

    async def receive():
        while True:
            msg = json.loads(await ws.receive_text())
            if not isinstance(msg, dict):
                continue
            if msg.get("t") == "ack":
                credit.release()
                continue
            if msg.get("t") == "size":
                with contextlib.suppress(TypeError, ValueError):
                    state["w"] = max(480, min(int(msg["w"]), 1920))
                    state["q"] = max(0.2, min(float(msg.get("q", state["q"])), 0.9))
                continue
            state["busy"] = time.monotonic()
            if msg.get("action") == "wake":
                await wake()
                continue
            try:
                op = input_op(msg)
            except ValueError as e:
                await ws.send_text(json.dumps({"t": "error", "error": str(e)}))
                continue
            if op["op"] == "down":
                state["held"] = (op["x"], op["y"])
            elif op["op"] in ("drag", "move") and state["held"]:
                state["held"] = (op["x"], op["y"])
            elif op["op"] == "up":
                state["held"] = None
            err = await do_input(msg)
            if err:
                await ws.send_text(json.dumps({"t": "error", "error": err}))

    async def send():
        last = ""
        while True:
            await credit.acquire()
            started = time.monotonic()
            head, jpeg = await frames.request({"op": "shot", "w": state["w"], "q": state["q"], "last": last})
            if not head.get("ok"):
                await ws.send_text(json.dumps({"t": "error", "error": head.get("error", "no picture")}))
                credit.release()
                await asyncio.sleep(2)
                continue
            if head.get("same"):
                credit.release()                         # nothing sent, nothing to wait for
                await ws.send_text(json.dumps({"t": "cursor", "cx": head["cx"], "cy": head["cy"]}))
            else:
                last = head["hash"]
                await ws.send_bytes(pack({"w": head["w"], "h": head["h"], "cx": head["cx"], "cy": head["cy"]}, jpeg))
            gap = FAST if time.monotonic() - state["busy"] < BUSY_FOR else SLOW
            await asyncio.sleep(max(0.0, gap - (time.monotonic() - started)))

    tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        if state["held"]:                                # never leave the button down when the phone goes away
            with contextlib.suppress(Exception):
                await inputs.request({"op": "up", "x": state["held"][0], "y": state["held"][1]})

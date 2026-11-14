"""Mouse, keyboard and screen for the Mac worker (core/mac.py), so it can use any app.

    mac.py check                    are Accessibility and Screen Recording allowed?
    mac.py shot [file.png]          screenshot, scaled so its pixels = click coordinates
    mac.py click X Y [--right] [--double]
    mac.py move X Y
    mac.py drag X1 Y1 X2 Y2
    mac.py scroll DY [X Y]          DY > 0 scrolls up, < 0 down (lines)
    mac.py type "any text"          types it (any language, emoji too); "-" reads the text from stdin
    mac.py size                     the main display in points: "1280 800"
    mac.py serve                    stay running for remote control (core/macremote.py): one JSON request
                                    per stdin line, one JSON line back; a frame's JPEG bytes follow its line
    mac.py key cmd+shift+t          one key or a combo: cmd ctrl alt shift + a key name

Coordinates are screen points, top-left = 0,0, the same as the pixels of `shot`.
Needs Accessibility (mouse/keys) and Screen Recording (shot) for the app that runs Jarvis.
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import ApplicationServices as AS
import Quartz as Q

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.paths import VAR  # noqa: E402

SHOTS = VAR / "shots"
# Every click and key this tool makes is Jarvis's, not a stranger at the Mac: the guard (core/guard.py) skips input
# right after this marker changes (ticket 733814dd: the reel poster's clicks locked the Mac twice on 13 Nov).
AUTOMATION = VAR / "state" / "automation_input"
_marked = [0.0]


def _mark() -> None:
    now = time.time()
    if now - _marked[0] < 0.5:
        return
    _marked[0] = now
    try:
        AUTOMATION.parent.mkdir(parents=True, exist_ok=True)
        AUTOMATION.write_text(str(now))
    except OSError:
        pass


def _post(event) -> None:
    _mark()
    Q.CGEventPost(Q.kCGHIDEventTap, event)

KEYS = {
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "esc": 53,
    "escape": 53, "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126, "volumeup": 72, "volumedown": 73, "mute": 74,
    **{f"f{i}": c for i, c in enumerate([122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111], 1)},
    **dict(zip("asdfhgzxcv", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])), "b": 11, "q": 12, "w": 13, "e": 14,
    "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37,
    "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
}
MODS = {"cmd": Q.kCGEventFlagMaskCommand, "command": Q.kCGEventFlagMaskCommand,
        "ctrl": Q.kCGEventFlagMaskControl, "control": Q.kCGEventFlagMaskControl,
        "alt": Q.kCGEventFlagMaskAlternate, "option": Q.kCGEventFlagMaskAlternate,
        "shift": Q.kCGEventFlagMaskShift, "fn": Q.kCGEventFlagMaskSecondaryFn}


def check() -> None:
    """Report both permissions; asking also adds Jarvis's app to the list in System Settings."""
    ax = AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})
    rec = Q.CGPreflightScreenCaptureAccess() or Q.CGRequestScreenCaptureAccess()
    print(f"Accessibility (mouse/keyboard): {'allowed' if ax else 'NOT allowed'}")
    print(f"Screen Recording (screenshots): {'allowed' if rec else 'NOT allowed'}")
    if not (ax and rec):
        print("Sanjar has to switch it on once at the Mac: System Settings > Privacy & Security > "
              "Accessibility / Screen Recording (the entry for Jarvis's Python or caffeinate).")
        sys.exit(1)


def shot(path: str = "") -> None:
    out = Path(path) if path else SHOTS / f"mac_{int(time.time() * 1000)}.png"
    with tempfile.NamedTemporaryFile(suffix=".png") as raw:
        if subprocess.run(["screencapture", "-x", "-C", raw.name]).returncode:
            sys.exit("Screenshot failed: Screen Recording is probably not allowed for Jarvis "
                     "(or the display is asleep). Run `mac.py check` and tell Sanjar.")
        bounds = Q.CGDisplayBounds(Q.CGMainDisplayID())
        # Retina screens capture at 2x; scale down so a pixel in the picture is a click point
        subprocess.run(["sips", "-z", str(int(bounds.size.height)), str(int(bounds.size.width)),
                        raw.name, "--out", str(out)], check=True, capture_output=True)
    print(f"{out} ({int(bounds.size.width)}x{int(bounds.size.height)} points)")


def _mouse(kind: int, x: float, y: float, button: int = Q.kCGMouseButtonLeft, clicks: int = 1) -> None:
    e = Q.CGEventCreateMouseEvent(None, kind, (x, y), button)
    Q.CGEventSetIntegerValueField(e, Q.kCGMouseEventClickState, clicks)
    _post(e)


def click(x: float, y: float, right: bool = False, double: bool = False) -> None:
    down, up, button = ((Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp, Q.kCGMouseButtonRight) if right
                        else (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGMouseButtonLeft))
    _mouse(Q.kCGEventMouseMoved, x, y)
    time.sleep(0.05)
    for n in (1, 2) if double else (1,):
        _mouse(down, x, y, button, n)
        _mouse(up, x, y, button, n)
        time.sleep(0.05)


def drag(x1: float, y1: float, x2: float, y2: float) -> None:
    _mouse(Q.kCGEventMouseMoved, x1, y1)
    _mouse(Q.kCGEventLeftMouseDown, x1, y1)
    for i in range(1, 21):
        _mouse(Q.kCGEventLeftMouseDragged, x1 + (x2 - x1) * i / 20, y1 + (y2 - y1) * i / 20)
        time.sleep(0.01)
    _mouse(Q.kCGEventLeftMouseUp, x2, y2)


def scroll(dy: int, x: float | None = None, y: float | None = None) -> None:
    if x is not None:
        _mouse(Q.kCGEventMouseMoved, x, y)
    _post(Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine, 1, dy))


def type_text(text: str) -> None:
    for ch in text:
        if ch == "\n":
            key("return")
            continue
        for down in (True, False):
            e = Q.CGEventCreateKeyboardEvent(None, 0, down)
            Q.CGEventKeyboardSetUnicodeString(e, len(ch.encode("utf-16-le")) // 2, ch)
            # No modifiers on typed text. Without this the event takes the system's current modifier state, which a
            # key("cmd+v") just before leaves holding Cmd, so every "n" typed became Cmd+N (dozens of empty Chrome
            # windows, 2026-11-10 and 11-14, ticket 4445813a).
            Q.CGEventSetFlags(e, 0)
            _post(e)
        time.sleep(0.01)


def key(combo: str) -> None:
    *mods, name = combo.lower().split("+")
    if name not in KEYS:
        sys.exit(f"Unknown key {name!r}. Known: {', '.join(sorted(KEYS))}")
    flags = 0
    for m in mods:
        flags |= MODS[m]
    for down in (True, False):
        e = Q.CGEventCreateKeyboardEvent(None, KEYS[name], down)
        Q.CGEventSetFlags(e, flags)
        _post(e)


# --- serve: a long-running helper, so remote control doesn't start Python for every click -------

def frame(width: int = 1024, quality: float = 0.5) -> tuple[bytes, int, int]:
    """The main display as JPEG `width` pixels wide, in memory (no screencapture, no files): ~60 ms."""
    from Foundation import NSMutableData
    image = Q.CGDisplayCreateImage(Q.CGMainDisplayID())
    if image is None:
        raise RuntimeError("no picture: Screen Recording is off, or the display is asleep")
    data = NSMutableData.data()
    dest = Q.CGImageDestinationCreateWithData(data, "public.jpeg", 1, None)
    Q.CGImageDestinationAddImage(dest, image, {Q.kCGImageDestinationLossyCompressionQuality: quality,
                                               Q.kCGImageDestinationImageMaxPixelSize: width})
    if not Q.CGImageDestinationFinalize(dest):
        raise RuntimeError("couldn't make the JPEG")
    bounds = Q.CGDisplayBounds(Q.CGMainDisplayID())
    return bytes(data), int(bounds.size.width), int(bounds.size.height)


def cursor() -> tuple[int, int]:
    at = Q.CGEventGetLocation(Q.CGEventCreate(None))
    return int(at.x), int(at.y)


def handle(req: dict) -> tuple[dict, bytes]:
    """One request of `serve`. Input requests are already checked by core/macremote.py."""
    op = req.get("op")
    if op == "shot":
        jpeg, w, h = frame(int(req.get("w", 1024)), float(req.get("q", 0.5)))
        cx, cy = cursor()
        digest = __import__("hashlib").blake2b(jpeg, digest_size=8).hexdigest()
        if digest == req.get("last"):
            return {"ok": True, "same": True, "cx": cx, "cy": cy}, b""
        return {"ok": True, "len": len(jpeg), "w": w, "h": h, "cx": cx, "cy": cy, "hash": digest}, jpeg
    if not AS.AXIsProcessTrusted():
        raise RuntimeError("Accessibility is off for Jarvis, so it can't use the mouse or keyboard.")
    x, y = req.get("x"), req.get("y")
    if op == "move":
        _mouse(Q.kCGEventMouseMoved, x, y)
    elif op == "down":
        _mouse(Q.kCGEventMouseMoved, x, y)
        _mouse(Q.kCGEventLeftMouseDown, x, y)
    elif op == "drag":
        _mouse(Q.kCGEventLeftMouseDragged, x, y)
    elif op == "up":
        _mouse(Q.kCGEventLeftMouseUp, x, y)
    elif op == "click":
        click(x, y, right=bool(req.get("right")), double=bool(req.get("double")))
    elif op == "scroll":
        scroll(int(req["dy"]), x, y)
    elif op == "type":
        type_text(req["text"])
    elif op == "key":
        name = req["key"].split("+")[-1]
        if name not in KEYS:
            raise RuntimeError(f"Unknown key {name!r}")
        key(req["key"])
    else:
        raise RuntimeError(f"unknown op {op!r}")
    return {"ok": True}, b""


def serve() -> None:
    import json
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        try:
            head, body = handle(json.loads(line))
        except Exception as e:                           # one bad request never ends the helper
            head, body = {"ok": False, "error": str(e)[:300]}, b""
        out.write(json.dumps(head).encode() + b"\n" + body)
        out.flush()


def main(args: list[str]) -> None:
    if not args:
        sys.exit(__doc__)
    cmd, rest = args[0], args[1:]
    nums = [float(a) for a in rest if not a.startswith("--")] if cmd in ("click", "move", "drag", "scroll") else []
    if cmd == "serve":
        serve()
        return
    if cmd == "size":
        bounds = Q.CGDisplayBounds(Q.CGMainDisplayID())
        print(int(bounds.size.width), int(bounds.size.height))
        return
    if cmd != "check" and cmd != "shot" and not AS.AXIsProcessTrusted():
        sys.exit("Accessibility is not allowed for Jarvis, so it can't use the mouse or keyboard. "
                 "Run `mac.py check` and tell Sanjar.")
    if cmd == "check":
        check()
    elif cmd == "shot":
        shot(rest[0] if rest else "")
    elif cmd == "click":
        click(*nums[:2], right="--right" in rest, double="--double" in rest)
    elif cmd == "move":
        _mouse(Q.kCGEventMouseMoved, *nums[:2])
    elif cmd == "drag":
        drag(*nums[:4])
    elif cmd == "scroll":
        scroll(int(nums[0]), *nums[1:3])
    elif cmd == "type":
        type_text(sys.stdin.read() if rest == ["-"] else " ".join(rest))
    elif cmd == "key":
        key(rest[0])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

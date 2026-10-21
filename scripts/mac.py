"""Mouse, keyboard and screen for the Mac worker (core/mac.py), so it can use any app.

    mac.py check                    are Accessibility and Screen Recording allowed?
    mac.py shot [file.png]          screenshot, scaled so its pixels = click coordinates
    mac.py click X Y [--right] [--double]
    mac.py move X Y
    mac.py drag X1 Y1 X2 Y2
    mac.py scroll DY [X Y]          DY > 0 scrolls up, < 0 down (lines)
    mac.py type "any text"          types it (any language, emoji too)
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

SHOTS = Path(__file__).parent.parent / "var" / "shots"

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
    Q.CGEventPost(Q.kCGHIDEventTap, e)


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
    Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitLine, 1, dy))


def type_text(text: str) -> None:
    for ch in text:
        if ch == "\n":
            key("return")
            continue
        for down in (True, False):
            e = Q.CGEventCreateKeyboardEvent(None, 0, down)
            Q.CGEventKeyboardSetUnicodeString(e, len(ch.encode("utf-16-le")) // 2, ch)
            Q.CGEventPost(Q.kCGHIDEventTap, e)
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
        Q.CGEventPost(Q.kCGHIDEventTap, e)


def main(args: list[str]) -> None:
    if not args:
        sys.exit(__doc__)
    cmd, rest = args[0], args[1:]
    nums = [float(a) for a in rest if not a.startswith("--")] if cmd in ("click", "move", "drag", "scroll") else []
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
        type_text(" ".join(rest))
    elif cmd == "key":
        key(rest[0])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

"""Mac guard (ticket 3b226456). Sanjar arms it himself ("guard") when he leaves; there is no automatic
away detection, so it never photographs him by mistake. While armed, any keyboard, mouse or trackpad
input means someone touched the Mac: he gets a Telegram alert at once, a screenshot, and a camera photo.

Touches come from macOS's "seconds since the last input" (no permission needed). The camera needs
macOS's Camera permission for the app that runs Jarvis: until he clicks Allow, ffmpeg just waits
forever, so it has a timeout and the alert says why there's no photo. The alert never depends on it.

The Mac worker's own clicks don't count (`busy`). Stays armed until he disarms it, also across restarts.
"""
import asyncio
import contextlib
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable

from .config import HERE, SHOTS, STATE

log = logging.getLogger("jarvis.guard")
FILE = STATE / "guard.json"       # {"armed_at", "watch_from", "last_alert"}; no file = not armed
GRACE = 60                        # seconds after arming, to let him walk away
POLL = 2
SECOND_PHOTO_AFTER = 8            # a second photo: by then they're usually looking at the screen
REPEAT_AFTER = 120                # still being used: another alert at most this often
PHOTO_TIMEOUT = 12
SEND_TRIES = (0, 5, 15, 30)       # opening the lid wakes the Mac before its network is back
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"     # launchd's PATH has no Homebrew
CAMERA = ["-f", "avfoundation", "-framerate", "30", "-video_size", "1280x720", "-pixel_format", "nv12", "-i", "0"]
WARM_UP_FRAMES = 30               # the first frames of a Mac camera are dark
AUTOMATION = STATE / "automation_input"   # scripts/mac.py marks every click and key it makes (ticket 733814dd)
AUTOMATION_GRACE = 15                        # seconds after Jarvis's own input that a touch isn't counted
WARNING = HERE / "assets" / "guard_warning.wav"   # "Do not touch this computer" in Uzbek, Russian and English (Gemini TTS)
ALARM = "/System/Library/Sounds/Basso.aiff"
WARN_VOLUME = 85                  # loud enough to be heard; his own volume is put back afterwards
LOGIN_FRAMEWORK = "/System/Library/PrivateFrameworks/login.framework/Versions/Current/login"
NO_CAMERA = ("the camera didn't answer: macOS is probably waiting for someone to allow it for Jarvis "
             "(a prompt on the Mac, or System Settings > Privacy & Security > Camera)")


def idle_seconds() -> float:
    """Seconds since the last keyboard, mouse or trackpad input on this Mac."""
    import Quartz
    return Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState,
                                                         Quartz.kCGAnyInputEventType)


def automation_recent(grace: float = AUTOMATION_GRACE) -> bool:
    """Was the latest input Jarvis's own (the reel poster, a Mac task, a worker, the app's remote control, all through
    scripts/mac.py)? Then it isn't someone touching the Mac."""
    try:
        return time.time() - AUTOMATION.stat().st_mtime < grace
    except OSError:
        return False


def lock_screen() -> bool:
    """Lock the Mac for real (ticket 4946d461: "lock it, not just make it sleep"): the same as Control-Command-Q,
    the login window comes up and only his password opens it. No Accessibility permission needed."""
    try:
        import ctypes
        ctypes.CDLL(LOGIN_FRAMEWORK).SACLockScreenImmediate()
        log.info("guard: locked the screen")
        return True
    except Exception as e:
        log.warning("guard: couldn't lock the screen: %r", e)
        return False


async def _osascript(script: str) -> str:
    proc = await asyncio.create_subprocess_exec("osascript", "-e", script, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    out, _ = await asyncio.wait_for(proc.communicate(), 10)
    return out.decode().strip()


async def _play(path: str | Path) -> None:
    proc = await asyncio.create_subprocess_exec("afplay", str(path))
    await asyncio.wait_for(proc.wait(), 60)


async def play_warning() -> None:
    """An alarm and a spoken "Do not touch this computer" (Uzbek, Russian, English), loud even if the Mac was
    muted; his own volume and mute come back afterwards."""
    before = ""
    try:
        before = await _osascript("get volume settings")      # "output volume:30, input volume:..., output muted:false"
        await _osascript(f"set volume output volume {WARN_VOLUME} without output muted")
        for _ in range(2):
            await _play(ALARM)
        if WARNING.exists():
            await _play(WARNING)
        await _play(ALARM)
    except Exception as e:
        log.warning("guard: warning sound failed: %r", e)
    finally:
        vol = next((p.split(":")[1] for p in before.split(", ") if p.startswith("output volume:")), "")
        muted = "output muted:true" in before
        if vol.isdigit():
            with contextlib.suppress(Exception):
                await _osascript(f"set volume output volume {vol}" + (" with output muted" if muted else " without output muted"))


warn = play_warning                  # what the guard calls (tests swap it for silence)


async def capture_photo() -> tuple[Path | None, str]:
    """A camera photo: (path, "") or (None, why not)."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"guard_{int(time.time() * 1000)}.jpg"
    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *CAMERA, "-vf", f"select=gte(n\\,{WARM_UP_FRAMES})",
            "-frames:v", "1", "-q:v", "3", str(path), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return None, f"ffmpeg couldn't start: {e}"
    try:
        _, err = await asyncio.wait_for(proc.communicate(), PHOTO_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, NO_CAMERA
    if path.exists() and path.stat().st_size:
        return path, ""
    return None, f"the camera failed: {err.decode(errors='replace').strip()[-200:] or proc.returncode}"



take_photo = capture_photo        # what the guard calls; tests swap it (they must never use the real camera)

class Guard:
    def __init__(self, send_text: Callable[[str], Awaitable], send_photos: Callable[[list[Path], str], Awaitable],
                 screenshot: Callable[[], Awaitable[Path]], busy: Callable[[], bool] = lambda: False):
        self.send_text, self.send_photos, self.screenshot, self.busy = send_text, send_photos, screenshot, busy
        self.lock, self.warn = lock_screen, warn          # replaced in tests: they must never lock the Mac or play sounds

    def state(self) -> dict:
        try:
            return json.loads(FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, s: dict) -> None:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(s))

    def arm(self, grace: float = GRACE) -> str:
        now = time.time()
        self._save({"armed_at": now, "watch_from": now + grace, "last_alert": 0})
        log.info("guard armed")
        return (f"Guard is on. I start watching in {grace:.0f} seconds. If anyone touches the Mac you get an "
                "alert and photos on Telegram. Turn it off before you touch it: /guard off")

    def disarm(self) -> str:
        was = bool(self.state())
        FILE.unlink(missing_ok=True)
        log.info("guard off")
        return "Guard is off." if was else "Guard wasn't on."

    def status(self) -> str:
        s = self.state()
        if not s:
            return "Guard is off."
        return "Guard is on since " + time.strftime("%H:%M", time.localtime(s["armed_at"])) + (
            ", last alert " + time.strftime("%H:%M", time.localtime(s["last_alert"])) if s.get("last_alert")
            else ", nothing touched so far")

    async def _deliver(self, fn, *args) -> None:
        for wait in SEND_TRIES:
            await asyncio.sleep(wait)
            try:
                await fn(*args)
                return
            except Exception as e:
                log.warning("guard: sending failed: %r", e)
        log.error("guard: couldn't deliver an alert")

    async def check(self) -> bool:
        """One look at the Mac. True if it sent an alert."""
        s = self.state()
        if not s:
            return False
        now = time.time()
        if self.busy() or automation_recent():   # Jarvis's own clicks and keys: that isn't anyone touching it
            s["watch_from"] = max(s["watch_from"], now + POLL)
            self._save(s)
            return False
        since = max(s["watch_from"], s["last_alert"] + REPEAT_AFTER if s["last_alert"] else 0)
        if now < since or now - idle_seconds() <= since:
            return False
        s["last_alert"] = now
        self._save(s)
        await self.alert(now)
        return True

    async def alert(self, when: float) -> None:
        log.warning("guard: the Mac was touched")
        at = time.strftime("%H:%M:%S", time.localtime(when))
        await self._deliver(self.send_text, f"🚨 Someone touched the Mac at {at}. Locking it, warning them out loud, taking photos.")
        shots, why = [], ""
        try:
            shots.append(await self.screenshot())     # first, while it still shows what they opened
        except Exception as e:
            log.warning("guard: no screenshot: %r", e)
        locked = await asyncio.to_thread(self.lock)
        sound = asyncio.create_task(self.warn())     # plays while the camera takes their photo
        if not locked:
            await self._deliver(self.send_text, "⚠️ I couldn't lock the Mac.")
        photo, why = await take_photo()
        if photo:
            shots.insert(0, photo)
        if shots:
            await self._deliver(self.send_photos, shots, f"🚨 The Mac at {at}")
        if why:
            await self._deliver(self.send_text, f"⚠️ No camera photo: {why}.")
        else:
            await asyncio.sleep(SECOND_PHOTO_AFTER)
            photo, _ = await take_photo()
            if photo:
                await self._deliver(self.send_photos, [photo], "🚨 A few seconds later")
        with contextlib.suppress(Exception):
            await sound

    async def test(self) -> str:
        """Take a camera photo now and send it, to check the camera (and to make macOS ask for it)."""
        photo, why = await take_photo()
        if not photo:
            return f"No photo: {why}."
        await self._deliver(self.send_photos, [photo], "Guard camera test")
        return "The camera works; the test photo is on Telegram."

    async def run_forever(self) -> None:
        while True:
            try:
                await self.check()
            except Exception:
                log.exception("guard check failed")
            await asyncio.sleep(POLL)

"""Where Jarvis keeps everything it makes while running (ticket 6ced33f0).

Root cause it fixes: every file used to be placed next to the code (`<jarvis>/var`), so a test run in the jarvis
folder used the real data, and each test had to remember to redirect each file; the ones it forgot (the app's
notification feed) put fake chats on Sanjar's phone. Now the data folder is one setting:

    JARVIS_VAR=<folder>      use that folder (tests: tests/conftest.py sets a fresh temporary one)
    (not set)                <jarvis>/var, the real one: only for the real Jarvis and its scripts

Under tests, JARVIS_VAR must be set: importing Jarvis code in a test without it fails at once, loudly, instead of
quietly writing into the real data. No other module may build a path to the data folder itself; they all use VAR.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def under_tests() -> bool:
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def data_folder() -> Path:
    chosen = os.environ.get("JARVIS_VAR", "").strip()
    if under_tests() and not chosen:
        raise RuntimeError("Tests must never use Jarvis's real data folder: set JARVIS_VAR to a temporary folder "
                           "(tests/conftest.py does it before importing any Jarvis code).")
    folder = Path(chosen).expanduser().resolve() if chosen else HERE / "var"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


VAR = data_folder()

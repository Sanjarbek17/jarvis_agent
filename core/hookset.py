"""The Claude Code settings every tool-using Claude that Jarvis starts must run with (ticket 81332fee).

Root cause it fixes: the hook that keeps pages out of Sanjar's real Chrome unless they go through
scripts/real_chrome.py (which remembers and closes them) was set only on the ticket runner's command line.
Mac tasks and the reel poster's backup worker started Claude without it, so a raw `open <url>` there still
left windows nobody closed. Every launcher now takes it from here: `["--settings", SETTINGS]`.

No Jarvis imports (only its own folder), so scripts in other projects (the poster) can use it.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SETTINGS = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": f"{HERE / '.venv/bin/python'} {HERE / 'scripts/hooks/no_raw_chrome.py'}"}]}]}})

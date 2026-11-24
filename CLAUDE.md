# Jarvis — notes for workers on Jarvis tickets

You run *inside* Jarvis: Jarvis started you as a ticket worker. Overview in README.md.

## Restarting Jarvis ("restart yourself" tickets)

Code changes to jarvis.py / core/ only go live after a restart. Never restart it directly
(`pkill -f jarvis.py`, `kill <pid>`, `launchctl kickstart -k`): that kills you and every other
running worker mid-ticket, and `pkill -f jarvis.py` also hits the caffeinate helper.

Instead start the waiting restart and finish your ticket:

    nohup scripts/restart_when_idle.sh >/dev/null 2>&1 &            # waits for active calls or chat answers
    nohup scripts/restart_when_idle.sh --now-ish >/dev/null 2>&1 &  # drops active calls too

It restarts Jarvis safely without waiting for running ticket workers (they run detached and
are adopted by the new instance); launchd brings it back within seconds.
Only one can wait at a time (a second one exits). Check `var/logs/restart.log`.

Not needed after editing config/projects.json: Jarvis reloads that by itself.

## Other rules
- Standing rules from Sanjar are in config/rules.md (also put into every worker's prompt).
- Git: work on your own branch, then merge it into `dev` and push `dev` yourself (no asking).
  Never touch main unless this ticket was explicitly allowed to; Sanjar tests `dev`, then has it
  merged to main in one go. If you made a worktree for the ticket, remove it after the merge to
  `dev` (from the main checkout) and delete the merged branch.
- Boards on the Mac ("local": true in projects.json) are read and changed with `scripts/board.py`, not Notion.
- Test tools through `core.tools.make_server(...).call_tool(...)`; test Notion changes on
  throwaway cards that are never "Queued" (the live runner would pick them up), then archive them.

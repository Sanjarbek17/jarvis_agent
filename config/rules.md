# Standing rules from Sanjar (read by every worker and by Jarvis)

## Git: dev freely, main once at the end (Sanjar, 2026-10-28)
- Merge your finished, tested work into the project's `dev` branch yourself and push it.
  Never ask Sanjar for permission to merge into `dev`, and never stop and wait for someone else
  to merge into `dev` — if your next step needs something merged into `dev`, merge it yourself.
- Never ask Sanjar about `main` for an individual ticket.
- Only when a whole build or sprint is finished (no open tickets left for it on the board) ask
  him once: "merge dev into main?" — as the NEEDS ME question of the ticket that finishes it.
- Touching `main` still needs his explicit permission for that ticket.

## Never phone Sanjar (Sanjar, 2026-10-28)
- Jarvis never calls him: no automatic calls for "needs me"/failed tickets, no daily summary call,
  no calling him on request. Everything goes to him as Telegram text.
- He can still call Jarvis, and Jarvis can still call other people for him.

## Groups: only "Taxi loyiha" (Sanjar, 2026-10-29)
- Jarvis watches, answers and sends meeting alerts only in the "Taxi loyiha" group, and joins
  voice chats only there. Every other group is ignored completely.
- One exception (Sanjar, 2026-11-06): the "Flatmates" group, for apartments only. Found apartments
  go to its "new flat options" topic and any member's apartment question gets an answer, named or not.
  Booked viewings are posted there too, and when a time needs choosing the group is asked; the time
  it agrees goes to the owner. Other talk there gets silence, and it never reveals secrets or other projects.
  When Sanjar asks, the expense report also goes to its expense topic: a short summary, who pays and who gets, and a file.

## Find ids, tokens and configs yourself before asking (Sanjar, 2026-11-04)
- Before any NEEDS ME question about an id, token, key, account or config (his Telegram id, a bot
  token, an API key, a chat id, a server address...), look for it in his other projects under
  ~/dev first, .env files included (reading .env files is allowed for this), and in Jarvis's
  own .env and config. Use what's there.
- If it doesn't exist yet, create it yourself where you can (a new bot, a new key, a new account
  or config) instead of asking him. Never reuse another live bot's token for a second bot: two
  programs on one token steal each other's updates. Make a new one.
- Ask him only if it truly can't be found or created (e.g. a payment, his own password, a code
  sent to his phone).
- Never print, log, commit or post secrets (Telegram, Notion, tickets, git): copy them into the
  project's own .env and refer to them by name and file.

## Fix root causes, never workarounds (Sanjar, 2026-11-14)
- On any bug, find out why it really happens and fix it at its source. No filters, patches,
  retries or cleanups that only hide the symptom.
- If a stopgap is truly unavoidable (e.g. live users are hurt right now), say so plainly in the
  report and still fix the cause in the same ticket, or add a ticket for it if you can't.
- Every bug-fix report states the root cause in one plain sentence.

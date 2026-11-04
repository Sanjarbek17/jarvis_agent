# Jarvis demo video for Upwork proposals (ticket fa10741e)

A 69 s, 1920x1080 walkthrough of Jarvis with an English voiceover and captions: request -> ticket,
the worker, status, the done update, long-term memory, draft-then-approve, the Mac task, and how the
same approach maps to an Outlook / Microsoft Graph portal. The chat and board are **recreated with
sample projects and names** (no private chats, numbers or clients); every flow shown is a real feature.

    set -a; . ../../.env; set +a
    ../../.venv/bin/python voice.py                 # storyboard.json -> audio/, timeline.js, narration.wav
    node render.mjs --frames                        # contact sheet in frames/sheet.png
    ../../scripts/heavy.py node render.mjs out.mp4  # full render

`node_modules` is a symlink to `code_explainer/animations/restoran-ai-uz/node_modules` (playwright-core);
`voice.py` and `render.mjs` come from that reel, changed to English and 16:9.

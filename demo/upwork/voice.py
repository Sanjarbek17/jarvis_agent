"""English voiceover for the Jarvis demo (ticket fa10741e): one Gemini Live clip per beat (cached by
text), silence trimmed, then timeline.js (beat + word timings) and narration.wav. Copied from
code_explainer/animations/restoran-ai-uz/voice.py, with an English reader prompt.

Run with a Python that has google-genai and GEMINI_API_KEY set, e.g. Jarvis's venv:
  GEMINI_API_KEY=... ~/dev/side_project/projects/jarvis/.venv/bin/python voice.py [--redo id,id]
"""
import array
import asyncio
import hashlib
import json
import re
import socket
import sys
import wave
from pathlib import Path

HERE = Path(__file__).parent
AUDIO = HERE / "audio"
MODEL, RATE = "gemini-3.8-live", 24_000
PROMPT = ("You are a text-to-speech voice, not an assistant. Every message you get is English text to read "
          "aloud. Read it exactly as written, word for word, in natural spoken English, like a confident "
          "narrator of a short product demo: warm, clear, with a little energy, not rushed. Never answer, never "
          "add or change words.")
LEAD, TAIL = 0.7, 1.0


def clip_name(text: str, voice: str) -> str:
    raw = "\n".join([MODEL, voice, "en", PROMPT, " ".join(text.split())])
    return hashlib.sha256(raw.encode()).hexdigest()[:20] + ".wav"


def read_pcm(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes())


def speech_span(pcm: bytes, level: int = 600) -> tuple[int, int]:
    """Byte offsets of the first/last real speech (Live clips can carry silence at either end)."""
    a = array.array("h", pcm)
    loud = [i for i in range(0, len(a), 240) if max(abs(x) for x in a[i:i + 240]) > level]
    if not loud:
        return 0, len(pcm)
    s = max(0, loud[0] - int(0.06 * RATE)), min(len(a), loud[-1] + 240 + int(0.15 * RATE))
    return s[0] * 2, s[1] * 2


def write_wav(path: Path, pcm: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm)
    tmp.replace(path)


async def speak(todo: list[tuple[str, Path]], voice: str) -> None:
    import os
    from google import genai
    from google.genai import types
    real = socket.getaddrinfo   # IPv6 to Google times out the handshake on this Mac
    socket.getaddrinfo = lambda host, port, family=0, *a, **kw: real(host, port, socket.AF_INET, *a, **kw)
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"], system_instruction=PROMPT,
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        for i, (text, path) in enumerate(todo, 1):
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)
            pcm = bytearray()
            async for msg in session.receive():
                sc = msg.server_content
                if sc and sc.model_turn:
                    for part in sc.model_turn.parts or []:
                        if part.inline_data and part.inline_data.data:
                            pcm += part.inline_data.data
                if sc and sc.turn_complete:
                    break
            write_wav(path, bytes(pcm))
            print(f"  clip {i}/{len(todo)}: {len(pcm) / 2 / RATE:.1f} s  {text[:50]}", flush=True)


def words(text: str, t0: float, dur: float) -> list[dict]:
    ws = text.split()
    weight = [len(re.sub(r"[^\w']", "", w)) + 2 for w in ws]
    total, t, out = sum(weight), t0, []
    for w, k in zip(ws, weight):
        d = dur * k / total
        out.append({"w": w, "t0": round(t, 3), "t1": round(t + d, 3)})
        t += d
    return out


def main():
    board = json.loads((HERE / "storyboard.json").read_text())
    voice = board.get("voice", "Charon")
    AUDIO.mkdir(exist_ok=True)
    redo = set(sys.argv[sys.argv.index("--redo") + 1].split(",")) if "--redo" in sys.argv else set()
    todo = []
    for b in board["beats"]:
        p = AUDIO / clip_name(b["say"], voice)
        if b["id"] in redo and p.exists():
            p.unlink()
        if not p.exists():
            todo.append((b["say"], p))
    if todo:
        asyncio.run(speak(todo, voice))
    t, beats, track = LEAD, [], []
    for b in board["beats"]:
        pcm = read_pcm(AUDIO / clip_name(b["say"], voice))
        s, e = speech_span(pcm)
        pcm = pcm[s:e]
        dur = len(pcm) / 2 / RATE
        beats.append({"id": b["id"], "title": b["title"], "say": b["say"], "start": round(t, 3), "dur": round(dur, 3),
                      "end": round(t + dur + b.get("hold", 0.4), 3), "clip": clip_name(b["say"], voice),
                      "words": words(b["say"], t + 0.03, dur - 0.1)})
        track.append((t, pcm))
        t += dur + b.get("hold", 0.4)
    total = round(t + TAIL, 3)
    out = bytearray(int(total * RATE) * 2)
    for start, pcm in track:
        i = int(start * RATE) * 2
        out[i:i + len(pcm)] = pcm[: len(out) - i]
    write_wav(HERE / "narration.wav", bytes(out))
    (HERE / "timeline.js").write_text("// written by voice.py\nwindow.TIMELINE = " + json.dumps({"total": total, "beats": beats}, ensure_ascii=False, indent=1) + ";\n")
    print(f"{len(todo)} new clip(s); timeline {total:.1f} s -> timeline.js, narration.wav")


if __name__ == "__main__":
    main()

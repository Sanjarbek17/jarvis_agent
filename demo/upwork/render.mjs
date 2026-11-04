// Records reel.html to MP4 with the narration: headless Chrome steps window.seek(t) for every
// frame, ffmpeg joins the frames and adds narration.wav. (Same method as
// projects/game/research/animation-demo/render.mjs.)
//   npm install                    once (playwright-core; uses the installed Chrome)
//   node render.mjs [out.mp4] [fps]
//   node render.mjs --frames [t1,t2,...]   only a contact sheet of single frames (check before rendering)
import { chromium } from "playwright-core";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const framesOnly = process.argv[2] === "--frames";
const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
const voice = process.env.VOICE || "";   // VOICE=charon -> voices/charon/{timeline.js,narration.wav}
const vdir = voice ? join(here, "voices", voice) : here;
await page.goto(pathToFileURL(join(here, "reel.html")).href + "?render=1" + (voice ? `&voice=${voice}` : ""));
await page.evaluate(() => document.fonts.ready);
await page.evaluate(() => Promise.all([...document.fonts].map((f) => f.load())));
const duration = await page.evaluate(() => window.DURATION);
const shot = async (t, path) => { await page.evaluate((x) => window.seek(x), t); await page.locator("#stage").screenshot({ path }); };

if (framesOnly) {
  const times = (process.argv[3] || "").split(",").filter(Boolean).map(Number);
  const ts = times.length ? times : Array.from({ length: 16 }, (_, i) => +(0.5 + i * (duration - 1) / 15).toFixed(2));
  const dir = join(here, "frames"); rmSync(dir, { recursive: true, force: true }); mkdirSync(dir, { recursive: true });
  for (const [i, t] of ts.entries()) await shot(t, join(dir, `t${String(i).padStart(2, "0")}.png`));
  await browser.close();
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-pattern_type", "glob", "-i", join(dir, "t*.png"),
    "-vf", "scale=480:-1,tile=4x4:padding=6:color=0x111111", join(dir, "sheet.png")]);
  console.log(`contact sheet: ${join(dir, "sheet.png")} at t = ${ts.join(", ")}`);
  process.exit(0);
}

const out = resolve(process.argv[2] ?? join(here, "jarvis-demo.mp4"));
const fps = Number(process.argv[3] ?? 30);
const frames = mkdtempSync(join(tmpdir(), "reel-"));
const total = Math.round(duration * fps);
for (let i = 0; i < total; i++) {
  await shot(i / fps, join(frames, `f${String(i).padStart(5, "0")}.png`));
  if (i % 150 === 0) console.log(`frame ${i}/${total}`);
}
await browser.close();
execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-framerate", String(fps), "-i", join(frames, "f%05d.png"),
  "-i", join(vdir, "narration.wav"), "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
  "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", out]);
rmSync(frames, { recursive: true });
console.log(`wrote ${out} (${total} frames, ${fps} fps, ${duration.toFixed(1)} s)`);

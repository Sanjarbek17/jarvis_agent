// Live Instagram numbers for one account, read right now from its own logged-in hidden Chrome (ticket 0960b065:
// "status" must never be yesterday's saved check). Followers from the profile header, views from the Reels grid.
// (Likes / saves aren't shown to a logged-in owner without opening insights: those come from the slower checks.)
//   node scripts/ig_stats.mjs --profile <chrome folder> --account <username> [--own-profile] [--reels 6]
// --own-profile: a Chrome folder made by playwright itself (instagram_channels/accounts/<ch>-chrome); without it
// the folder is a copy of a real Chrome profile (haydovchi_darslari/posting/chrome-profile), which needs the real keychain.
// Prints one JSON line: {account, followers, reels: [{url, views}], fetched_at} or {account, error}.
import { chromium } from "/Users/sanjarbek/dev/side_project/projects/haydovchi_darslari/studio/node_modules/playwright-core/index.mjs";

const arg = (name, dflt = "") => { const i = process.argv.indexOf(`--${name}`); return i > 0 ? process.argv[i + 1] : dflt; };
const profile = arg("profile"), account = arg("account"), want = Number(arg("reels", "6"));
const own = process.argv.includes("--own-profile");
const UA = own
  ? "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
  : "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36";

// "1,234" "4.9K" "1.2M" "12 ming" -> number (null if unreadable)
const num = (s) => {
  if (s == null) return null;
  const m = String(s).replace(/\s/g, "").replace(/,/g, "").match(/^([\d.]+)([KkMm]?)/);
  if (!m) return null;
  const n = parseFloat(m[1]) * ({ k: 1e3, m: 1e6 }[m[2].toLowerCase()] || 1);
  return Math.round(n);
};

let ctx;
const out = { account, fetched_at: new Date().toISOString() };
try {
  ctx = await chromium.launchPersistentContext(profile, {
    channel: "chrome", headless: true, viewport: { width: 1280, height: 900 }, userAgent: UA, locale: "en-US",
    ...(own ? {} : { ignoreDefaultArgs: ["--use-mock-keychain", "--password-store=basic"] }),
  });
  const page = ctx.pages()[0] || await ctx.newPage();
  await page.goto(`https://www.instagram.com/${account}/reels/`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(6000);
  if (/accounts\/login|challenge/.test(page.url())) throw new Error(`not logged in (${page.url()})`);
  const header = await page.locator("header").innerText().catch(() => "");
  out.followers = num(header.match(/([\d.,]+\s*[KkMm]?)\s+followers/)?.[1]);
  const tiles = page.locator('a[href*="/reel/"]');
  const reels = [];
  for (let i = 0; i < Math.min(await tiles.count(), want); i++) {
    const t = tiles.nth(i), href = await t.getAttribute("href");
    const code = href.match(/\/reel\/([^/]+)/)?.[1];
    const first = (await t.innerText().catch(() => "")).split("\n").map((s) => s.trim()).filter(Boolean)[0];
    reels.push({ url: `https://www.instagram.com/reel/${code}/`, views: num(first) });
  }
  out.reels = reels;
} catch (e) {
  out.error = String(e).split("\n")[0].slice(0, 300);
}
await ctx?.close().catch(() => {});
console.log(JSON.stringify(out));

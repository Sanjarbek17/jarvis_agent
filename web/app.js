// The Jarvis website: same backend and same login as the phone app (core/appserver.py, /api/*).
// All text from the server goes in through textContent, never as HTML.

const GENERAL = "General";
const UPDATES = "updates";
const TICK = 3000;
const MAX_WAIT = 10 * 60 * 1000;   // stop waiting for a reply the connection lost track of
const LOST_AFTER = 60 * 1000;      // ...or for a message that never showed up on the server
const VOICE_OK = window.isSecureContext && !!navigator.mediaDevices?.getUserMedia;
const VOICE_HELP = "Voice needs the https address of this site.";
const COLORS = ["#2f6fed", "#30a46c", "#d6409f", "#f76b15", "#8e4ec6", "#0090ff", "#12a594"];
const ICONS = {
  mic: "M12 15a3.5 3.5 0 0 0 3.5-3.5v-6a3.5 3.5 0 0 0-7 0v6A3.5 3.5 0 0 0 12 15Zm6.5-3.5a1 1 0 1 0-2 0 4.5 4.5 0 0 1-9 0 1 1 0 1 0-2 0 6.5 6.5 0 0 0 5.5 6.4V20H9a1 1 0 1 0 0 2h6a1 1 0 1 0 0-2h-2v-2.1a6.5 6.5 0 0 0 5.5-6.4Z",
  send: "M3.4 20.6 22 12 3.4 3.4 3.4 10l12 2-12 2z",
  phone: "M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.25 11 11 0 0 0 3.6.6 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1 11 11 0 0 0 .6 3.6 1 1 0 0 1-.25 1z",
  close: "M18.3 5.7a1 1 0 0 0-1.4 0L12 10.6 7.1 5.7a1 1 0 0 0-1.4 1.4l4.9 4.9-4.9 4.9a1 1 0 1 0 1.4 1.4l4.9-4.9 4.9 4.9a1 1 0 0 0 1.4-1.4L13.4 12l4.9-4.9a1 1 0 0 0 0-1.4Z",
};

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* still works this session */ } },
  drop(key) { try { localStorage.removeItem(key); } catch { /* nothing to drop */ } },
};

let session = store.get("jarvis.session");
let projects = [];
let chats = [];            // the chats of the project on screen
let chatsOf = null;
let open = null;           // the chat on screen: {id, project, msgs, key, found}
let unread = Number(store.get("jarvis.unread")) || 0;
let workers = null;
let ticks = 0;
let timer = null;
const waiting = new Map(); // chat id -> {since, text, voice, lost, at}: a message Jarvis is answering
const drafts = new Map();
const images = new Map();  // image name -> a promise of its blob: URL

// --- small helpers ---------------------------------------------------------------------------

function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "text") el.textContent = value;
    else if (value === true) el.setAttribute(key, "");
    else if (value !== false && value != null) el.setAttribute(key, value);
  }
  el.append(...kids.filter(k => k != null));
  return el;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

function avatar(name) {
  let sum = 0;
  for (const c of name) sum = (sum * 31 + c.codePointAt(0)) % 9973;
  const el = h("span", { class: "avatar", text: [...name][0]?.toUpperCase() || "?", "aria-hidden": "true" });
  el.style.background = COLORS[sum % COLORS.length];
  return el;
}

function toast(text, onclick) {
  const el = h(onclick ? "button" : "div", { class: "toast", text });
  if (onclick) el.addEventListener("click", () => { el.remove(); onclick(); });
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), onclick ? 20000 : 6000);
}

function ago(seconds) {
  if (!seconds) return "";
  const gone = Date.now() / 1000 - seconds;
  if (gone < 60) return "now";
  if (gone < 3600) return `${Math.floor(gone / 60)}m`;
  if (gone < 86400) return `${Math.floor(gone / 3600)}h`;
  return new Date(seconds * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
}

function clock(seconds) {
  const when = new Date(seconds * 1000);
  const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return when.toDateString() === new Date().toDateString()
    ? time : `${when.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
}

const chatLink = (project, id) => `#/c/${encodeURIComponent(project)}/${encodeURIComponent(id)}`;
const projectLink = project => `#/p/${encodeURIComponent(project)}`;
const about = name => projects.find(p => p.name === name)?.about || "";

function device() {
  const ua = navigator.userAgent;
  const browser = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari" : "browser";
  const system = /iPhone|iPad/.test(ua) ? "iPhone" : /Android/.test(ua) ? "Android" : /Mac/.test(ua) ? "Mac"
    : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "computer";
  return `Website: ${browser} on ${system}`;
}

// --- talking to Jarvis -----------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
    // Cloudflare gives up on slow answers and connections drop, while Jarvis keeps working on it.
    this.unsure = status === 0 || status === 502 || status === 504 || (status >= 520 && status <= 524);
  }
}

async function request(path, { method = "GET", json, raw } = {}) {
  const headers = {};
  if (session) headers.Authorization = `Bearer ${session}`;
  if (json) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: json ? JSON.stringify(json) : raw });
  } catch {
    throw new ApiError("Can't reach Jarvis.", 0);
  }
  if (res.status === 401 && session) {
    endSession();
    throw new ApiError("Please log in again.", 401);
  }
  return res;
}

async function api(path, options) {
  const res = await request(path, options);
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(body?.error || `Jarvis said ${res.status}.`, res.status);
  return body;
}

function endSession() {
  session = null;
  store.drop("jarvis.session");
  for (const url of images.values()) url.then(URL.revokeObjectURL, () => {});
  images.clear();
  waiting.clear();
  open = null;
  chatsOf = null;
  clearInterval(timer);
  showLogin();
}

function image(name) {
  if (!images.has(name)) {
    const url = request(`/api/images/${encodeURIComponent(name)}`).then(async res => {
      if (!res.ok) throw new ApiError("no image", res.status);
      return URL.createObjectURL(await res.blob());
    });
    url.catch(() => images.delete(name));
    images.set(name, url);
  }
  return images.get(name);
}

// --- login -----------------------------------------------------------------------------------

function showLogin() {
  const error = h("p", { class: "error", role: "alert" });
  const code = h("input", {
    class: "field code", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6",
    placeholder: "000000", "aria-label": "Login code",
  });
  const ask = h("button", { class: "btn primary", type: "button", text: "Send a code to my Telegram" });
  const go = h("button", { class: "btn primary", type: "submit", text: "Log in" });
  const again = h("button", { class: "link small", type: "button", text: "Send a new code" });
  const hint = h("p", { class: "muted", text: "Log in with a code from your Telegram." });
  const second = [code, go, again];

  const stage = sent => {
    second.forEach(el => el.classList.toggle("hide", !sent));
    ask.classList.toggle("hide", sent);
    if (sent) {
      hint.textContent = "Type the 6-digit code Jarvis sent to your Telegram.";
      code.focus();
    }
  };
  const start = async () => {
    error.textContent = "";
    ask.disabled = again.disabled = true;
    try {
      await api("/api/login/start", { method: "POST", json: { device: device() } });
      stage(true);
    } catch (e) {
      error.textContent = e.message;
    }
    ask.disabled = again.disabled = false;
  };
  const submit = async event => {
    event.preventDefault();
    if (go.classList.contains("hide")) return start();
    error.textContent = "";
    go.disabled = true;
    try {
      const res = await api("/api/login", { method: "POST", json: { code: code.value.trim(), device: device() } });
      session = res.token;
      store.set("jarvis.session", session);
      await showApp();
    } catch (e) {
      error.textContent = e.message;
      code.select();
    }
    go.disabled = false;
  };

  ask.addEventListener("click", start);
  again.addEventListener("click", start);
  document.getElementById("root").replaceChildren(h("main", { class: "login" },
    h("form", { onsubmit: submit }, h("img", { src: "icon.svg", alt: "" }), h("h1", { text: "Jarvis" }),
      hint, ask, code, go, error, again)));
  stage(false);
}

// --- the screens -----------------------------------------------------------------------------

async function showApp() {
  document.getElementById("root").replaceChildren(h("div", { class: "shell", id: "shell", "data-view": "home" },
    h("section", { class: "pane side", id: "side" }),
    h("section", { class: "pane chats", id: "chats" }),
    h("section", { class: "pane main", id: "main" })));
  renderSide();
  try {
    projects = await api("/api/projects");
  } catch (e) {
    if (e.status === 401) return;
    offline(true);
  }
  renderSide();
  clearInterval(timer);
  timer = setInterval(tick, TICK);
  route();
  pollWorkers();
  pollNews();
}

function offline(on) {
  const bar = document.getElementById("offline");
  if (on && !bar) document.body.append(h("div", { class: "offline", id: "offline", text: "Can't reach Jarvis. Trying again…" }));
  if (!on) bar?.remove();
}

function renderSide() {
  const side = document.getElementById("side");
  if (!side) return;
  const here = location.hash;
  const count = workers?.count || 0;
  const updates = chatLink(GENERAL, UPDATES);
  side.replaceChildren(
    h("header", { class: "top" }, h("img", { class: "brand-mark", src: "icon.svg", alt: "" }), h("h1", { class: "brand", text: "Jarvis" })),
    h("nav", { class: "list", "aria-label": "Jarvis" },
      h("a", { class: `row ${here === updates ? "on" : ""}`, href: updates },
        avatar("Updates"),
        h("span", { class: "grow" }, h("div", { class: "name", text: "Updates from Jarvis" }),
          h("div", { class: "about", text: "Ticket news and questions" })),
        unread ? h("span", { class: "badge", text: String(unread) }) : null),
      h("a", { class: `row ${count ? "busy" : ""} ${here === "#/workers" ? "on" : ""}`, href: "#/workers" },
        h("span", { class: "dot" }),
        h("span", { class: "grow" }, h("div", { class: "name", text: "Workers" }),
          h("div", { class: "about", text: count ? `${count} running` : "Nobody is working" }))),
      h("div", { class: "section", text: "Projects" }),
      ...projects.map(p => h("a", {
        class: `row ${p.name === activeProject() && !here.startsWith("#/workers") && here !== updates ? "on" : ""}`,
        href: projectLink(p.name),
      }, avatar(p.name), h("span", { class: "grow" }, h("div", { class: "name", text: p.name }),
        h("div", { class: "about", text: p.about }))))),
    h("footer", { class: "foot" }, h("button", { text: "Log out", onclick: logout })));
  document.title = unread ? `(${unread}) Jarvis` : "Jarvis";
}

async function logout() {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  if (session) endSession();
}

function parseRoute() {
  try {
    const [kind, one, two] = location.hash.slice(2).split("/").map(decodeURIComponent);
    if (kind === "p" && one) return { view: "chats", project: one };
    if (kind === "c" && one && two) return { view: "main", project: one, chat: two };
    if (kind === "workers") return { view: "main", workers: true };
  } catch { /* a broken link: go home */ }
  return { view: "home" };
}

const activeProject = () => parseRoute().project || GENERAL;

function route() {
  const shell = document.getElementById("shell");
  if (!shell) return;
  const where = parseRoute();
  shell.dataset.view = where.view;
  renderSide();
  renderChats(where.project || GENERAL);
  if (where.workers) showWorkers();
  else if (where.chat) showChat(where.project, where.chat);
  else showNothing();
}

function showNothing() {
  open = null;
  document.getElementById("main").replaceChildren(
    h("div", { class: "empty", text: "Pick a chat, or start a new one." }));
}

async function renderChats(project) {
  const pane = document.getElementById("chats");
  if (chatsOf !== project) {
    chats = [];
    chatsOf = project;
    pane.replaceChildren(chatsTop(project), h("div", { class: "empty", text: "Loading…" }));
  }
  try {
    const items = (await api(`/api/chats?project=${encodeURIComponent(project)}`)).filter(c => c.id !== UPDATES);
    if (chatsOf !== project) return;
    chats = items;
    drawChats(project);
  } catch (e) {
    if (e.status !== 401 && chatsOf === project && !chats.length) {
      pane.replaceChildren(chatsTop(project), h("div", { class: "empty", text: e.message }));
    }
  }
}

function chatsTop(project) {
  return h("header", { class: "top" },
    h("button", { class: "back", "aria-label": "Back to projects", text: "‹", onclick: () => { location.hash = "#/"; } }),
    h("div", { class: "grow" }, h("h2", { text: project }), h("div", { class: "sub", text: about(project) })),
    h("button", { class: "new", title: "New chat", "aria-label": "New chat", text: "+", onclick: () => newChat(project) }));
}

function drawChats(project) {
  const title = document.getElementById("chat-title");
  const named = chats.find(c => c.id === open?.id);
  if (title && named) title.textContent = named.title;
  const rows = chats.map(c => h("a", { class: `row ${c.id === open?.id ? "on" : ""}`, href: chatLink(project, c.id) },
    h("span", { class: "grow" }, h("div", { class: "name", text: c.title }), h("div", { class: "about", text: c.last.replace(/\*\*|`/g, "") || "No messages yet" })),
    h("span", { class: "when", text: ago(c.updated) })));
  document.getElementById("chats").replaceChildren(chatsTop(project), h("nav", { class: "list", "aria-label": `Chats in ${project}` },
    ...(rows.length ? rows : [h("div", { class: "empty", text: "No chats here yet." })])));
}

async function newChat(project) {
  try {
    const chat = await api("/api/chats", { method: "POST", json: { project } });
    chats.unshift(chat);
    location.hash = chatLink(project, chat.id);
  } catch (e) {
    if (e.status !== 401) toast(e.message);
  }
}

// --- a chat ----------------------------------------------------------------------------------

function showChat(project, id) {
  if (open?.id === id && document.getElementById("messages")) return;
  const chat = open = { id, project, msgs: [], key: null, found: true };
  const title = id === UPDATES ? "Updates from Jarvis" : chats.find(c => c.id === id)?.title || "Chat";
  document.getElementById("main").replaceChildren(
    h("header", { class: "top" },
      h("button", { class: "back", "aria-label": "Back to chats", text: "‹", onclick: () => { location.hash = projectLink(project); } }),
      h("div", { class: "grow" }, h("h2", { id: "chat-title", text: title }), h("div", { class: "sub", text: project })),
      h("button", { class: "round", title: "Call Jarvis", "aria-label": "Call Jarvis", onclick: () => startCall(chat) }, icon("phone"))),
    h("div", { class: "messages", id: "messages", role: "log", "aria-label": "Messages" },
      h("div", { class: "notice first", text: "Loading…" })),
    composer(chat));
  document.getElementById("composer-text")?.focus();
  if (id === UPDATES) clearUnread();
  drawChatsIfOpen();
  loadMessages(chat);
}

function drawChatsIfOpen() {
  if (chats.length) drawChats(chatsOf);
}

function clearUnread() {
  unread = 0;
  store.set("jarvis.unread", "0");
  renderSide();
}

async function loadMessages(chat) {
  try {
    const msgs = await api(`/api/chats/${encodeURIComponent(chat.id)}/messages`);
    if (open !== chat) return;
    chat.msgs = msgs;
    chat.found = true;
    offline(false);
    settle(chat);
    drawMessages(chat);
  } catch (e) {
    if (open !== chat || e.status === 401) return;
    if (e.status === 404) {
      chat.found = false;
      drawMessages(chat);
    } else if (e.unsure) {
      offline(true);
    }
  }
}

/** A reply the connection lost track of: see if it arrived, or give up on it. */
function settle(chat) {
  const wait = waiting.get(chat.id);
  if (!wait?.lost) return;
  const news = chat.msgs.filter(m => m.ts > wait.since);
  const mine = news.findIndex(m => m.role === "me");
  const gone = Date.now() - wait.at;
  if (mine >= 0 && news.slice(mine).some(m => m.role === "jarvis" && !m.pushed)) waiting.delete(chat.id);
  else if (gone > MAX_WAIT || (mine < 0 && gone > LOST_AFTER)) {
    waiting.delete(chat.id);
    toast(mine < 0 ? "That message didn't reach Jarvis. Try again." : "Still no reply from Jarvis.");
  }
}

function drawMessages(chat) {
  const box = document.getElementById("messages");
  if (!box) return;
  const wait = waiting.get(chat.id);
  const last = chat.msgs[chat.msgs.length - 1];
  const key = `${chat.found}|${chat.msgs.length}|${last?.ts}|${wait?.since}|${!!wait}`;
  if (key === chat.key) return;
  const stuck = chat.key === null || box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  chat.key = key;

  const out = [];
  let before = null;
  for (const m of chat.msgs) {
    out.push(bubble(m, before !== null && before !== m.role));
    before = m.role;
  }
  if (wait) {
    const arrived = chat.msgs.some(m => m.role === "me" && m.ts > wait.since);
    if (!arrived) out.push(bubble({ role: "me", text: wait.text, voice: wait.voice }, before === "jarvis"));
    out.push(h("div", { class: "bubble gap typing", text: "Jarvis is working on it…" }));
  }
  if (!out.length) {
    out.push(h("div", { class: "notice", text: chat.found ? "No messages yet. Say something." : "This chat isn't there." }));
  }
  out[0].classList.add("first");
  box.replaceChildren(...out);
  if (stuck) box.scrollTop = box.scrollHeight;
}

function bubble(m, gap) {
  const el = h("div", { class: `bubble ${m.role === "me" ? "me" : ""} ${gap ? "gap" : ""}` });
  for (const name of m.images || []) {
    const pic = h("img", { class: "pic", alt: "Image from Jarvis", onclick: () => lightbox(pic.src) });
    image(name).then(url => { pic.src = url; }, () => pic.replaceWith(h("div", { class: "muted small", text: "Image not available" })));
    el.append(pic);
  }
  if (m.text) el.append(h("div", { class: "text" }, rich(m.text)));
  const from = m.chat_title ? `from ${m.chat_title}` : "";
  const meta = [m.voice ? "voice" : "", m.ts ? clock(m.ts) : "sending"].filter(Boolean).join(" · ");
  el.append(h("div", { class: "meta" }, from && m.chat_project ? h("a", { href: chatLink(m.chat_project, m.chat), text: from }) : from,
    from ? " · " : "", meta));
  return el;
}

function lightbox(src) {
  if (!src) return;
  const close = () => { box.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = e => { if (e.key === "Escape") close(); };
  const box = h("button", { class: "lightbox", "aria-label": "Close image", onclick: close }, h("img", { src, alt: "" }));
  document.addEventListener("keydown", onKey);
  document.body.append(box);
  box.focus();
}

/** Jarvis's light Markdown as DOM nodes: code fences, **bold**, `code`, bullets and links. */
function rich(text) {
  const out = document.createDocumentFragment();
  text.split(/```[^\n]*\n?/).forEach((part, i) => {
    if (i % 2) out.append(h("pre", { text: part.replace(/\n$/, "") }));
    else if (part) inline(part.replace(/^[ \t]*[-*][ \t]+/gm, "•  "), out);
  });
  return out;
}

function inline(text, out) {
  const marks = /\*\*(.+?)\*\*|`([^`\n]+)`|(https?:\/\/[^\s<>()\]]+)/g;
  let at = 0;
  for (const m of text.matchAll(marks)) {
    out.append(text.slice(at, m.index));
    at = m.index + m[0].length;
    if (m[1]) out.append(h("strong", { text: m[1] }));
    else if (m[2]) out.append(h("code", { text: m[2] }));
    else {
      const url = m[3].replace(/[.,;:!?'"]+$/, "");
      at -= m[3].length - url.length;
      out.append(h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: url }));
    }
  }
  out.append(text.slice(at));
}

function composer(chat) {
  const text = h("textarea", {
    id: "composer-text", rows: "1", placeholder: "Message Jarvis", "aria-label": "Message", maxlength: "20000",
  });
  const sendBtn = h("button", { class: "round primary", title: "Send", "aria-label": "Send" }, icon("send"));
  const micBtn = h("button", { class: "round", title: "Voice message", "aria-label": "Voice message" }, icon("mic"));
  const form = h("div", { class: "composer" }, text, micBtn, sendBtn);

  const refresh = () => {
    text.style.height = "auto";
    text.style.height = `${Math.min(text.scrollHeight + 2, 180)}px`;
    sendBtn.classList.toggle("hide", !text.value.trim());
    micBtn.classList.toggle("hide", !!text.value.trim());
    if (text.value) drafts.set(chat.id, text.value);
    else drafts.delete(chat.id);
  };
  const submit = () => {
    const value = text.value.trim();
    if (!value) return;
    if (waiting.has(chat.id)) return toast("Jarvis is still answering the last one.");
    text.value = "";
    refresh();
    send(chat, value);
  };

  text.value = drafts.get(chat.id) || "";
  text.addEventListener("input", refresh);
  text.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });
  sendBtn.addEventListener("click", submit);
  micBtn.addEventListener("click", () => record(chat, form, text));
  queueMicrotask(refresh);
  return form;
}

async function send(chat, text, audio) {
  const known = open?.id === chat.id ? open.msgs : chat.msgs;
  const since = known[known.length - 1]?.ts || 0;
  const wait = { since, text: audio ? "Voice message" : text, voice: !!audio, lost: false, at: Date.now() };
  waiting.set(chat.id, wait);
  drawMessages(chat);
  const path = `/api/chats/${encodeURIComponent(chat.id)}`;
  try {
    const res = audio
      ? await api(`${path}/voice`, { method: "POST", raw: audio })
      : await api(`${path}/messages`, { method: "POST", json: { text } });
    waiting.delete(chat.id);
    if (audio && !res.heard) toast("Jarvis couldn't make out that voice message.");
  } catch (e) {
    if (e.status === 401) return;
    if (e.unsure) wait.lost = true;    // Jarvis may well be answering: the regular check picks it up
    else {
      waiting.delete(chat.id);
      toast(e.message);
    }
  }
  if (open === chat) await loadMessages(chat);
  if (open === chat && !waiting.has(chat.id)) drawMessages(chat);
  if (chatsOf === chat.project) renderChats(chat.project);
}

async function record(chat, form, text) {
  if (!VOICE_OK) return toast(VOICE_HELP);
  if (waiting.has(chat.id)) return toast("Jarvis is still answering the last one.");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch {
    return toast("Jarvis needs the microphone for voice messages.");
  }
  const type = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(t => MediaRecorder.isTypeSupported(t));
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const parts = [];
  const started = Date.now();
  let cancelled = false;
  const label = h("div", { class: "recording", role: "status", text: "0:00" });
  const counter = setInterval(() => {
    const seconds = Math.floor((Date.now() - started) / 1000);
    label.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }, 500);
  const saved = [...form.children];
  const done = keep => { cancelled = !keep; recorder.stop(); };
  form.replaceChildren(
    h("button", { class: "round", title: "Cancel", "aria-label": "Cancel recording", onclick: () => done(false) }, icon("close")),
    label,
    h("button", { class: "round primary", title: "Send voice message", "aria-label": "Send voice message", onclick: () => done(true) }, icon("send")));
  recorder.ondataavailable = e => parts.push(e.data);
  recorder.onstop = () => {
    clearInterval(counter);
    stream.getTracks().forEach(track => track.stop());
    form.replaceChildren(...saved);
    text.focus();
    const audio = new Blob(parts, { type: recorder.mimeType || type || "audio/webm" });
    if (!cancelled && audio.size) send(chat, "", audio);
  };
  recorder.start();
}

// --- a live call, over the same socket as the phone app ------------------------------------

async function startCall(chat) {
  if (!VOICE_OK) return toast(VOICE_HELP);
  if (document.getElementById("call")) return;
  const status = h("p", { class: "muted", role: "status", text: "Connecting…" });
  const lines = h("div", { class: "transcript" });
  const muteBtn = h("button", { class: "round", title: "Mute", "aria-label": "Mute", "aria-pressed": "false" }, icon("mic"));
  const endBtn = h("button", { class: "round danger", title: "Hang up", "aria-label": "Hang up" }, icon("close"));
  const overlay = h("div", { class: "call", id: "call", role: "dialog", "aria-label": "Call with Jarvis" },
    h("span", { class: "avatar", text: "J", "aria-hidden": "true" }), h("h1", { text: "Jarvis" }),
    chat.project !== GENERAL ? h("p", { class: "muted small", text: chat.project }) : null,
    status, lines, h("div", { class: "buttons" }, muteBtn, endBtn));
  document.body.append(overlay);

  let stream, ctx, ws, lock, node;
  let over = false;
  let playAt = 0;
  const stop = why => {
    if (over) return;
    over = true;
    status.textContent = why;
    node?.port.close();
    stream?.getTracks().forEach(track => track.stop());
    ctx?.close().catch(() => {});
    if (ws && ws.readyState < 2) ws.close(1000, "hang up");
    lock?.release().catch(() => {});
  };
  endBtn.addEventListener("click", () => {
    stop("Call ended");
    overlay.remove();
  });
  muteBtn.addEventListener("click", () => {
    const track = stream?.getAudioTracks()[0];
    if (!track) return;
    track.enabled = !track.enabled;
    muteBtn.classList.toggle("off", !track.enabled);
    muteBtn.setAttribute("aria-pressed", String(!track.enabled));
  });

  try {
    ctx = new AudioContext({ sampleRate: 48000, latencyHint: "interactive" });   // while the click still counts
    ctx.resume().catch(() => {});
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    if (over) return stream.getTracks().forEach(track => track.stop());
    let source;
    try {
      source = ctx.createMediaStreamSource(stream);
    } catch {                       // Firefox wants the mic's own rate; the worklet resamples
      ctx.close().catch(() => {});
      ctx = new AudioContext({ latencyHint: "interactive" });
      source = ctx.createMediaStreamSource(stream);
    }
    ctx.resume().catch(() => {});
    await ctx.audioWorklet.addModule("mic-worklet.js");
    node = new AudioWorkletNode(ctx, "mic", { numberOfOutputs: 1 });
    const silent = ctx.createGain();
    silent.gain.value = 0;
    source.connect(node).connect(silent).connect(ctx.destination);
    lock = await navigator.wakeLock?.request("screen").catch(() => null);
  } catch (e) {
    stop(e.name === "NotAllowedError" ? "Jarvis needs the microphone for calls." : "Can't open the microphone.");
    return;
  }
  if (over) return;

  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams({ token: session, chat: chat.id });
  ws = new WebSocket(`${scheme}://${location.host}/api/call?${params}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    if (over) return;
    status.textContent = "On the call";
    node.port.onmessage = e => { if (ws.readyState === 1) ws.send(e.data); };
  };
  ws.onmessage = e => {
    if (typeof e.data !== "string") {
      playAt = play(ctx, e.data, playAt);
      return;
    }
    try {
      const line = JSON.parse(e.data);
      lines.append(h("p", { text: `${line.who}: ${line.text}` }));
      lines.scrollTop = lines.scrollHeight;
    } catch { /* not a transcript line */ }
  };
  ws.onclose = e => {
    const reason = { 4401: "Please log in again.", 4409: "Jarvis is already on another call.", 4503: "Calls aren't up right now." };
    stop(reason[e.code] || (e.code === 1000 ? "Call ended" : "Lost Jarvis"));
    endBtn.setAttribute("aria-label", "Close");
    endBtn.title = "Close";
  };
}

/** 24 kHz mono s16 from Jarvis, one short piece after the other. Returns when the next one should start. */
function play(ctx, data, playAt) {
  if (ctx.state === "closed") return playAt;
  const pcm = new Int16Array(data);
  const buffer = ctx.createBuffer(1, pcm.length, 24000);
  const out = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) out[i] = pcm[i] / 32768;
  const piece = ctx.createBufferSource();
  piece.buffer = buffer;
  piece.connect(ctx.destination);
  const now = ctx.currentTime;
  const at = playAt > now && playAt < now + 0.6 ? playAt : now + 0.12;   // small cushion; never lag far behind
  piece.start(at);
  return at + buffer.duration;
}

// --- workers ---------------------------------------------------------------------------------

function showWorkers() {
  open = null;
  document.getElementById("main").replaceChildren(
    h("header", { class: "top" },
      h("button", { class: "back", "aria-label": "Back", text: "‹", onclick: () => { location.hash = "#/"; } }),
      h("div", { class: "grow" }, h("h2", { text: "Workers" }), h("div", { class: "sub", id: "workers-sub" }))),
    h("div", { class: "workers", id: "workers" }));
  drawWorkers();
  pollWorkers();
}

function drawWorkers() {
  const box = document.getElementById("workers");
  if (!box || !workers) return;
  const n = workers.count;
  document.getElementById("workers-sub").textContent = n ? `${n} running` : "Nobody is working";
  const note = workers.error || (workers.waiting ? `Next in line: ${workers.waiting}` : n ? "" : "No tickets are being worked on right now.");
  box.replaceChildren(
    ...(note ? [h("p", { class: "muted", text: note })] : []),
    ...workers.workers.map(w => {
      const name = `${w.project}: ${w.title}`;
      return h("article", { class: "worker" },
        /^https:\/\//.test(w.url || "")
          ? h("a", { class: "name", href: w.url, target: "_blank", rel: "noopener noreferrer", text: name })
          : h("div", { class: "name", text: name }),
        h("div", { class: "muted small", text: `${w.ref}${w.started ? ` · started ${ago(w.started)}${ago(w.started) === "now" ? "" : " ago"}` : ""}` }),
        h("div", { class: "step", text: w.step || "working" }));
    }));
}

async function pollWorkers() {
  try {
    workers = await api("/api/workers");
    offline(false);
    renderSideKeepingScroll();
    drawWorkers();
  } catch (e) {
    if (e.unsure) offline(true);
  }
}

function renderSideKeepingScroll() {
  const list = document.querySelector("#side .list");
  const top = list?.scrollTop || 0;
  renderSide();
  const fresh = document.querySelector("#side .list");
  if (fresh) fresh.scrollTop = top;
}

// --- what Jarvis wrote on its own ------------------------------------------------------------

async function pollNews() {
  let after = Number(store.get("jarvis.after"));
  try {
    const res = await api(`/api/notifications?after=${after || 0}`);
    const items = res.items;
    if (!items.length) {
      if (!after) store.set("jarvis.after", String(Date.now() / 1000));
      return;
    }
    store.set("jarvis.after", String(items[items.length - 1].ts));
    if (!after) return;               // first visit: old news stays in the Updates chat
    if (open && (open.id === UPDATES || items.some(i => i.chat === open.id))) loadMessages(open);
    if (items.some(i => i.chat_project === chatsOf)) renderChats(chatsOf);
    if (open?.id === UPDATES) return;
    const fresh = items.filter(i => i.chat !== open?.id);
    unread += fresh.length;
    store.set("jarvis.unread", String(unread));
    renderSideKeepingScroll();
    fresh.slice(-3).forEach(newsToast);
  } catch { /* the next check tries again */ }
}

/** News from a chat opens that chat; the rest opens Updates. */
function newsToast(item) {
  const inChat = item.chat && item.chat_project;
  const text = (item.text || "Jarvis sent an image").replace(/\*\*|`/g, "").slice(0, 140);
  toast(inChat ? `${item.chat_title}: ${text}` : text, () => {
    location.hash = inChat ? chatLink(item.chat_project, item.chat) : chatLink(GENERAL, UPDATES);
    if (inChat) {
      unread = Math.max(0, unread - 1);
      store.set("jarvis.unread", String(unread));
      renderSideKeepingScroll();
    }
  });
}

function tick() {
  ticks++;
  if (ticks % 10 === 0) pollNews();
  if (document.hidden) return;
  if (open && (waiting.has(open.id) || ticks % 4 === 0)) loadMessages(open);
  if (document.getElementById("workers") ? ticks % 2 === 0 : ticks % 5 === 0) pollWorkers();
}

window.addEventListener("hashchange", () => { if (session) route(); });
if (session) showApp();
else showLogin();

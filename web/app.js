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
  clip: "M16.5 6.5v10a4.5 4.5 0 0 1-9 0V5a3 3 0 0 1 6 0v10.5a1.5 1.5 0 0 1-3 0V6.5H9v9a3 3 0 0 0 6 0V5a4.5 4.5 0 0 0-9 0v11.5a6 6 0 0 0 12 0v-10z",
  phone: "M6.6 10.8a15 15 0 0 0 6.6 6.6l2.2-2.2a1 1 0 0 1 1-.25 11 11 0 0 0 3.6.6 1 1 0 0 1 1 1V20a1 1 0 0 1-1 1A17 17 0 0 1 3 4a1 1 0 0 1 1-1h3.5a1 1 0 0 1 1 1 11 11 0 0 0 .6 3.6 1 1 0 0 1-.25 1z",
  move: "M3 6a2 2 0 0 1 2-2h4.6a2 2 0 0 1 1.4.6L12.4 6H19a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zm9.3 4.3a1 1 0 0 0 0 1.4l.3.3H8a1 1 0 1 0 0 2h4.6l-.3.3a1 1 0 0 0 1.4 1.4l2-2a1 1 0 0 0 0-1.4l-2-2a1 1 0 0 0-1.4 0z",
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
const boards = new Map();  // project -> the last /api/board answer, so a board opens at once
let boardOn = null;        // the board on screen: {project, show: "open"|"done"|"all", find, adding}

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
const boardLink = project => `#/b/${encodeURIComponent(project)}`;
const ticketLink = (project, ref) => `#/t/${encodeURIComponent(project)}/${encodeURIComponent(ref)}`;
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
    if (kind === "b" && one) return { view: "main", project: one, board: true };
    if (kind === "t" && one && two) return { view: "main", project: one, ticket: two };
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
  else if (where.board) showBoard(where.project);
  else if (where.ticket) showTicket(where.project, where.ticket);
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
  const boardRow = project === GENERAL ? null : h("a", { class: `row ${location.hash.startsWith(boardLink(project)) ? "on" : ""}`, href: boardLink(project) },
    h("span", { class: "grow" }, h("div", { class: "name", text: "Board" }), h("div", { class: "about", text: "Tickets for this project" })));
  document.getElementById("chats").replaceChildren(chatsTop(project), h("nav", { class: "list", "aria-label": `Chats in ${project}` }, boardRow,
    ...(rows.length ? rows : [h("div", { class: "empty", text: "No chats here yet." })])));
}

async function newChat(project) {
  try {
    const chat = await api("/api/chats", { method: "POST", json: { project } });
    chats.unshift(chat);
    location.hash = chatLink(project, chat.id);
    pollProjects();
  } catch (e) {
    if (e.status !== 401) toast(e.message);
  }
}

// --- a chat ----------------------------------------------------------------------------------

function showChat(project, id) {
  if (open?.id === id && open.project === project && document.getElementById("messages")) return;
  const chat = open = { id, project, msgs: [], key: null, found: true, pinned: true, newest: null, bubbles: new Map() };
  const title = id === UPDATES ? "Updates from Jarvis" : chats.find(c => c.id === id)?.title || "Chat";
  document.getElementById("main").replaceChildren(
    h("header", { class: "top" },
      h("button", { class: "back", "aria-label": "Back to chats", text: "‹", onclick: () => { location.hash = projectLink(project); } }),
      h("div", { class: "grow" }, h("h2", { id: "chat-title", text: title }), h("div", { class: "sub", text: project })),
      id === UPDATES ? null : h("button", { class: "round", title: "Move to another project", "aria-label": "Move to another project", onclick: () => moveSheet(chat) }, icon("move")),
      h("button", { class: "round", title: "Call Jarvis", "aria-label": "Call Jarvis", onclick: () => startCall(chat) }, icon("phone"))),
    h("div", { class: "messages", id: "messages", role: "log", "aria-label": "Messages" },
      h("div", { class: "notice first", text: "Loading…" })),
    h("button", { class: "jump hide", id: "jump", text: "New message below", onclick: toBottom }),
    composer(chat));
  document.getElementById("messages").addEventListener("scroll", e => {
    chat.pinned = e.target.scrollHeight - e.target.scrollTop - e.target.clientHeight < 80;
    if (chat.pinned) document.getElementById("jump")?.classList.add("hide");
  });
  document.getElementById("composer-text")?.focus();
  if (id === UPDATES) clearUnread();
  drawChatsIfOpen();
  loadMessages(chat);
}

function moveSheet(chat) {
  const close = () => { sheet.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = e => { if (e.key === "Escape") close(); };
  const pick = async name => {
    close();
    try {
      await api(`/api/chats/${encodeURIComponent(chat.id)}/move`, { method: "POST", json: { project: name } });
      toast(`Moved to ${name}.`);
      location.hash = chatLink(name, chat.id);
      pollProjects();
    } catch (e) {
      if (e.status !== 401) toast(e.message);
    }
  };
  const rows = projects.filter(p => p.name !== chat.project).map(p => h("button", { class: "row", onclick: () => pick(p.name) },
    avatar(p.name), h("span", { class: "grow" }, h("div", { class: "name", text: p.name }), h("div", { class: "about", text: p.about }))));
  const sheet = h("div", { class: "sheet", role: "dialog", "aria-label": "Move this chat to a project", onclick: e => { if (e.target === sheet) close(); } },
    h("div", { class: "sheet-box" }, h("h2", { text: "Move this chat to" }), h("div", { class: "list" }, ...rows),
      h("button", { class: "btn", text: "Cancel", onclick: close })));
  document.addEventListener("keydown", onKey);
  document.body.append(sheet);
  sheet.querySelector(".row")?.focus();
}

/** Jarvis moved the chat while answering: follow it. */
function followChat(chat, project) {
  if (!project || project === chat.project) return;
  if (open === chat) location.hash = chatLink(project, chat.id);
  else chat.project = project;
}

function toBottom() {
  const box = document.getElementById("messages");
  if (box) box.scrollTop = box.scrollHeight;
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
  const arrived = chat.newest !== null && last?.ts !== chat.newest;
  chat.newest = last?.ts ?? null;
  chat.key = key;

  const out = [];
  let before = null;
  for (const m of chat.msgs) {
    const id = `${m.ts}|${m.role}`;      // keep drawn bubbles: redrawn images would jump the page
    const el = chat.bubbles.get(id) || bubble(m, before !== null && before !== m.role);
    chat.bubbles.set(id, el);
    el.classList.remove("first");
    out.push(el);
    before = m.role;
  }
  if (wait) {
    const arrived = chat.msgs.some(m => m.role === "me" && m.ts > wait.since);
    if (!arrived) out.push(bubble({ role: "me", text: wait.text, voice: wait.voice, reply: wait.reply }, before === "jarvis"));
    out.push(h("div", { class: "bubble gap typing", text: "Jarvis is working on it…" }));
  }
  if (!out.length) {
    out.push(h("div", { class: "notice", text: chat.found ? "No messages yet. Say something." : "This chat isn't there." }));
  }
  out[0].classList.add("first");
  box.replaceChildren(...out);
  if (chat.pinned) toBottom();
  else if (arrived) document.getElementById("jump")?.classList.remove("hide");
}

function sizeText(bytes) {
  return bytes < 1e6 ? `${Math.max(1, Math.round(bytes / 1e3))} KB` : `${(bytes / 1e6).toFixed(1)} MB`;
}

/** A file from Jarvis: videos play here, everything else downloads. */
async function openFile(file, button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Getting it…";
  try {
    const res = await request(`/api/files/${encodeURIComponent(file.name)}`);
    if (!res.ok) throw new ApiError("That file is gone.", res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const save = () => h("a", { class: "btn", href: url, download: file.title, text: "Save" });
    if (/\.(mp4|webm|mov|m4v)$/i.test(file.title)) {
      const video = new Blob([blob], { type: "video/mp4" });
      const playing = URL.createObjectURL(video);
      const close = () => { sheet.remove(); URL.revokeObjectURL(playing); URL.revokeObjectURL(url); };
      const sheet = h("div", { class: "sheet", role: "dialog", "aria-label": file.title, onclick: e => { if (e.target === sheet) close(); } },
        h("div", { class: "sheet-box wide" }, h("h2", { text: file.title }),
          h("video", { src: playing, controls: true, autoplay: true }), save(),
          h("button", { class: "btn", text: "Close", onclick: close })));
      document.body.append(sheet);
    } else {
      save().click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
  } catch (e) {
    if (e.status !== 401) toast(e.message);
  }
  button.disabled = false;
  button.textContent = label;
}

/** A short line for a quoted message (ticket 256550ce). */
function snippet(m) {
  const text = (m.text || "").replace(/\s+/g, " ").trim();
  if (text) return text.length > 120 ? `${text.slice(0, 120)}…` : text;
  if (m.images) return "📷 Image";
  if (m.files?.length) return `📎 ${m.files[0].title || m.files[0]}`;
  return "…";
}

function quoteOf(m) {
  return { role: m.role, text: m.text || "", ts: m.ts, ...(m.images?.length ? { images: m.images.length } : {}),
           ...(m.files?.length ? { files: m.files.map(f => f.title || f) } : {}) };
}

/** Tap a quote: scroll to the message it answers, and flash it. */
function jumpTo(ts) {
  const el = document.querySelector(`#messages .bubble[data-ts="${CSS.escape(String(ts))}"]`);
  if (!el) return toast("That message is too far back to show.");
  if (open) open.pinned = false;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

function bubble(m, gap) {
  const el = h("div", { class: `bubble ${m.role === "me" ? "me" : ""} ${gap ? "gap" : ""}` });
  if (m.ts) el.dataset.ts = String(m.ts);
  if (m.reply) {
    el.append(h("button", { class: "quote", title: "Show that message", onclick: () => jumpTo(m.reply.ts) },
      h("b", { text: m.reply.role === "me" ? "You" : "Jarvis" }), h("span", { text: snippet(m.reply) })));
  }
  for (const name of m.images || []) {
    const pic = h("img", { class: "pic", alt: "Image from Jarvis", onclick: () => lightbox(pic.src) });
    pic.addEventListener("load", () => { if (open?.pinned) toBottom(); });    // images arrive late and push the end down
    image(name).then(url => { pic.src = url; }, () => pic.replaceWith(h("div", { class: "muted small", text: "Image not available" })));
    el.append(pic);
  }
  if (m.text) el.append(h("div", { class: "text" }, rich(m.text)));
  for (const file of m.files || []) {
    const btn = h("button", { class: "file", text: `${file.title} · ${sizeText(file.size)}`, onclick: () => openFile(file, btn) });
    el.append(btn);
  }
  const from = m.chat_title ? `from ${m.chat_title}` : "";
  const meta = [m.voice ? "voice" : "", m.ts ? clock(m.ts) : "sending"].filter(Boolean).join(" · ");
  const replyBtn = m.ts ? h("button", { class: "reply-btn", title: "Reply to this message", "aria-label": "Reply to this message",
    text: "↩ Reply", onclick: () => open?.startReply?.(m) }) : null;
  el.append(h("div", { class: "meta" }, replyBtn, from && m.chat_project ? h("a", { href: chatLink(m.chat_project, m.chat), text: from }) : from,
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
  // photos (ticket 5d5a2c64): the clip button, pasting, or dropping them on the chat
  const picker = h("input", { type: "file", accept: "image/*", multiple: true, class: "hide" });
  const clipBtn = h("button", { class: "round", title: "Attach images", "aria-label": "Attach images", onclick: () => picker.click() }, icon("clip"));
  const tray = h("div", { class: "tray", "aria-label": "Images to send" });
  const form = h("div", { class: "composer" }, clipBtn, picker, text, micBtn, sendBtn);
  const pending = [];          // [{file, url}] waiting to be sent

  const attach = files => {
    for (const file of files) {
      if (!file.type.startsWith("image/")) continue;
      if (file.size > 20e6) { toast(`${file.name || "That image"} is too big (20 MB at most).`); continue; }
      if (pending.length >= 10) { toast("Ten images at a time."); break; }
      const item = { file, url: URL.createObjectURL(file) };
      pending.push(item);
      tray.append(h("div", { class: "thumb" }, h("img", { src: item.url, alt: "" }),
        h("button", { "aria-label": "Remove image", text: "×", onclick: e => {
          pending.splice(pending.indexOf(item), 1);
          URL.revokeObjectURL(item.url);
          e.currentTarget.parentElement.remove();
          refresh();
        } })));
    }
    refresh();
    text.focus();
  };
  picker.addEventListener("change", () => { attach([...picker.files]); picker.value = ""; });
  text.addEventListener("paste", e => {
    const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); attach(files); }
  });
  const onDrop = e => {
    if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
    e.preventDefault();
    if (e.type === "drop") attach([...e.dataTransfer.files]);
  };
  queueMicrotask(() => {
    for (const el of [document.getElementById("messages"), box]) {
      el?.addEventListener("dragover", onDrop);
      el?.addEventListener("drop", onDrop);
    }
  });

  const refresh = () => {
    text.style.height = "auto";
    text.style.height = `${Math.min(text.scrollHeight + 2, 180)}px`;
    const ready = !!text.value.trim() || pending.length > 0;
    sendBtn.classList.toggle("hide", !ready);
    micBtn.classList.toggle("hide", ready);
    if (text.value) drafts.set(chat.id, text.value);
    else drafts.delete(chat.id);
  };
  const submit = async () => {
    const value = text.value.trim();
    if (!value && !pending.length) return;
    if (waiting.has(chat.id)) return toast("Jarvis is still answering the last one.");
    if (sendBtn.disabled) return;
    let images = [];
    if (pending.length) {
      sendBtn.disabled = true;
      try {
        for (const item of pending) images.push((await api("/api/uploads", { method: "POST", raw: item.file })).name);
      } catch (e) {
        sendBtn.disabled = false;
        if (e.status !== 401) toast(e.message);
        return;                          // the images stay in the tray to try again
      }
      sendBtn.disabled = false;
      for (const item of pending) URL.revokeObjectURL(item.url);
      pending.length = 0;
      tray.replaceChildren();
    }
    text.value = "";
    refresh();
    send(chat, value, null, images, chat.takeReply());
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
  // replying to one message (ticket 256550ce): a bar above the input until it's sent or closed
  const replyBar = h("div", { class: "reply-bar hide" });
  const showReply = () => {
    const m = chat.replyTo;
    replyBar.classList.toggle("hide", !m);
    if (!m) return replyBar.replaceChildren();
    replyBar.replaceChildren(
      h("button", { class: "grow quote", title: "Show that message", onclick: () => jumpTo(m.ts) },
        h("b", { text: `Replying to ${m.role === "me" ? "yourself" : "Jarvis"}` }), h("span", { text: snippet(m) })),
      h("button", { class: "round", title: "Cancel reply", "aria-label": "Cancel reply", onclick: () => { chat.replyTo = null; showReply(); } }, icon("close")));
  };
  chat.startReply = m => { chat.replyTo = quoteOf(m); showReply(); text.focus(); };
  chat.takeReply = () => { const m = chat.replyTo; chat.replyTo = null; showReply(); return m; };
  text.addEventListener("keydown", e => { if (e.key === "Escape" && chat.replyTo) { chat.replyTo = null; showReply(); } });
  const box = h("div", { class: "composer-box" }, replyBar, tray, form);
  return box;
}

async function send(chat, text, audio, images = [], reply = null) {
  const known = open?.id === chat.id ? open.msgs : chat.msgs;
  const since = known[known.length - 1]?.ts || 0;
  const shown = text || (images.length > 1 ? `${images.length} images` : "Image");
  const wait = { since, text: audio ? "Voice message" : shown, voice: !!audio, lost: false, at: Date.now(), reply };
  waiting.set(chat.id, wait);
  drawMessages(chat);
  const path = `/api/chats/${encodeURIComponent(chat.id)}`;
  try {
    const res = audio
      ? await api(`${path}/voice${reply ? `?reply_to=${reply.ts}` : ""}`, { method: "POST", raw: audio })
      : await api(`${path}/messages`, { method: "POST", json: {
          text, ...(images.length ? { images } : {}), ...(reply ? { reply_to: reply.ts } : {}) } });
    waiting.delete(chat.id);
    if (audio && !res.heard) toast("Jarvis couldn't make out that voice message.");
    followChat(chat, res.chat_project || res.reply?.chat_project);
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
  pollProjects();
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
    if (!cancelled && audio.size) send(chat, "", audio, [], chat.takeReply?.());
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

// --- boards ----------------------------------------------------------------------------------

const CLOSED = /^(done|shipped|complete|completed|cancelled|canceled|dropped|won't do|archived)$/i;
const STATUSES = ["To do", "In progress", "Test", "Done", "Dropped"];

function pill(jarvis) {
  if (!jarvis) return null;
  return h("span", { class: `pill ${jarvis.toLowerCase().replace(/\s/g, "-")}`, text: jarvis });
}

function showBoard(project) {
  open = null;
  if (boardOn?.project !== project) boardOn = { project, show: "open", find: "", adding: false };
  const back = h("button", { class: "back", "aria-label": "Back to chats", text: "‹", onclick: () => { location.hash = projectLink(project); } });
  document.getElementById("main").replaceChildren(
    h("header", { class: "top" }, back,
      h("div", { class: "grow" }, h("h2", { text: `${project} board` }), h("div", { class: "sub", id: "board-sub" })),
      h("button", { class: "new", title: "New ticket", "aria-label": "New ticket", text: "+", onclick: () => { boardOn.adding = !boardOn.adding; drawBoardForm(); } })),
    h("div", { class: "board-bar" },
      h("input", { class: "field", type: "search", placeholder: "Find a ticket", "aria-label": "Find a ticket", value: boardOn.find,
        oninput: e => { boardOn.find = e.target.value; drawBoard(); } }),
      h("div", { class: "seg", id: "board-seg" })),
    h("div", { id: "board-form" }),
    h("div", { class: "board", id: "board" }));
  drawBoardForm();
  drawBoard();
  loadBoard(project);
}

async function loadBoard(project) {
  try {
    const data = await api(`/api/board?project=${encodeURIComponent(project)}`);
    boards.set(project, data);
    if (boardOn?.project === project) drawBoard();
  } catch (e) {
    const box = document.getElementById("board");
    if (e.status !== 401 && box && boardOn?.project === project && !boards.has(project)) box.replaceChildren(h("p", { class: "empty", text: e.message }));
  }
}

function drawBoard() {
  const box = document.getElementById("board");
  if (!box) return;
  const data = boards.get(boardOn.project);
  const seg = document.getElementById("board-seg");
  seg.replaceChildren(...[["open", "Open"], ["done", "Finished"], ["all", "All"]].map(([key, label]) =>
    h("button", { class: boardOn.show === key ? "on" : "", text: label, "aria-pressed": String(boardOn.show === key), onclick: () => { boardOn.show = key; drawBoard(); } })));
  if (!data) { if (!box.firstChild) box.append(h("p", { class: "empty", text: "Loading…" })); return; }
  const open_ = data.tickets.filter(t => !CLOSED.test(t.status)).length;
  document.getElementById("board-sub").textContent = `${open_} open of ${data.tickets.length}${data.local ? "" : " · still on Notion"}`;
  const words = boardOn.find.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = data.tickets.filter(t => {
    const closed = CLOSED.test(t.status);
    if (boardOn.show === "open" ? closed : boardOn.show === "done" && !closed) return false;
    const text = `${t.title} ${t.ref} ${t.sprint}`.toLowerCase();
    return words.every(w => text.includes(w));
  });
  const groups = data.columns.map(name => [name, shown.filter(t => (t.status || "No status") === name)]).filter(([, rows]) => rows.length);
  const top = box.scrollTop;
  box.replaceChildren(...(groups.length ? groups.map(([name, rows]) => h("section", { class: "column" },
    h("h3", {}, h("span", { text: name }), h("span", { class: "muted", text: String(rows.length) })),
    ...rows.map(t => h("a", { class: "card", href: ticketLink(boardOn.project, t.ref) },
      h("div", { class: "title", text: t.title || "Untitled" }),
      h("div", { class: "meta" }, pill(t.jarvis), h("span", { class: "muted", text: [t.sprint, t.due && `due ${t.due.slice(0, 10)}`, t.ref].filter(Boolean).join(" · ") })))))) :
    [h("p", { class: "empty", text: words.length ? "No ticket matches that." : "No tickets here." })]));
  box.scrollTop = top;
}

function drawBoardForm() {
  const box = document.getElementById("board-form");
  if (!box) return;
  if (!boardOn.adding) { box.replaceChildren(); return; }
  const title = h("input", { class: "field", placeholder: "Title", "aria-label": "Title", maxlength: "200" });
  const text = h("textarea", { class: "field", rows: "4", placeholder: "What should be done", "aria-label": "What should be done" });
  const add = async queue => {
    if (!title.value.trim()) { title.focus(); return; }
    try {
      const { ticket } = await api("/api/board", { method: "POST", json: { project: boardOn.project, title: title.value, text: text.value, queue } });
      boardOn.adding = false;
      drawBoardForm();
      location.hash = ticketLink(ticket.project, ticket.ref);
    } catch (e) {
      if (e.status !== 401) toast(e.message);
    }
  };
  box.replaceChildren(h("form", { class: "ticket-form", onsubmit: e => { e.preventDefault(); add(false); } }, title, text,
    h("div", { class: "actions" },
      h("button", { class: "btn", type: "submit", text: "Add" }),
      h("button", { class: "btn primary", type: "button", text: "Add and queue for Jarvis", onclick: () => add(true) }))));
  title.focus();
}

async function showTicket(project, ref) {
  open = null;
  const main = document.getElementById("main");
  const backToBoard = () => { location.hash = boardLink(project); };
  main.replaceChildren(
    h("header", { class: "top" },
      h("button", { class: "back", "aria-label": "Back to the board", text: "‹", onclick: backToBoard }),
      h("div", { class: "grow" }, h("h2", { id: "ticket-head", text: ref }), h("div", { class: "sub" }, h("a", { href: boardLink(project), text: `${project} board` })))),
    h("div", { class: "ticket", id: "ticket" }, h("p", { class: "empty", text: "Loading…" })));
  try {
    const data = await api(`/api/tickets/${encodeURIComponent(ref)}`);
    if (location.hash === ticketLink(project, ref)) drawTicket(data);
  } catch (e) {
    const box = document.getElementById("ticket");
    if (e.status !== 401 && box) box.replaceChildren(h("p", { class: "empty", text: e.message }));
  }
}

async function ticketAction(ref, json) {
  try {
    await api(`/api/tickets/${encodeURIComponent(ref)}`, { method: "POST", json });
    return true;
  } catch (e) {
    if (e.status !== 401) toast(e.message);
    return false;
  }
}

function drawTicket(data, editing = false) {
  const t = data.ticket;
  const box = document.getElementById("ticket");
  if (!box) return;
  const local = data.notion_url !== undefined;
  const again = () => { boards.delete(t.project); showTicket(t.project, t.ref); };
  const act = (label, action, cls = "btn", ask = "") => h("button", { class: cls, text: label, onclick: async () => {
    if (ask && !confirm(ask)) return;
    if (await ticketAction(t.ref, { action })) again();
  } });
  document.getElementById("ticket-head").textContent = t.title || t.ref;
  if (editing) {
    const title = h("input", { class: "field", value: t.title, "aria-label": "Title", maxlength: "200" });
    const text = h("textarea", { class: "field", rows: "16", "aria-label": "Text" });
    text.value = data.text;
    box.replaceChildren(h("form", { class: "ticket-form", onsubmit: async e => {
      e.preventDefault();
      if (await ticketAction(t.ref, { action: "edit", title: title.value, text: text.value })) again();
    } }, title, text, h("div", { class: "actions" },
      h("button", { class: "btn primary", type: "submit", text: "Save" }),
      h("button", { class: "btn", type: "button", text: "Cancel", onclick: () => drawTicket(data) }))));
    return;
  }
  let status = h("span", { class: "chip", text: t.status || "No status" });
  if (local) {
    const names = [...new Set([...(boards.get(t.project)?.columns || []), ...STATUSES, t.status].filter(Boolean))];
    status = h("select", { class: "chip", "aria-label": "Status", onchange: async e => {
      if (await ticketAction(t.ref, { action: "edit", status: e.target.value })) again();
    } }, ...names.map(name => h("option", { value: name, selected: name === t.status, text: name })));
  }
  const working = t.jarvis === "Working";
  const steps = data.log.filter(l => !l.line.startsWith("• "));
  box.replaceChildren(...[
    h("div", { class: "meta" }, status, pill(t.jarvis), h("span", { class: "muted small", text: [t.sprint, t.due && `due ${t.due.slice(0, 10)}`, t.ref].filter(Boolean).join(" · ") })),
    t.note ? h("p", { class: "note", text: t.note }) : null,
    h("div", { class: "actions" },
      t.jarvis === "Queued" || working ? null : act("Queue for Jarvis", "queue", "btn primary"),
      working ? null : t.jarvis && t.jarvis !== "Done" ? act("Take off queue", "unqueue") : null,
      CLOSED.test(t.status) || working ? null : act("Mark done", "done"),
      local ? h("button", { class: "btn", text: "Edit", onclick: () => drawTicket(data, true) }) : null,
      CLOSED.test(t.status) || working ? null : act("Drop", "drop", "btn", `Drop "${t.title}"?`)),
    h("pre", { class: "ticket-text", text: data.text || "This ticket has no text." }),
    data.log.length ? h("details", { class: "log", open: steps.length > 0 },
      h("summary", { text: `Jarvis's log (${data.log.length})` }),
      ...data.log.map(l => h("div", { class: l.line.startsWith("• ") ? "muted small" : "", text: l.line }))) : null,
    data.notion_url || !local ? h("a", { class: "small", href: data.notion_url || t.url, target: "_blank", rel: "noopener noreferrer", text: "Open the original page" }) : null,
  ].filter(Boolean));
}

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

async function pollProjects() {
  try {
    projects = await api("/api/projects");
    renderSideKeepingScroll();
  } catch { /* the next check tries again */ }
}

function tick() {
  ticks++;
  if (ticks % 10 === 0) { pollNews(); pollProjects(); }
  if (ticks % 5 === 0 && boardOn && document.getElementById("board")) loadBoard(boardOn.project);
  if (document.hidden) return;
  if (open && (waiting.has(open.id) || ticks % (open.id === UPDATES ? 2 : 4) === 0)) loadMessages(open);
  if (document.getElementById("workers") ? ticks % 2 === 0 : ticks % 5 === 0) pollWorkers();
}

window.addEventListener("hashchange", () => { if (session) route(); });
if (session) showApp();
else showLogin();

"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const WARMUP_BUCKETS = 18; // the watcher needs 3 minutes of chat history before it clips
const STATUS_LABEL = {
  recording: "Live · recording",
  waiting: "Waiting for stream",
  starting: "Starting…",
  stopping: "Finishing clips…",
  stopped: "Stopped",
  error: "Stopped with an error",
};

const state = {
  view: "streamers",
  streamers: [],
  defaults: {},
  qualities: [],
  layouts: {},
  templates: {},
  title_modes: {},
  ai: null,              // the active AI provider for titles, or null (titles from chat)
  aiSettings: null,      // the AI tab's data
  clips: [],
  clipsKey: "",
  cards: new Map(),      // channel -> card element
  seen: loadSeen(),
  player: null,          // clip being played
};

// ------------------------------------------------------------------ helpers

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

function loadSeen() {
  try {
    const raw = localStorage.getItem("cw-seen");
    return raw ? new Set(JSON.parse(raw)) : null; // null = first visit
  } catch { return null; }
}
function saveSeen() {
  try { localStorage.setItem("cw-seen", JSON.stringify([...state.seen])); } catch { /* private mode */ }
}

function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
function duration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : m ? `${m}m` : `${sec}s`;
}
function mmss(t) {
  const m = Math.floor(t / 60), s = Math.floor(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
function sizeMB(b) { return `${(b / 1e6).toFixed(1)} MB`; }
const icon = (name, cls = "") => `<svg class="i ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
// a stable colour per streamer, for their avatar
const hue = (s) => [...String(s)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
// the streamer's Twitch profile picture over a coloured letter; the letter stays if there's no picture
const avatarHTML = (ch, cls = "") => `<span class="avatar ${cls}" style="--h:${hue(ch)}" aria-hidden="true">${esc((ch || "?")[0])}` +
  `<img src="/avatars/${encodeURIComponent(ch)}" alt="" loading="lazy" onload="this.classList.add('ok')" onerror="this.remove()"></span>`;
function dayLabel(iso) {
  const d = new Date(iso), today = new Date();
  const days = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}

// ------------------------------------------------------------------ theme (auto / light / dark)

const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  $("#theme use").setAttribute("href", `#i-${t === "auto" ? "auto" : t === "light" ? "sun" : "moon"}`);
  $("#theme").title = t === "auto" ? "Theme: automatic (follows your system)" : `Theme: ${t}`;
  try { localStorage.setItem("cw-theme", t); } catch { /* private mode */ }
}
let theme = "auto";
try { theme = localStorage.getItem("cw-theme") || "auto"; } catch { /* ignore */ }
applyTheme(THEMES.includes(theme) ? theme : "auto");
$("#theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
  toast(theme === "auto" ? "Theme follows your system" : `${theme[0].toUpperCase()}${theme.slice(1)} theme`);
});

// ------------------------------------------------------------------ tabs

function showView(view) {
  state.view = view;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === view)));
  $("#view-streamers").hidden = view !== "streamers";
  $("#view-clips").hidden = view !== "clips";
  $("#view-ai").hidden = view !== "ai";
  $("#view-setup").hidden = view !== "setup";
  try { localStorage.setItem("cw-view", view); } catch { /* ignore */ }
  if (view === "clips") refreshClips();
  if (view === "ai") loadAI();
  if (view === "setup") loadSetup();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));
$(".brand").addEventListener("click", (e) => { e.preventDefault(); showView("streamers"); });
// "⋯" menus close when you click anywhere else
document.addEventListener("click", (e) => {
  document.querySelectorAll("details.menu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
});

// ------------------------------------------------------------------ settings form

function fillOptions(root) {
  for (const sel of root.querySelectorAll("select[data-options]")) {
    if (sel.options.length) continue;
    for (const [value, label] of Object.entries(state[sel.dataset.options] || {})) sel.add(new Option(label, value));
  }
}
const facecamText = (box) => box ? `set (${Math.round(box[2] * 100)}% × ${Math.round(box[3] * 100)}% of the frame)` : "not set";

// channelOf: returns the streamer name the facecam picker should grab a frame from
function buildSettings(container, values, channelOf) {
  container.replaceChildren($("#settings-template").content.cloneNode(true));
  const q = container.querySelector('[name="quality"]');
  const labels = { best: "Best available", "1080p60": "1080p60", "720p": "720p", "480p": "480p (smallest)" };
  for (const v of state.qualities) q.add(new Option(labels[v] || v, v));
  fillOptions(container);
  setSettings(container, values);
  const layout = container.querySelector('[name="layout"]');
  const syncFacecamRow = () => { $("[data-facecam-row]", container).hidden = layout.value !== "split"; };
  layout.addEventListener("change", syncFacecamRow);
  syncFacecamRow();
  $("[data-act-facecam]", container).addEventListener("click", () => {
    const hidden = container.querySelector('[name="facecam"]');
    openFacecam(channelOf(), hidden.value ? JSON.parse(hidden.value) : null, (box) => {
      hidden.value = box ? JSON.stringify(box) : "";
      $("[data-facecam-label]", container).textContent = facecamText(box);
    });
  });
  $("[data-claude-note]", container).textContent = aiNote();
}
const aiNote = () => state.ai
  ? `AI: ${state.ai.label} (${state.ai.model}) writes titles, scores and post captions.`
  : "No AI set up: titles come from chat. Add a free key in the AI tab for AI titles and ratings.";
function setSettings(container, s) {
  for (const el of container.querySelectorAll("[name]")) {
    if (!(el.name in s)) continue;
    if (el.name === "facecam") {
      el.value = s.facecam ? JSON.stringify(s.facecam) : "";
      $("[data-facecam-label]", container).textContent = facecamText(s.facecam);
    } else if (el.type === "checkbox") el.checked = !!s[el.name];
    else el.value = s[el.name];
  }
}
function readSettings(container) {
  const s = {};
  for (const el of container.querySelectorAll("[name]")) {
    if (el.name === "facecam") s.facecam = el.value ? JSON.parse(el.value) : null;
    else if (el.type === "checkbox") s[el.name] = el.checked;
    else if (el.type === "number") s[el.name] = Number(el.value);
    else s[el.name] = el.value;
  }
  return s;
}
// most telling first: the card shows the first few and folds the rest into "+N"
function settingsChips(s) {
  const layout = { blur: "blurred layout", crop: "center crop", split: s.facecam ? "streamer split" : "split (no facecam box yet!)" };
  const out = [`${s.ratio}× usual chat`, layout[s.layout] || s.layout,
    s.captions ? `${(state.templates[s.template] || s.template || "").split(" (")[0]} captions` : "no captions"];
  if (s.max_per_hour) out.push(`max ${s.max_per_hour} clips/hour`);
  out.push(`min ${s.min} msgs`, `${s.gap ?? 30} s between clips`, s.quality === "best" ? "best quality" : s.quality);
  if (s.captions && s.speakers) out.push("speaker colours");
  out.push(s.title === "off" ? "no hook title" : s.title === "chat" ? "titles from chat" : "auto titles");
  if (s.title !== "off" && s.title_in_video) out.push("title in video");
  if (s.keep_recordings) out.push("keeps recordings");
  return out;
}
function chipsHTML(list, shown = 3) {
  const rest = list.slice(shown);
  return list.slice(0, shown).map((c) => `<span class="chip">${esc(c)}</span>`).join("") +
    (rest.length ? `<span class="chip more" title="${esc(rest.join(" · "))}">+${rest.length} more</span>` : "");
}

// ------------------------------------------------------------------ facecam picker

const picker = { onSave: null, box: null, drag: null };
function openFacecam(channel, current, onSave) {
  if (!channel) { toast("Type the streamer's name first."); return; }
  picker.onSave = onSave;
  picker.box = current;
  const img = $("#facecam-img"), loading = $("#facecam-loading");
  $("#facecam-save").disabled = true;
  $("#facecam-box").hidden = true;
  loading.hidden = false;
  loading.textContent = "Getting a frame from the stream… (up to ~20 s if they aren't being recorded yet)";
  img.removeAttribute("src");
  $("#facecam").hidden = false;
  img.onload = () => { loading.hidden = true; drawPickerBox(); };
  img.onerror = async () => {
    const r = await fetch(img.src).catch(() => null);
    const msg = r ? (await r.json().catch(() => ({}))).error : null;
    loading.textContent = msg || "Couldn't get a frame. Is the streamer live?";
  };
  img.src = `/api/frame?channel=${encodeURIComponent(channel)}&t=${Date.now()}`;
}
function drawPickerBox() {
  const el = $("#facecam-box"), b = picker.box;
  el.hidden = !b;
  $("#facecam-save").disabled = !b;
  if (!b) return;
  Object.assign(el.style, { left: `${b[0] * 100}%`, top: `${b[1] * 100}%`, width: `${b[2] * 100}%`, height: `${b[3] * 100}%` });
}
const stage = $("#facecam-stage");
function stagePoint(e) {
  const r = $("#facecam-img").getBoundingClientRect();
  return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
}
stage.addEventListener("pointerdown", (e) => {
  if (!$("#facecam-img").complete || !$("#facecam-img").naturalWidth) return;
  stage.setPointerCapture(e.pointerId);
  picker.drag = stagePoint(e);
});
stage.addEventListener("pointermove", (e) => {
  if (!picker.drag) return;
  const [x0, y0] = picker.drag, [x1, y1] = stagePoint(e);
  picker.box = [Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0)].map((v) => Math.round(v * 10000) / 10000);
  drawPickerBox();
});
stage.addEventListener("pointerup", () => {
  picker.drag = null;
  if (picker.box && (picker.box[2] < 0.03 || picker.box[3] < 0.03)) { picker.box = null; drawPickerBox(); }
});
$("#facecam").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) $("#facecam").hidden = true; });
$("#facecam-save").addEventListener("click", () => { picker.onSave?.(picker.box); $("#facecam").hidden = true; });
$("#facecam-clear").addEventListener("click", () => { picker.onSave?.(null); $("#facecam").hidden = true; });

// ------------------------------------------------------------------ add a streamer

$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#add-btn"), err = $("#add-error");
  err.textContent = "";
  btn.disabled = true;
  try {
    const w = await api("POST", "/api/streamers", {
      channel: $("#add-channel").value,
      settings: readSettings($("#add-settings [data-settings]")),
      start: true,
    });
    $("#add-channel").value = "";
    toast(`Started ${w.channel}. Recording begins as soon as they're live.`);
    await refreshState();
  } catch (ex) {
    err.textContent = ex.message;
  } finally {
    btn.disabled = false;
  }
});

// ------------------------------------------------------------------ streamer cards

function makeCard(ch) {
  const el = document.createElement("article");
  el.className = "card streamer";
  el.dataset.channel = ch;
  el.innerHTML = `
    <div class="streamer-head">
      ${avatarHTML(ch)}
      <div class="who">
        <h3 title="${esc(ch)}">${esc(ch)}</h3>
        <a class="ext" href="https://www.twitch.tv/${esc(ch)}" target="_blank" rel="noopener">twitch.tv/${esc(ch)}${icon("ext")}</a>
      </div>
      <span class="status" data-status></span>
    </div>
    <div class="activity" data-activity>
      <div class="activity-head"><span>Chat messages per 10 s</span><span data-now></span></div>
      <svg viewBox="0 0 360 64" preserveAspectRatio="none" role="img" aria-label="Chat activity" data-chart></svg>
      <div class="activity-note" data-activity-label></div>
    </div>
    <div class="activity idle" data-idle hidden>
      <div class="idle-bars" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></div>
      <div class="idle-text" data-idle-text></div>
    </div>
    <div class="stats">
      <div class="stat"><b data-saved>0</b>clips saved</div>
      <div class="stat"><b data-clips>0</b>this session</div>
      <div class="stat"><b data-uptime>–</b>running for</div>
    </div>
    <div class="chips" data-chips></div>
    <div class="actions">
      <button data-act="start" class="primary">${icon("play", "fill")}Start</button>
      <button data-act="clipnow" class="primary" title="Clip the last 20 seconds and the next 10, right now">${icon("scissors")}Clip now</button>
      <button data-act="stop">${icon("stop", "fill")}<span>Stop</span></button>
      <span class="spacer"></span>
      <button class="icon-btn bordered" data-act="edit" title="Settings" aria-label="Settings for ${esc(ch)}">${icon("sliders")}</button>
      <button class="icon-btn bordered" data-act="clips" title="${esc(ch)}'s clips" aria-label="Show ${esc(ch)}'s clips">${icon("film")}</button>
      <details class="menu">
        <summary aria-label="More actions" title="More">${icon("more")}</summary>
        <div class="menu-list">
          <button data-act="log">${icon("log")}<span>Show log</span></button>
          <button data-act="remove" class="danger">${icon("trash")}<span>Remove streamer</span></button>
        </div>
      </details>
    </div>
    <div class="edit-settings" data-edit hidden>
      <div class="settings-grid" data-settings></div>
      <div class="actions">
        <button class="primary" data-act="save">Save settings</button>
        <button data-act="cancel">Cancel</button>
      </div>
    </div>
    <pre class="log" data-log hidden></pre>`;
  el.addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act) cardAction(ch, act, e.target.closest("[data-act]"));
  });
  return el;
}

async function cardAction(ch, act, btn) {
  const card = state.cards.get(ch);
  try {
    if (act === "start") { await api("POST", `/api/streamers/${ch}/start`); toast(`Started ${ch}`); }
    else if (act === "clipnow") {
      await api("POST", `/api/streamers/${ch}/clip`);
      btn.dataset.cooling = "1";
      btn.disabled = true;
      setTimeout(() => { delete btn.dataset.cooling; btn.disabled = false; }, 3000);
      toast(`Clipping ${ch}: the last 20 s + the next 10 s. It shows up in Clips in about a minute.`);
    }
    else if (act === "stop") { await api("POST", `/api/streamers/${ch}/stop`); toast(`Stopping ${ch}: finishing its clips first`); }
    else if (act === "log") {
      const log = $("[data-log]", card);
      log.hidden = !log.hidden;
      $("span", btn).textContent = log.hidden ? "Show log" : "Hide log";
      btn.closest("details")?.removeAttribute("open");
      if (!log.hidden) {
        log.textContent = (state.streamers.find((w) => w.channel === ch)?.log || []).join("\n") || "No output yet.";
        log.scrollTop = log.scrollHeight;
      }
      return;
    } else if (act === "edit") {
      const s = state.streamers.find((w) => w.channel === ch);
      const box = $("[data-edit]", card);
      if (box.hidden) buildSettings($("[data-settings]", box), s.settings, () => ch);
      box.hidden = !box.hidden;
      return;
    } else if (act === "cancel") { $("[data-edit]", card).hidden = true; return; }
    else if (act === "save") {
      await api("POST", `/api/streamers/${ch}/settings`, { settings: readSettings($("[data-edit] [data-settings]", card)) });
      $("[data-edit]", card).hidden = true;
      const wasRunning = state.streamers.find((w) => w.channel === ch)?.running;
      toast(wasRunning ? `Saved. Restarting ${ch} with the new settings (chat history is kept, no warm-up)`
                       : `Saved settings for ${ch}`);
    } else if (act === "clips") {
      $("#clip-channel").value = ch;
      showView("clips");
      return;
    } else if (act === "remove") {
      if (!btn.classList.contains("confirm")) {          // two-step confirm, no browser dialog
        btn.classList.add("confirm");
        $("span", btn).textContent = "Click again to remove";
        setTimeout(() => { btn.classList.remove("confirm"); $("span", btn).textContent = "Remove streamer"; }, 4000);
        return;
      }
      await api("DELETE", `/api/streamers/${ch}`);
      toast(`Removed ${ch}. Their clips are kept.`);
    }
    await refreshState();
  } catch (ex) {
    toast(ex.message);
  }
}

function chartSVG(buckets) {
  if (!buckets.length) {
    return `<text class="none" x="180" y="38" text-anchor="middle">Chat activity shows up here within 10 seconds</text>`;
  }
  const slots = 36, w = 360 / slots;
  const max = Math.max(5, ...buckets.map((b) => Math.max(b.msgs, b.median * 1.2)));
  const y = (v) => 62 - (v / max) * 58;
  const start = slots - buckets.length;
  let bars = "", line = "";
  buckets.forEach((b, i) => {
    const x = (start + i) * w;
    const h = Math.max(1, 62 - y(b.msgs));
    bars += `<rect class="bar${b.clip ? " spike" : ""}" x="${x + 1}" y="${62 - h}" width="${w - 2}" height="${h}" rx="1.5"><title>${esc(b.t)}: ${b.msgs} msgs${b.clip ? " · clipped" : ""}</title></rect>`;
    line += `${i ? "L" : "M"}${x + w / 2} ${y(b.median)}`;
  });
  return `<line class="baseline" x1="0" x2="360" y1="62.5" y2="62.5"/>` + bars + `<path class="median" d="${line}"/>`;
}

function updateCard(card, s) {
  const st = $("[data-status]", card);
  st.className = `status ${s.status}`;
  st.textContent = STATUS_LABEL[s.status] || s.status;
  card.classList.toggle("is-live", s.status === "recording");

  // stopped and nothing to show: say so, instead of an empty chart that promises data
  const idle = !s.running && !s.buckets.length;
  $("[data-activity]", card).hidden = idle;
  $("[data-idle]", card).hidden = !idle;
  if (idle) {
    $("[data-idle-text]", card).innerHTML = s.status === "error"
      ? `<b>Stopped with an error.</b> Open ⋯ → Show log to see why, then press Start.`
      : `<b>Not recording.</b> Press Start: it waits for ${esc(s.channel)} to go live and starts by itself.`;
  }
  $("[data-saved]", card).textContent = state.savedByChannel?.get(s.channel) ?? 0;

  const last = s.buckets.at(-1);
  $("[data-chart]", card).innerHTML = chartSVG(s.buckets);
  $("[data-now]", card).innerHTML = last ? `<strong>${last.msgs}</strong> now · usual ${last.median}` : "";
  const warming = s.running && s.warming;
  $("[data-activity-label]", card).textContent = warming
    ? `Learning the usual chat level: ${s.warming[0]} of ${s.warming[1]}. Clips start in about ${Math.max(1, Math.ceil((s.warming[1] - s.warming[0]) / 6))} min.`
    : s.buckets.length ? "Last 6 minutes · purple bars were clipped · dashed line = usual level" : "";

  $("[data-clips]", card).textContent = s.clips_made;
  $("[data-uptime]", card).textContent = s.started_at ? duration(Date.now() / 1000 - s.started_at) : "–";
  const chips = chipsHTML(settingsChips(s.settings));
  if (card.dataset.chips !== chips) { $("[data-chips]", card).innerHTML = chips; card.dataset.chips = chips; }

  const running = s.running;
  $('[data-act="start"]', card).hidden = running;
  const clipNow = $('[data-act="clipnow"]', card);
  clipNow.hidden = !running || s.status === "stopping";
  if (s.status !== "recording") clipNow.disabled = true;
  else if (!clipNow.dataset.cooling) clipNow.disabled = false;
  clipNow.title = s.status === "recording" ? "Clip the last 20 seconds and the next 10, right now"
                                           : "Available once the stream is live and recording";
  const stop = $('[data-act="stop"]', card);
  stop.hidden = !running;
  stop.disabled = s.status === "stopping";
  $("span", stop).textContent = s.status === "stopping" ? "Stopping…" : "Stop";
  $('[data-act="edit"]', card).hidden = s.status === "stopping";
  $('[data-act="remove"]', card).hidden = running;

  const log = $("[data-log]", card);
  if (!log.hidden) {
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    log.textContent = s.log.join("\n") || "No output yet.";
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
}

function renderStreamers() {
  const list = $("#streamers");
  const names = new Set(state.streamers.map((s) => s.channel));
  for (const [ch, card] of state.cards) {
    if (!names.has(ch)) { card.remove(); state.cards.delete(ch); }
  }
  for (const s of state.streamers) {
    let card = state.cards.get(s.channel);
    if (!card) {
      card = makeCard(s.channel);
      state.cards.set(s.channel, card);
      list.append(card);
    }
    updateCard(card, s);
  }
  $("#streamers-empty").hidden = state.streamers.length > 0;

  const rec = state.streamers.filter((s) => s.status === "recording").length;
  const wait = state.streamers.filter((s) => s.running && s.status !== "recording").length;
  const parts = [];
  if (rec) parts.push(`<span class="hpill live">${rec} recording</span>`);
  if (wait) parts.push(`<span class="hpill warn">${wait} waiting for stream</span>`);
  const aiTip = "AI models are loaded only while a clip is being made, then the memory is freed";
  parts.push(state.rendering
    ? `<span class="hpill busy" title="${aiTip}"><span class="mini-spin"></span>Making a clip</span>`
    : `<span class="hpill optional" title="${aiTip}">${icon("cpu")}AI idle · 0 MB</span>`);
  if (state.diskFree != null) {
    const cls = state.diskFree < 3 ? " bad" : state.diskFree < 15 ? " warn" : " optional";
    parts.push(`<span class="hpill${cls}" title="Free disk space">${icon("disk")}${state.diskFree} GB free</span>`);
  }
  const html = parts.join("");
  if ($("#summary").innerHTML !== html) $("#summary").innerHTML = html;
}

async function refreshState() {
  try {
    const data = await api("GET", "/api/state");
    const first = !state.qualities.length;
    state.streamers = data.streamers;
    state.qualities = data.qualities;
    state.defaults = data.defaults;
    state.layouts = data.layouts;
    state.templates = data.templates;
    state.title_modes = data.title_modes;
    state.ai = data.ai;
    document.querySelectorAll("[data-claude-note]").forEach((el) => { el.textContent = aiNote(); });
    const off = data.features_off || [];
    $("#features-off").hidden = !off.length;
    $("#features-off").innerHTML = off.length
      ? `<b>Some features are off</b> until their parts are downloaded. Nothing downloads without your OK.
         <a href="#" data-goto-setup>Open Setup</a><ul>${off.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : "";
    $("#setup-count").hidden = !off.length;
    $("#setup-count").textContent = off.length;
    state.rendering = data.rendering;
    state.diskFree = data.disk_free_gb;
    if (first) {
      buildSettings($("#add-settings [data-settings]"), data.defaults, () => $("#add-channel").value.trim());
      fillOptions($("#player"));
    }
    renderStreamers();
  } catch {
    $("#summary").innerHTML = `<span class="hpill bad">Dashboard server not reachable: is clipwatcher still running?</span>`;
  }
}

// ------------------------------------------------------------------ clips

function filteredClips() {
  const q = $("#clip-search").value.trim().toLowerCase();
  const ch = $("#clip-channel").value;
  const out = state.clips.filter((c) => (!ch || c.channel === ch) &&
    (!q || c.text.toLowerCase().includes(q) || c.channel.includes(q) || (c.title || "").toLowerCase().includes(q)));
  if ($("#clip-sort").value === "best") {             // the server sends newest first; keep that as the tie-break
    const score = (c) => c.virality ?? c.hype ?? -1;
    out.sort((a, b) => score(b) - score(a));
  }
  return out;
}

function renderClips() {
  const sel = $("#clip-channel"), cur = sel.value;
  const channels = [...new Set(state.clips.map((c) => c.channel).concat(state.streamers.map((s) => s.channel)))].sort();
  const want = ["", ...channels].join("|");
  if ([...sel.options].map((o) => o.value).join("|") !== want) {
    sel.replaceChildren(new Option("All streamers", ""), ...channels.map((c) => new Option(c, c)));
    sel.value = channels.includes(cur) ? cur : "";
  }

  const clips = filteredClips();
  const key = clips.map((c) => c.name + c.version + c.status + (c.title || "") + (state.seen?.has(c.name) ? 1 : 0)).join("|");
  $("#clip-count").textContent = state.clips.length;
  $("#clip-shown").textContent = clips.length === state.clips.length ? `${clips.length} clips` : `${clips.length} of ${state.clips.length} clips`;
  $("#clips-empty").hidden = state.clips.length > 0;
  if (key === state.clipsKey) {          // nothing changed: only refresh the "x min ago" labels
    document.querySelectorAll(".clip [data-ago]").forEach((el) => (el.textContent = ago(el.dataset.ago)));
    return;
  }
  state.clipsKey = key;
  // newest first: grouped under Today / Yesterday / date headings. "Best first" is one ranked list.
  const grouped = $("#clip-sort").value !== "best";
  const perDay = new Map();
  if (grouped) for (const c of clips) perDay.set(dayLabel(c.time), (perDay.get(dayLabel(c.time)) || 0) + 1);
  let html = "", lastDay = null;
  for (const c of clips) {
    if (grouped) {
      const day = dayLabel(c.time);
      if (day !== lastDay) {
        const n = perDay.get(day);
        html += `<div class="clip-group"><h3>${esc(day)}</h3><span>${n} clip${n === 1 ? "" : "s"}</span></div>`;
        lastDay = day;
      }
    }
    html += clipCard(c);
  }
  $("#clips").innerHTML = html;
}

function clipCard(c) {
  const isNew = state.seen && !state.seen.has(c.name) && c.ready;
  const title = c.title ? `<span class="clip-title">${esc(c.title)}</span>`
    : c.text ? `<span class="clip-title quote">“${esc(c.text.slice(0, 90))}”</span>` : "";
  const busy = c.status === "queued" || c.status === "rendering";
  const overlay = busy
    ? `<span class="state"><span><span class="spinner"></span>${c.status === "queued" ? "Waiting its turn…" : "Adding captions…"}</span></span>`
    : c.status === "failed" ? `<span class="state failed" title="${esc(c.error || "")}">Couldn't make this clip</span>` : "";
  const thumb = c.ready ? `<img loading="lazy" src="/thumbs/${encodeURIComponent(c.name.replace(/\.mp4$/, ""))}.jpg?v=${c.version}" alt="">` : "";
  const value = c.virality ?? c.hype;
  const hot = value != null && value >= 70;
  const score = value == null ? "" : `<span class="badge${hot ? " hot" : ""}" title="${c.virality != null ? "AI virality score" : "How hard chat reacted"}">${hot ? icon("flame", "fill") : ""}${c.virality != null ? "Viral" : "Hype"} ${value}</span>`;
  return `<button class="clip" data-name="${esc(c.name)}" title="${esc(new Date(c.time).toLocaleString())}"${c.ready ? "" : " disabled"}>
    ${thumb}
    <span class="top">${isNew ? `<span class="badge new">NEW</span>` : "<span></span>"}${score}</span>
    <span class="play">${icon("play", "fill")}</span>
    <span class="meta">${title}<span class="clip-sub">${avatarHTML(c.channel, "xs")}<b>${esc(c.channel)}</b> · <span data-ago="${esc(c.time)}">${ago(c.time)}</span>${c.msgs ? ` · ${c.msgs} msgs` : ""}</span></span>
    ${overlay}
  </button>`;
}

async function refreshClips() {
  try {
    const data = await api("GET", "/api/clips");
    state.clips = data.clips;
    if (state.seen === null) {           // first visit: everything that already exists counts as seen
      state.seen = new Set(state.clips.map((c) => c.name));
      saveSeen();
    }
    state.savedByChannel = new Map();
    for (const c of state.clips) if (c.ready) state.savedByChannel.set(c.channel, (state.savedByChannel.get(c.channel) || 0) + 1);
    renderClips();
    if (state.streamers.length) renderStreamers();
  } catch { /* server message shown by refreshState */ }
}

$("#clip-search").addEventListener("input", () => { state.clipsKey = ""; renderClips(); });
$("#clip-channel").addEventListener("change", () => { state.clipsKey = ""; renderClips(); });
$("#clip-sort").addEventListener("change", () => {
  try { localStorage.setItem("cw-sort", $("#clip-sort").value); } catch { /* private mode */ }
  state.clipsKey = "";
  renderClips();
});
try { if (localStorage.getItem("cw-sort") === "best") $("#clip-sort").value = "best"; } catch { /* ignore */ }
$("#clips").addEventListener("click", (e) => {
  const card = e.target.closest(".clip");
  if (card) openPlayer(card.dataset.name);
});

// ------------------------------------------------------------------ player

async function openPlayer(name) {
  const c = state.clips.find((x) => x.name === name);
  if (!c) return;
  state.player = c;
  state.seen?.add(name);
  saveSeen();
  state.clipsKey = "";
  renderClips();

  const src = `/clips/${encodeURIComponent(name)}`;
  const video = $("#player-video");
  video.src = `${src}?v=${c.version}`;
  state.showingSource = false;
  $("#player-avatar").outerHTML = avatarHTML(c.channel).replace('<span class="avatar ', '<span id="player-avatar" class="avatar ');
  $("#player-channel").textContent = c.channel;
  $("#player-when").textContent = `${new Date(c.time).toLocaleString()} · ${ago(c.time)}`;
  const h3 = $("#player-title");
  h3.textContent = c.title || `${c.channel} clip`;
  h3.classList.toggle("plain", !c.title);
  $("#player-hook").hidden = !c.title_by;
  $("#player-hook").textContent = c.title_by === "chat" ? "Hook title from what chat said"
    : c.title_by ? `Hook title written by ${c.title_by}` : "";
  $("#player-meta").textContent = `${c.msgs} chat messages in 10 s` + (c.median ? ` (usually ${c.median})` : "") +
    ` · ${sizeMB(c.size)}`;
  $("#player-path-row").hidden = !c.folder;
  $("#player-path").textContent = c.folder ? `${c.folder}/${c.name}` : "";
  $("#player-path").title = c.path || "";
  const scores = [];
  if (c.hype != null) scores.push(`<span class="score${c.hype >= 70 ? " hot" : ""}" title="How hard chat reacted compared to usual">Chat hype <b>${c.hype}</b>/100</span>`);
  if (c.virality != null) scores.push(`<span class="score${c.virality >= 70 ? " hot" : ""}" title="The AI's estimate (${esc(c.title_by || "")})">Virality <b>${c.virality}</b>/100</span>`);
  for (const [k, v] of Object.entries(c.scores || {})) scores.push(`<span class="score">${esc(k)} <b>${v}</b>/10</span>`);
  if (c.speakers > 1) scores.push(`<span class="score"><b>${c.speakers}</b> speakers</span>`);
  $("#player-scores").innerHTML = scores.join("");
  const post = c.post ? `${c.post.caption}\n${(c.post.hashtags || []).join(" ")}` : "";
  $("#player-post").hidden = !post;
  $("#player-post-text").textContent = post;
  fillEditor(c);
  $("#player-dl").href = `${src}?download=1`;
  $("#player-dl-srt").href = `/clips/${encodeURIComponent(name.replace(/\.mp4$/, ".srt"))}?download=1`;
  $("#player-dl-srt").hidden = !c.has_srt;
  const del = $("#player-delete");
  del.classList.remove("confirm");
  $("span", del).textContent = "Delete";
  $("#player").hidden = false;
  video.play().catch(() => {});

  const list = $("#player-transcript");
  list.innerHTML = `<li class="muted">Loading…</li>`;
  try {
    const { cues } = await api("GET", `/api/clips/${encodeURIComponent(name)}/transcript`);
    list.innerHTML = cues.length
      ? cues.map((q) => `<li><button data-t="${q.start}"><time>${mmss(q.start)}</time><span>${esc(q.text)}</span></button></li>`).join("")
      : `<li class="muted">Nobody spoke in this clip.</li>`;
  } catch {
    list.innerHTML = `<li class="muted">No transcript.</li>`;
  }
}

function closePlayer() {
  const video = $("#player-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("#player").hidden = true;
  state.player = null;
}

$("#player").addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) return closePlayer();
  const cue = e.target.closest("[data-t]");
  if (cue) {
    const v = $("#player-video");
    v.currentTime = Number(cue.dataset.t);
    v.play().catch(() => {});
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#facecam").hidden) $("#facecam").hidden = true;      // the picker sits on top of the player
  else if (!$("#player").hidden) closePlayer();
});
$("#player-path-copy").addEventListener("click", async () => {
  const c = state.player;
  if (!c) return;
  try { await navigator.clipboard.writeText(c.path || `${c.folder}/${c.name}`); toast("File path copied"); }
  catch { toast("Couldn't copy: select the path and copy it yourself"); }
});

$("#player-video").addEventListener("timeupdate", () => {
  const t = $("#player-video").currentTime;
  const cues = [...document.querySelectorAll("#player-transcript [data-t]")];
  let active = null;
  for (const c of cues) if (Number(c.dataset.t) <= t + 0.05) active = c;
  cues.forEach((c) => c.classList.toggle("active", c === active));
});

$("#player-delete").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  if (!btn.classList.contains("confirm")) {
    btn.classList.add("confirm");
    $("span", btn).textContent = "Click again to delete";
    return;
  }
  const name = state.player?.name;
  if (!name) return;
  closePlayer();
  try {
    await api("DELETE", `/api/clips/${encodeURIComponent(name)}`);
    toast("Clip deleted");
  } catch (ex) {
    toast(ex.message);
  }
  refreshClips();
});

// ------------------------------------------------------------------ clip editor

function fillEditor(c) {
  const ed = $("#player-editor");
  ed.hidden = !c.editable;
  if (!c.editable) return;
  const s = c.settings || {};
  $("#editor-start").value = c.start;
  $("#editor-end").value = c.end;
  $("#editor-layout").value = s.layout || "blur";
  $("#editor-template").value = s.template || "karaoke";
  $("#editor-captions").checked = s.captions !== false;
  $("#editor-title-video").checked = !!s.title_in_video;
  $("#editor-title").value = c.title || "";
  state.editFacecam = s.facecam || null;
  $("#editor-facecam-label").textContent = facecamText(state.editFacecam);
  $("#editor-facecam-row").hidden = $("#editor-layout").value !== "split";
  $("#editor-error").textContent = "";
  $("#editor-note").textContent = `Trim within the saved 1-minute source: the clip runs ${c.start}s to ${c.end}s of it.`;
  $("#editor-source").textContent = "Show the 1-minute source to trim";
  $("#editor-set-start").hidden = $("#editor-set-end").hidden = true;
}

$("#editor-layout").addEventListener("change", () => {
  $("#editor-facecam-row").hidden = $("#editor-layout").value !== "split";
});
$("#editor-facecam").addEventListener("click", () => {
  openFacecam(state.player?.channel, state.editFacecam, (box) => {
    state.editFacecam = box;
    $("#editor-facecam-label").textContent = facecamText(box);
  });
});
$("#editor-source").addEventListener("click", () => {
  const c = state.player, v = $("#player-video");
  if (!c) return;
  state.showingSource = !state.showingSource;
  v.src = state.showingSource ? `/sources/${encodeURIComponent(c.name)}` : `/clips/${encodeURIComponent(c.name)}?v=${c.version}`;
  if (state.showingSource) v.addEventListener("loadedmetadata", () => { v.currentTime = Number($("#editor-start").value) || 0; }, { once: true });
  $("#editor-source").textContent = state.showingSource ? "Back to the finished clip" : "Show the 1-minute source to trim";
  $("#editor-set-start").hidden = $("#editor-set-end").hidden = !state.showingSource;
  v.play().catch(() => {});
});
$("#editor-set-start").addEventListener("click", () => { $("#editor-start").value = $("#player-video").currentTime.toFixed(1); });
$("#editor-set-end").addEventListener("click", () => { $("#editor-end").value = $("#player-video").currentTime.toFixed(1); });

async function rerender(body) {
  const c = state.player;
  if (!c) return;
  $("#editor-error").textContent = "";
  try {
    await api("POST", `/api/clips/${encodeURIComponent(c.name)}/render`, body);
    closePlayer();
    toast("Re-rendering… the clip updates here in a few seconds.");
    refreshClips();
  } catch (ex) {
    $("#editor-error").textContent = ex.message;
  }
}
$("#editor-render").addEventListener("click", () => rerender({
  start: Number($("#editor-start").value), end: Number($("#editor-end").value),
  layout: $("#editor-layout").value, template: $("#editor-template").value,
  captions: $("#editor-captions").checked, title: $("#editor-title").value,
  title_in_video: $("#editor-title-video").checked,
  facecam: state.editFacecam,
}));
$("#editor-retitle").addEventListener("click", () => rerender({ retitle: true }));

$("#player-post-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#player-post-text").textContent); toast("Copied"); }
  catch { toast("Couldn't copy: select the text and copy it yourself"); }
});

// ------------------------------------------------------------------ AI tab

async function loadAI() {
  try {
    state.aiSettings = await api("GET", "/api/ai");
  } catch (ex) {
    $("#ai-error").textContent = ex.message;
    return;
  }
  const d = state.aiSettings;
  if (!state.aiPick) state.aiPick = d.choice;
  renderAI();
}

function renderAI() {
  const d = state.aiSettings;
  if (!d) return;
  $("#ai-active").innerHTML = d.active
    ? `Active now: <b>${esc(d.active.label)}</b> · <code>${esc(d.active.model)}</code>. New clips get AI titles and ratings.`
    : `No AI active: hook titles come from <b>chat</b> (free). Pick a provider below and paste a key to turn AI on.`;
  const cards = [
    { id: "auto", label: "Automatic", sub: "Use whichever provider below has a key", tag: "" },
    ...d.providers.map((p) => ({
      id: p.id, label: p.label,
      sub: p.has_key ? `Key saved (${p.key_hint})`
        : p.id === "custom" ? "DeepSeek, Mistral, Together, a local server…"
        : p.free ? "Free tier: just needs a free key" : "Paid API key",
      tag: p.has_key ? "key" : (p.free ? "free" : ""),
    })),
    { id: "chat", label: "No AI", sub: "Titles from chat only (free, works offline)", tag: "" },
  ];
  $("#ai-providers").innerHTML = cards.map((c) => `
    <button type="button" class="provider" role="radio" aria-checked="${state.aiPick === c.id}" data-id="${c.id}">
      <span class="name">${esc(c.label)}</span><span class="sub">${esc(c.sub)}</span>
      ${c.tag === "free" ? `<span class="tag free">FREE</span>` : c.tag === "key" ? `<span class="tag key">KEY ✓</span>` : ""}
    </button>`).join("");
  const p = d.providers.find((x) => x.id === state.aiPick);
  $("#ai-fields").hidden = !p;
  $("#ai-remove").hidden = !(p && p.has_key);
  if (p) {
    $("#ai-key").value = "";
    $("#ai-key").placeholder = p.has_key ? `saved (${p.key_hint}); paste a new one to replace it` : "paste your key";
    $("#ai-key-note").innerHTML = p.signup
      ? `Get one at <a href="${esc(p.signup)}" target="_blank" rel="noopener">${esc(p.signup.replace("https://", ""))}</a>${p.free ? " (free)" : ""}`
      : "";
    if (p.needs_package) $("#ai-key-note").innerHTML += `<br>Also run <code>pip install anthropic</code> once.`;
    const savedModel = d.choice === p.id && d.model ? d.model : "";
    $("#ai-model").value = savedModel || p.model;
    $("#ai-model-note").textContent = p.model ? `Suggested: ${p.model}` : "The model name your provider gives you";
    $("#ai-url-row").hidden = p.id !== "custom";
    $("#ai-url").value = d.base_url || "";
  }
}

$("#ai-providers").addEventListener("click", (e) => {
  const b = e.target.closest(".provider");
  if (!b) return;
  state.aiPick = b.dataset.id;
  $("#ai-result").hidden = true;
  $("#ai-error").textContent = "";
  renderAI();
});

async function saveAI() {
  $("#ai-error").textContent = "";
  const body = { provider: state.aiPick, model: $("#ai-model").value, base_url: $("#ai-url").value, key: $("#ai-key").value };
  state.aiSettings = await api("POST", "/api/ai", body);
  renderAI();
  refreshState();
}
$("#ai-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await saveAI(); toast("AI settings saved. New clips use them right away."); }
  catch (ex) { $("#ai-error").textContent = ex.message; }
});
$("#ai-test").addEventListener("click", async () => {
  const out = $("#ai-result"), btn = $("#ai-test");
  btn.disabled = true;
  btn.textContent = "Testing…";
  try {
    await saveAI();
    const r = await api("POST", "/api/ai/test");
    out.hidden = false;
    out.classList.toggle("bad", !r.ok);
    out.innerHTML = r.ok
      ? `<b>It works.</b> ${esc(r.provider)} · <code>${esc(r.model)}</code> answered in ${r.seconds}s.<br>
         Sample hook title: <b>${esc(r.title)}</b> · virality ${r.virality}/100<br>
         <span class="muted">${esc(r.post.caption)} ${esc((r.post.hashtags || []).join(" "))}</span>`
      : `<b>Not working yet:</b> ${esc(r.error)}`;
  } catch (ex) {
    $("#ai-error").textContent = ex.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Save & test";
  }
});
$("#ai-remove").addEventListener("click", async () => {
  try {
    state.aiSettings = await api("POST", "/api/ai", { provider: state.aiPick, remove_key: true, model: $("#ai-model").value });
    renderAI();
    refreshState();
    toast("Key removed");
  } catch (ex) { $("#ai-error").textContent = ex.message; }
});

// ------------------------------------------------------------------ setup tab (opt-in installs)

let setupTimer = null;
// a first click arms the button; only a second click within 6 s starts the download/install.
// Kept in state (not on the element) so the periodic refresh doesn't reset it.
const pendingInstall = { id: null, until: 0 };
const isPending = (id) => pendingInstall.id === id && Date.now() < pendingInstall.until;
function installButton(c, label) {
  return isPending(c.id)
    ? `<button class="confirm-install" data-install="${esc(c.id)}">Click again to confirm</button>`
    : `<button data-install="${esc(c.id)}">${esc(label)}</button>`;
}
async function loadSetup() {
  try {
    state.setup = await api("GET", "/api/setup");
  } catch (ex) {
    $("#setup-error").textContent = ex.message;
    return;
  }
  renderSetup();
  loadStorage();
  const running = state.setup.components.some((c) => c.job?.status === "running");
  clearTimeout(setupTimer);
  if (running || state.view === "setup") setupTimer = setTimeout(() => { if (state.view === "setup") loadSetup(); }, running ? 1500 : 6000);
}

function renderSetup() {
  const d = state.setup;
  const rows = d.components.filter((c) => c.group !== "whisper");
  $("#setup-python").textContent = d.python ? ` Running on ${d.python}.` : "";
  $("#setup-list").innerHTML = rows.map((c) => {
    const job = c.job;
    let pill, action = "";
    if (job?.status === "running") pill = `<span class="pill run">Working…</span>`;
    else if (c.installed) pill = `<span class="pill ok">${icon("check")}Installed</span>`;
    else if (c.required) pill = `<span class="pill req">Required · missing</span>`;
    else pill = `<span class="pill">Not installed</span>`;
    if (!c.installed && !c.manual && job?.status !== "running") {
      action = installButton(c, `${c.package ? "Install" : "Download"} (${c.size})`);
    }
    const manual = !c.installed && c.manual ? `<code class="cmd">Install it yourself: ${esc(c.manual)}</code>` : "";
    const reason = job?.status === "failed" ? job.log.filter((l) => l.includes("FAILED: ")).at(-1) : null;
    const warn = !c.installed && c.warning && !reason ? `<p class="part-warn">${esc(c.warning)}</p>` : "";
    const shown = job ? job.log.filter((l) => !l.includes("FAILED: ")).slice(-5) : [];
    const log = shown.length ? `<pre class="joblog">${esc(shown.join("\n"))}</pre>` : "";
    const failed = reason ? `<p class="part-warn"><b>Install failed:</b> ${esc(reason.split("FAILED: ")[1])}</p>` : "";
    return `<div class="part"><span class="name">${esc(c.label)}${c.required ? "<small>needed</small>" : "<small>optional</small>"}</span>
      <span class="side">${pill}${action}</span><span class="purpose">${esc(c.purpose)}</span>${manual}${warn}${failed}${log}</div>`;
  }).join("");
  $("#whisper-pick").innerHTML = d.components.filter((c) => c.group === "whisper").map((c) => {
    const size = c.id.replace("whisper-", "");
    const running = c.job?.status === "running";
    const status = running ? "Downloading…" : c.installed ? "Downloaded" : `Not downloaded (${c.size})`;
    return `<div class="provider" role="radio" aria-checked="${c.selected}" data-size="${size}">
      <span class="name">${esc(size)}${c.selected ? " · in use" : ""}</span>
      <span class="sub">${esc(c.purpose.replace("Caption model: ", ""))}</span>
      <span class="sub">${status}</span>
      ${!c.installed && !running ? installButton(c, `Download (${c.size})`)
        : c.installed && !c.selected ? `<button data-use="${esc(size)}">Use this one</button>` : ""}
      ${c.job?.status === "failed" ? `<span class="sub">Download failed: ${esc(c.job.log.at(-1) || "")}</span>` : ""}
    </div>`;
  }).join("");
}

$("#view-setup").addEventListener("click", async (e) => {
  const inst = e.target.closest("[data-install]");
  const use = e.target.closest("[data-use]");
  $("#setup-error").textContent = "";
  try {
    if (inst) {
      const id = inst.dataset.install;
      if (!isPending(id)) {                                       // second click = consent
        Object.assign(pendingInstall, { id, until: Date.now() + 6000 });
        renderSetup();
        setTimeout(() => { if (!isPending(id)) renderSetup(); }, 6100);
        return;
      }
      pendingInstall.id = null;
      state.setup = await api("POST", "/api/setup/install", { id });
      renderSetup();
      loadSetup();
    } else if (use) {
      state.setup = await api("POST", "/api/setup/whisper", { model: use.dataset.use });
      renderSetup();
      toast(`New clips use the ${use.dataset.use} caption model.`);
      refreshState();
    }
  } catch (ex) {
    $("#setup-error").textContent = ex.message;
  }
});
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-goto-setup]")) { e.preventDefault(); showView("setup"); }
});

// ------------------------------------------------------------------ storage (Setup tab)

const gb = (b) => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : sizeMB(b);
async function loadStorage(fromSave) {
  let d;
  try {
    d = fromSave || await api("GET", "/api/storage");
  } catch (ex) {
    $("#storage-error").textContent = ex.message;
    return;
  }
  $("#storage-folder").textContent = d.folder;
  $("#storage-free").innerHTML = `${icon("disk")}<span><b>${d.free_gb} GB</b> free on this disk · new clips pause below ${d.low_disk_gb} GB</span>`;
  const max = Math.max(1, d.sources_bytes || 0, ...Object.values(d.channels).map((r) => r.bytes));
  const bar = (bytes, cls = "") => `<span class="track"><i class="${cls}" style="width:${Math.max(1, bytes / max * 100).toFixed(1)}%"></i></span>`;
  const rows = Object.entries(d.channels).map(([ch, r]) =>
    `<li><strong>${esc(ch)}</strong>${bar(r.bytes)}<span class="size">${r.clips} clip${r.clips === 1 ? "" : "s"} · ${gb(r.bytes)}</span></li>`);
  if (d.sources_bytes) rows.push(`<li><strong>Clip sources</strong>${bar(d.sources_bytes, "src")}<span class="size">${gb(d.sources_bytes)}</span></li>`);
  $("#storage-usage").innerHTML = rows.join("") || `<li class="muted">No clips yet.</li>`;
  // don't overwrite what someone is typing during the periodic refresh
  for (const [id, key] of [["#keep-sources", "keep_sources_days"], ["#keep-clips", "keep_clips_days"]]) {
    const el = $(id);
    if (fromSave || !el.dataset.loaded) { el.value = d[key]; el.dataset.loaded = "1"; }
  }
}
$("#storage-save").addEventListener("click", async () => {
  $("#storage-error").textContent = "";
  try {
    const d = await api("POST", "/api/storage", {
      keep_sources_days: Number($("#keep-sources").value), keep_clips_days: Number($("#keep-clips").value) });
    loadStorage(d);
    toast("Storage settings saved. Old files are cleaned up in the background.");
  } catch (ex) {
    $("#storage-error").textContent = ex.message;
  }
});

// ------------------------------------------------------------------ start

try { const v = localStorage.getItem("cw-view"); if (["clips", "ai", "setup"].includes(v)) showView(v); } catch { /* ignore */ }
refreshState();
refreshClips();
setInterval(() => { if (!document.hidden) refreshState(); }, 2000);
setInterval(() => { if (!document.hidden) refreshClips(); }, 5000);

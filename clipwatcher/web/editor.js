"use strict";
/* clipwatcher editor: cut, add text and music, reframe, then export a NEW video. The original clip is
   never changed. The page edits a small "project" (kept parts, text layers, framing, audio); the server
   validates it, saves it, and renders it with ffmpeg (clipwatcher/editor.py).
   Uses helpers from app.js: $, esc, api, toast, icon, sizeMB, ago, openFacecam. */
(() => {
  const ASPECT = { "9:16": [1080, 1920], "4:5": [1080, 1350], "1:1": [1080, 1080] };
  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 2];
  const CAPTION = { karaoke: { size: 64, upper: true, words: null }, boxed: { size: 58, upper: true, words: null },
                    clean: { size: 54, upper: false, words: 6 }, pop: { size: 96, upper: true, words: 1 } };
  const SPEAKER_RGB = ["#ffffff", "#4dd8ff", "#ff6fd8", "#b388ff", "#ff9f43"];
  const PRESETS = {
    hook: { label: "Hook title", hint: "White on a violet band", style: "band", size: 58, color: "#ffffff", bg: "#7c5cff", y: 0.16, anim: "pop", dur: 3.5 },
    big: { label: "Big reaction", hint: "Huge outlined words", style: "outline", size: 112, color: "#ffe500", bg: "#000000", y: 0.45, anim: "pop", dur: 2, text: "NO WAY" },
    sub: { label: "Subtitle box", hint: "Text on a dark box", style: "band", size: 46, color: "#ffffff", bg: "#111111", y: 0.74, anim: "fade", dur: 3, text: "Wait for it…" },
    label: { label: "Label", hint: "Small text, soft shadow", style: "plain", size: 42, color: "#ffffff", bg: "#000000", y: 0.9, anim: "fade", dur: 4, text: "@streamer" },
  };
  const STYLE_NAMES = { band: "Band (box behind)", outline: "Outline", plain: "Shadow" };

  const video = $("#ed-video"), canvas = $("#ed-canvas"), ctx = canvas.getContext("2d");
  const off = document.createElement("canvas"), octx = off.getContext("2d");
  const ED = {
    open: false, name: null, data: null, project: null, sel: null, t: 0, i: 0, playing: false,
    undo: [], redo: [], lastKey: null, lastAt: 0, saveTimer: null, dirty: false,
    pps: 40, zoom: 0, strip: null, wave: null, tab: "media", cssW: 300, overlayKey: "", captions: null,
    audios: new Map(), job: null,
  };

  // ------------------------------------------------------------------ small helpers
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const r3 = (v) => Math.round(v * 1000) / 1000;
  const P = () => ED.project;
  const media = () => ED.data.media[P().media];
  const enc = () => encodeURIComponent(ED.name);
  const segDur = (s) => (s.out - s.in) / s.speed;
  const uid = () => Math.random().toString(36).slice(2, 10);
  const fmt = (t) => { t = Math.max(0, t); const m = Math.floor(t / 60); return `${m}:${(t - m * 60).toFixed(1).padStart(4, "0")}`; };
  function timing() {
    const starts = []; let t = 0;
    for (const s of P().segments) { starts.push(t); t += segDur(s); }
    return { starts, total: t };
  }
  function locate(t) {                 // output time -> part index + time in the media
    const segs = P().segments, { starts } = timing();
    let i = segs.length - 1;
    for (let k = 0; k < segs.length; k++) if (t < starts[k] + segDur(segs[k]) - 1e-6) { i = k; break; }
    const s = segs[i];
    return { i, mt: clamp(s.in + (t - starts[i]) * s.speed, s.in, s.out) };
  }
  function selected() {
    const s = ED.sel;
    if (!s) return null;
    if (s.type === "sound") return P().audio;          // the original video's own sound
    const list = s.type === "seg" ? P().segments : s.type === "text" ? P().texts : P().music;
    return list[s.i] || null;
  }

  // ------------------------------------------------------------------ history + saving
  const snapshot = () => JSON.stringify(P());
  function beginChange() {
    ED.undo.push(snapshot());
    if (ED.undo.length > 150) ED.undo.shift();
    ED.redo = [];
  }
  // key: consecutive edits of the same control within 1.5 s are one undo step (sliders, typing)
  function edit(fn, key = null, opts = {}) {
    const now = Date.now();
    if (!key || key !== ED.lastKey || now - ED.lastAt > 1500) beginChange();
    ED.lastKey = key; ED.lastAt = now;
    fn(P());
    afterChange(opts);
  }
  function afterChange({ props = true, panel = false } = {}) {
    fixTimes();
    ED.captions = null;
    renderTimeline();
    renderOverlays(true);
    if (props) renderProps();
    if (panel) renderPanel();
    seekTo(Math.min(ED.t, timing().total - 0.001));
    if (!ED.playing) draw();
    scheduleSave();
    updateHistory();
  }
  function fixTimes() {                // keep texts and music inside the edit's length
    const { total } = timing();
    P().texts = P().texts.filter((t) => t.start < total - 0.05);
    for (const t of P().texts) { t.end = r3(Math.min(t.end, total)); if (t.end - t.start < 0.1) t.start = r3(Math.max(0, t.end - 0.1)); }
    P().music = P().music.filter((m) => m.start < total - 0.05);
    for (const m of P().music) m.dur = r3(Math.max(0.1, Math.min(m.dur, total - m.start)));
    if (ED.sel && !selected()) ED.sel = null;
  }
  function undo(redo = false) {
    const from = redo ? ED.redo : ED.undo, to = redo ? ED.undo : ED.redo;
    if (!from.length) return;
    to.push(snapshot());
    ED.project = JSON.parse(from.pop());
    ED.lastKey = null;
    if (ED.sel && !selected()) ED.sel = null;
    ED.captions = null;
    renderAll();
    scheduleSave();
    toast(redo ? "Redone" : "Undone");
  }
  function updateHistory() {
    $("#ed-undo").disabled = !ED.undo.length;
    $("#ed-redo").disabled = !ED.redo.length;
  }
  function setStatus(text, bad = false) {
    const el = $("#ed-status");
    el.textContent = text;
    el.classList.toggle("bad", bad);
  }
  function scheduleSave() {
    ED.dirty = true;
    setStatus("Saving…");
    clearTimeout(ED.saveTimer);
    ED.saveTimer = setTimeout(save, 800);
  }
  async function save() {
    clearTimeout(ED.saveTimer);
    if (!ED.dirty || !ED.name) return;
    ED.dirty = false;
    try {
      await api("POST", `/api/clips/${enc()}/project`, P());
      setStatus("All changes saved · the original clip is never changed");
    } catch (ex) {
      setStatus(`Not saved: ${ex.message}`, true);
    }
  }

  // ------------------------------------------------------------------ open / close
  async function openEditor(name) {
    let data;
    try { data = await api("GET", `/api/clips/${encodeURIComponent(name)}/editor`); }
    catch (ex) { toast(ex.message); return; }
    Object.assign(ED, { name, data, sel: null, t: 0, i: 0, undo: [], redo: [], lastKey: null, dirty: false, zoom: 0, captions: null, tab: "media" });
    const kind = data.project ? data.project.media : data.defaults.source ? "source" : "clip";
    ED.project = structuredClone(data.project || data.defaults[kind]);
    for (const t of ED.project.texts) t.id = t.id || uid();
    $("#ed-zoom").value = 0;
    $("#ed-name").textContent = `${data.channel} · ${data.title || name}`;
    setStatus(data.project ? "Your saved edit was restored" : "The original clip is never changed: exports are new files");
    $("#editor").hidden = false;
    document.body.classList.add("ed-open");
    ED.open = true;
    selectTab("media");
    loadMedia();
    loadMusicAudio();
    renderAll();
    if (document.fonts) document.fonts.load('64px "Montserrat ExtraBold"').then(() => { draw(); renderOverlays(true); }).catch(() => {});
  }
  async function closeEditor() {
    pause();
    await save();
    video.removeAttribute("src");
    video.load();
    for (const a of ED.audios.values()) { a.pause(); a.removeAttribute("src"); }
    ED.audios.clear();
    $("#editor").hidden = true;
    document.body.classList.remove("ed-open");
    ED.open = false;
    window.dispatchEvent(new CustomEvent("editor-closed", { detail: { name: ED.name } }));
  }
  function loadMedia() {
    const m = media(), kind = P().media;
    video.src = m.url;
    video.load();
    ED.strip = null; ED.wave = null;
    const img = new Image();
    img.onload = () => { ED.strip = img; renderTimeline(); };
    img.src = `/api/clips/${enc()}/filmstrip/${kind}`;
    api("GET", `/api/clips/${enc()}/waveform/${kind}`).then((w) => { ED.wave = w; drawWave(); }).catch(() => {});
  }
  video.addEventListener("loadeddata", () => { layout(); seekTo(ED.t, true); });
  video.addEventListener("seeked", () => { if (!ED.playing) draw(); });

  // ------------------------------------------------------------------ playback engine
  function seekTo(t, force = false) {
    if (!ED.project) return;
    const { total } = timing();
    ED.t = clamp(t, 0, Math.max(0, total - 0.001));
    const { i, mt } = locate(ED.t);
    ED.i = i;
    video.playbackRate = P().segments[i].speed;
    if (force || Math.abs(video.currentTime - mt) > 0.03) { try { video.currentTime = mt; } catch { /* not loaded yet */ } }
    syncAudio();
    updateClock();
    renderOverlays();
    if (!ED.playing) draw();
  }
  function play() {
    const { total } = timing();
    if (ED.t >= total - 0.05) seekTo(0, true);
    ED.playing = true;
    video.playbackRate = P().segments[ED.i].speed;
    video.play().catch(() => { ED.playing = false; updatePlayButton(); });
    updatePlayButton();
    syncAudio();
    requestAnimationFrame(loop);
  }
  function pause() {
    ED.playing = false;
    video.pause();
    for (const a of ED.audios.values()) a.pause();
    updatePlayButton();
  }
  function loop() {
    if (!ED.playing || !ED.open) return;
    const segs = P().segments, { starts, total } = timing();
    let s = segs[ED.i];
    if (video.currentTime >= s.out - 0.02 || video.ended) {
      if (ED.i + 1 < segs.length) {                  // jump to the next kept part
        ED.i += 1; s = segs[ED.i];
        video.currentTime = s.in;
        video.playbackRate = s.speed;
        ED.t = starts[ED.i];
      } else {
        ED.t = total; pause(); updateClock(); draw(); return;
      }
    } else {
      ED.t = starts[ED.i] + Math.max(0, video.currentTime - s.in) / s.speed;
    }
    syncAudio();
    draw();
    renderOverlays();
    updateClock();
    requestAnimationFrame(loop);
  }
  function syncAudio() {
    const a = P().audio;
    video.volume = clamp(a.volume, 0, 1);
    video.muted = a.volume === 0;
    P().music.forEach((m, k) => {
      const el = ED.audios.get(`${k}:${m.id}`);
      if (!el) return;
      el.volume = clamp(m.volume, 0, 1);
      const inside = ED.t >= m.start && ED.t < m.start + m.dur;
      if (ED.playing && inside) {
        const want = m.offset + (ED.t - m.start);
        if (Math.abs(el.currentTime - want) > 0.25) el.currentTime = want;
        if (el.paused) el.play().catch(() => {});
      } else if (!el.paused) el.pause();
    });
  }
  function loadMusicAudio() {
    for (const a of ED.audios.values()) a.pause();
    ED.audios.clear();
    P().music.forEach((m, k) => {
      const el = new Audio(`/music/${encodeURIComponent(m.id)}`);
      el.preload = "auto";
      ED.audios.set(`${k}:${m.id}`, el);
    });
  }
  function updatePlayButton() {
    $("#ed-play use").setAttribute("href", ED.playing ? "#i-pause" : "#i-play");
    $("#ed-play").setAttribute("aria-label", ED.playing ? "Pause" : "Play");
  }
  function updateClock() {
    const { total } = timing();
    $("#ed-now").textContent = fmt(ED.t);
    $("#ed-total").textContent = fmt(total);
    const x = ED.t * ED.pps;
    $("#ed-playhead").style.left = `${x}px`;
    const sc = $("#ed-scroll");
    if (ED.playing && (x > sc.scrollLeft + sc.clientWidth - 40 || x < sc.scrollLeft)) sc.scrollLeft = x - 60;
  }

  // ------------------------------------------------------------------ preview (mirrors the ffmpeg recipe)
  function layout() {
    if (!ED.open) return;
    const vp = $("#ed-viewport"), [W, H] = ASPECT[P().aspect];
    const aw = vp.clientWidth - 28, ah = vp.clientHeight - 28;
    let h = ah, w = h * W / H;
    if (w > aw) { w = aw; h = w * H / W; }
    w = Math.max(60, w); h = Math.max(100, h);
    const frame = $("#ed-frame");
    frame.style.width = `${w}px`; frame.style.height = `${h}px`;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = off.width = Math.round(w * dpr);
    canvas.height = off.height = Math.round(h * dpr);
    ED.cssW = w;
    renderOverlays(true);
    draw();
  }
  new ResizeObserver(() => { if (ED.open) { layout(); renderTimeline(); } }).observe($("#ed-viewport"));

  function drawCover(c, src, sx, sy, sw, sh, dx, dy, dw, dh) {
    if (sw <= 0 || sh <= 0) return;
    const sa = sw / sh, da = dw / dh;
    if (sa > da) { const nw = sh * da; sx += (sw - nw) / 2; sw = nw; } else { const nh = sw / da; sy += (sh - nh) / 2; sh = nh; }
    c.drawImage(src, sx, sy, sw, sh, dx, dy, dw, dh);
  }
  function blurredBackground(c, w, h) {
    c.save();
    c.filter = `blur(${Math.max(2, w * 0.03)}px) brightness(0.72)`;
    drawCover(c, video, 0, 0, video.videoWidth, video.videoHeight, -w * 0.06, -h * 0.06, w * 1.12, h * 1.12);
    c.restore();
  }
  function compose(c, w, h) {
    const vw = video.videoWidth, vh = video.videoHeight, f = P().frame;
    c.fillStyle = "#000"; c.fillRect(0, 0, w, h);
    if (!vw) return;
    if (P().media === "source") {
      if (f.layout === "split" && f.facecam) {
        const top = Math.round(h * 0.4), [fx, fy, fw, fh] = f.facecam;
        drawCover(c, video, fx * vw, fy * vh, fw * vw, fh * vh, 0, 0, w, top);
        drawCover(c, video, 0, 0, vw, vh, 0, top, w, h - top);
      } else if (f.layout === "crop") {
        drawCover(c, video, 0, 0, vw, vh, 0, 0, w, h);
      } else {
        blurredBackground(c, w, h);
        const fh = w * vh / vw;
        c.drawImage(video, 0, 0, vw, vh, 0, (h - fh) / 2, w, fh);
      }
    } else if (f.fit === "blur") {
      blurredBackground(c, w, h);
      const s = Math.min(w / vw, h / vh);
      c.drawImage(video, 0, 0, vw, vh, (w - vw * s) / 2, (h - vh * s) / 2, vw * s, vh * s);
    } else {
      drawCover(c, video, 0, 0, vw, vh, 0, 0, w, h);
    }
  }
  function punchAt(t) {
    const { starts } = timing();
    let z = 1;
    P().segments.forEach((s, i) => {
      const p = s.punch;
      if (!p) return;
      const a = starts[i] + p.at, e = starts[i] + segDur(s);
      if (t >= a && t <= e) z += (p.zoom - 1) * Math.min(1, Math.max(0, (t - a) / p.dur));
    });
    return z;
  }
  function draw() {
    if (!ED.open) return;
    const w = canvas.width, h = canvas.height, f = P().frame;
    compose(octx, w, h);
    // zoom & pan, then a punch-in around the centre of that view: the same order as the export
    let rw = w / f.zoom, rh = h / f.zoom, rx = (w - rw) * f.x, ry = (h - rh) * f.y;
    const pz = punchAt(ED.t);
    if (pz > 1.0001) { const nw = rw / pz, nh = rh / pz; rx += (rw - nw) / 2; ry += (rh - nh) / 2; rw = nw; rh = nh; }
    ctx.drawImage(off, rx, ry, rw, rh, 0, 0, w, h);
    drawCaptions(w, h);
  }

  // captions: re-timed through the cuts exactly like the export (clipwatcher/editor.py retime_words)
  function captionGroups() {
    if (ED.captions) return ED.captions;
    const c = P().captions, info = CAPTION[c.template] || CAPTION.karaoke;
    const words = [];
    let t0 = 0;
    for (const s of P().segments) {
      for (const w of ED.data.words) {
        if (w.start >= s.in && w.start < s.out) {
          words.push({ start: t0 + (w.start - s.in) / s.speed, end: t0 + (Math.min(w.end, s.out) - s.in) / s.speed, text: w.text, speaker: w.speaker || 0 });
        }
      }
      t0 += segDur(s);
    }
    const talk = new Map();
    for (const w of words) talk.set(w.speaker, (talk.get(w.speaker) || 0) + w.end - w.start);
    const rank = new Map([...talk.entries()].sort((a, b) => b[1] - a[1]).map(([k], i) => [k, i]));
    for (const w of words) w.speaker = rank.get(w.speaker) || 0;
    const maxWords = info.words || c.words, maxChars = info.size >= 60 ? 20 : 28, groups = [];
    let cur = [];
    for (const w of words) {
      const len = cur.reduce((n, x) => n + x.text.length + 1, 0);
      if (cur.length && (cur.length >= maxWords || len + w.text.length > maxChars || w.start - cur.at(-1).end > 1 || w.speaker !== cur.at(-1).speaker)) {
        groups.push(cur); cur = [];
      }
      cur.push(w);
    }
    if (cur.length) groups.push(cur);
    ED.captions = groups.map((g, i) => {
      const next = groups[i + 1]?.[0].start;
      return { words: g, end: next != null && next - g.at(-1).end < 1.5 ? next : g.at(-1).end + 0.4 };
    });
    return ED.captions;
  }
  function drawCaptions(w, h) {
    const c = P().captions;
    if (!c.on || P().media !== "source" || !ED.data.words.length) return;
    const g = captionGroups().find((x) => x.words[0].start <= ED.t && ED.t < x.end);
    if (!g) return;
    const info = CAPTION[c.template] || CAPTION.karaoke, k = w / 1080;
    const size = info.size * k;
    let wi = 0;
    g.words.forEach((x, i) => { if (x.start <= ED.t) wi = i; });
    let tokens = g.words.map((x) => (info.upper ? x.text.toUpperCase() : x.text).trim());
    let hi = c.template === "karaoke" || c.template === "boxed" ? wi : -1;
    if (c.template === "pop") { tokens = [tokens[wi]]; hi = -1; }
    ctx.save();
    ctx.font = `${size}px "Montserrat ExtraBold", "Arial Black", sans-serif`;
    ctx.textBaseline = "middle";
    const space = ctx.measureText(" ").width;
    const widths = tokens.map((t) => ctx.measureText(t).width);
    const total = widths.reduce((a, b) => a + b, 0) + space * (tokens.length - 1);
    const y = h * (P().frame.layout === "split" && P().media === "source" ? 0.4375 : 0.7708);
    let x = (w - total) / 2;
    if (c.template === "boxed") {
      ctx.fillStyle = "rgba(0,0,0,.6)";
      ctx.fillRect(x - 14 * k, y - size * 0.62, total + 28 * k, size * 1.24);
    }
    const colour = SPEAKER_RGB[g.words[0].speaker % SPEAKER_RGB.length];
    ctx.lineJoin = "round";
    tokens.forEach((t, i) => {
      if (c.template !== "boxed") { ctx.lineWidth = 6 * k; ctx.strokeStyle = "#000"; ctx.strokeText(t, x, y); }
      ctx.fillStyle = i === hi ? "#ffe500" : colour;
      ctx.fillText(t, x, y);
      x += widths[i] + space;
    });
    ctx.restore();
  }

  // ------------------------------------------------------------------ text layers on the preview
  function renderOverlays(force = false) {
    if (!ED.open) return;
    const visible = P().texts.map((t, i) => [t, i]).filter(([t]) => ED.t >= t.start && ED.t < t.end);
    const key = visible.map(([t, i]) => `${i}:${t.id}:${ED.sel?.type === "text" && ED.sel.i === i}`).join("|");
    if (!force && key === ED.overlayKey) return;
    ED.overlayKey = key;
    const k = ED.cssW / 1080;
    $("#ed-overlays").innerHTML = visible.map(([t, i]) => {
      const sel = ED.sel?.type === "text" && ED.sel.i === i;
      return `<div class="ed-txt s-${t.style}${sel ? " sel" : ""}" data-oi="${i}" style="left:${t.x * 100}%;top:${t.y * 100}%;` +
        `font-size:${t.size * k}px;color:${t.color};--bg-c:${t.bg};--k:${k}">${esc(t.text)}</div>`;
    }).join("");
  }
  $("#ed-overlays").addEventListener("pointerdown", (e) => {
    const start = e.target.closest("[data-oi]");
    if (!start) return;
    e.preventDefault();
    const i = Number(start.dataset.oi), t = P().texts[i];
    select({ type: "text", i });            // re-draws the overlays, so the element is looked up again below
    const rect = $("#ed-frame").getBoundingClientRect(), x0 = e.clientX, y0 = e.clientY, tx = t.x, ty = t.y;
    let moved = false;
    const move = (ev) => {
      if (!moved) { if (Math.hypot(ev.clientX - x0, ev.clientY - y0) < 2) return; beginChange(); moved = true; }
      t.x = r3(clamp(tx + (ev.clientX - x0) / rect.width, 0.02, 0.98));
      t.y = r3(clamp(ty + (ev.clientY - y0) / rect.height, 0.02, 0.98));
      const el = $(`#ed-overlays [data-oi="${i}"]`);
      if (el) { el.style.left = `${t.x * 100}%`; el.style.top = `${t.y * 100}%`; }
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (moved) { ED.lastKey = null; afterChange(); }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });

  // ------------------------------------------------------------------ timeline
  function renderTimeline() {
    if (!ED.open) return;
    const { starts, total } = timing(), sc = $("#ed-scroll");
    const fit = Math.max(8, (sc.clientWidth - 40) / Math.max(total, 1));
    ED.pps = fit * Math.pow(12, ED.zoom / 100);
    const width = Math.max(sc.clientWidth, total * ED.pps + 40);
    $("#ed-inner").style.width = `${width}px`;
    // ruler: a label every 0.5-60 s depending on the zoom
    const step = [0.5, 1, 2, 5, 10, 15, 30, 60].find((s) => s * ED.pps >= 64) || 60;
    let ticks = "";
    for (let t = 0; t <= total + 1e-6; t += step) {
      ticks += `<span class="tick" style="left:${t * ED.pps}px">${fmt(t).replace(/\.0$/, "")}</span>`;
      if (step * ED.pps >= 120) ticks += `<span class="tick minor" style="left:${(t + step / 2) * ED.pps}px"></span>`;
    }
    $("#ed-ruler").innerHTML = ticks;
    const m = media();
    $("#ed-lane-video").innerHTML = P().segments.map((s, i) => {
      const sel = ED.sel?.type === "seg" && ED.sel.i === i;
      const strip = ED.strip ? `background-image:url(${ED.strip.src});background-size:${(m.duration / s.speed) * ED.pps}px 100%;` +
        `background-position:${(-s.in / s.speed) * ED.pps}px 0;` : "";
      const tags = [];
      if (s.speed !== 1) tags.push(`<span>${icon("gauge")}${s.speed}×</span>`);
      if (s.punch) tags.push(`<span>${icon("punch")}${s.punch.zoom}×</span>`);
      return `<div class="ed-seg${sel ? " sel" : ""}" data-seg="${i}" title="Part ${i + 1}: ${fmt(segDur(s))}" ` +
        `style="left:${starts[i] * ED.pps}px;width:${Math.max(4, segDur(s) * ED.pps - 2)}px;${strip}">` +
        `<span class="h l" data-h="l"></span><span class="ed-seg-tag">${tags.join("")}</span><span class="h r" data-h="r"></span></div>`;
    }).join("");
    const textRows = stackRows(P().texts.map((t) => [t.start, t.end]), "t");
    $("#ed-lane-text").innerHTML = P().texts.map((t, i) => {
      const sel = ED.sel?.type === "text" && ED.sel.i === i;
      return `<div class="ed-bar text${sel ? " sel" : ""}" data-text="${i}" style="left:${t.start * ED.pps}px;width:${Math.max(6, (t.end - t.start) * ED.pps)}px;${textRows[i]}">` +
        `<span class="h l" data-h="l"></span><span class="ed-bar-label">${esc(t.text.replace(/\n/g, " "))}</span><span class="h r" data-h="r"></span></div>`;
    }).join("");
    const names = new Map(ED.data.music.map((x) => [x.id, x.name]));
    const musicRows = stackRows(P().music.map((x) => [x.start, x.start + x.dur]), "m");
    $("#ed-lane-music").innerHTML = P().music.map((x, i) => {
      const sel = ED.sel?.type === "music" && ED.sel.i === i;
      return `<div class="ed-bar music${sel ? " sel" : ""}" data-music="${i}" style="left:${x.start * ED.pps}px;width:${Math.max(6, x.dur * ED.pps)}px;${musicRows[i]}">` +
        `<span class="h l" data-h="l"></span>${icon("music")}<span class="ed-bar-label">${esc(names.get(x.id) || "sound")}</span><span class="h r" data-h="r"></span></div>`;
    }).join("");
    const soundSel = ED.sel?.type === "sound", vol = P().audio.volume;
    $("#ed-wave").classList.toggle("sel", soundSel);
    const lab = $(".ed-lab.w");
    lab.classList.toggle("sel", soundSel);
    lab.innerHTML = `Sound<small>${vol === 0 ? "muted" : `${Math.round(vol * 100)}%`}</small>`;
    drawWave();
    updateClock();
  }
  // items that overlap in time go on separate rows of their track (and the track grows to fit)
  function stackRows(spans, track) {
    const ends = [], rowOf = [];
    spans.map(([a, b], i) => ({ a, b, i })).sort((x, y) => x.a - y.a).forEach(({ a, b, i }) => {
      let r = ends.findIndex((e) => e <= a + 1e-6);
      if (r < 0) { r = ends.length; ends.push(0); }
      ends[r] = b;
      rowOf[i] = r;
    });
    const rows = Math.max(1, ends.length), rowH = 30;
    const height = `${rows * rowH + 4}px`;
    $(`.ed-lane.${track}`).style.height = height;
    $(`.ed-lab.${track}`).style.height = height;
    return rowOf.map((r) => `top:${4 + r * rowH}px;height:${rowH - 4}px;bottom:auto`);
  }
  function drawWave() {
    const cv = $("#ed-wave");
    if (!ED.open) return;
    const width = parseFloat($("#ed-inner").style.width) || cv.clientWidth, height = 40, dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.style.width = `${width}px`;
    cv.width = Math.round(width * dpr); cv.height = height * dpr;
    const c = cv.getContext("2d");
    c.scale(dpr, dpr);
    const peaks = ED.wave?.peaks || [], dur = ED.wave?.duration || media().duration;
    if (!peaks.length) return;
    const loudest = ED.wave.loudest ??= Math.max(0.05, ...peaks);   // scale to this clip's loudest moment
    c.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--bar-spike").trim() || "#7c5cff";
    c.globalAlpha = 0.8 * clamp(P().audio.volume, 0.15, 1);
    const { starts } = timing();
    P().segments.forEach((s, i) => {
      const x0 = starts[i] * ED.pps, x1 = x0 + segDur(s) * ED.pps;
      for (let x = x0; x < x1; x += 2) {
        const mt = s.in + ((x - x0) / ED.pps) * s.speed;
        const p = peaks[Math.min(peaks.length - 1, Math.floor((mt / dur) * peaks.length))] || 0;
        const bh = Math.max(1, (p / loudest) * (height - 6));
        c.fillRect(x, (height - bh) / 2, 1.4, bh);
      }
    });
  }

  // dragging on the timeline: trim parts, move/resize text and music, or scrub the playhead
  let drag = null, dragFrame = 0;
  $("#ed-inner").addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const inner = $("#ed-inner"), x = e.clientX - inner.getBoundingClientRect().left;
    const segEl = e.target.closest("[data-seg]"), txtEl = e.target.closest("[data-text]"), musEl = e.target.closest("[data-music]");
    const handle = e.target.dataset.h || null;
    try { inner.setPointerCapture(e.pointerId); } catch { /* capture is a nicety: dragging still works without it */ }
    if (segEl) {
      const i = Number(segEl.dataset.seg), s = P().segments[i];
      select({ type: "seg", i });
      drag = handle ? { kind: "trim", i, handle, x0: x, a: s.in, b: s.out } : { kind: "scrub" };
      if (!handle) seekTo(x / ED.pps);
    } else if (txtEl || musEl) {
      const type = txtEl ? "text" : "music", i = Number((txtEl || musEl).dataset[type === "text" ? "text" : "music"]);
      const item = (type === "text" ? P().texts : P().music)[i];
      select({ type, i });
      drag = { kind: type, i, handle, x0: x, item: { ...item } };
    } else if (e.target.id === "ed-wave") {           // the Sound track: the original video's sound
      select({ type: "sound" });
      drag = { kind: "scrub" };
      seekTo(x / ED.pps);
    } else {
      if (e.target.closest(".ed-lane") && !e.target.closest("#ed-ruler")) select(null);
      drag = { kind: "scrub" };
      seekTo(x / ED.pps);
    }
    drag.moved = false;
  });
  $("#ed-inner").addEventListener("pointermove", (e) => {
    if (!drag) return;
    const x = e.clientX - $("#ed-inner").getBoundingClientRect().left;
    if (drag.kind === "scrub") { if (!ED.playing) seekTo(x / ED.pps); return; }
    const dt = (x - drag.x0) / ED.pps;
    if (!drag.moved) { if (Math.abs(x - drag.x0) < 3) return; beginChange(); drag.moved = true; }
    const { total } = timing();
    if (drag.kind === "trim") {
      const s = P().segments[drag.i];
      if (drag.handle === "l") s.in = r3(clamp(drag.a + dt * s.speed, 0, s.out - 0.1));
      else s.out = r3(clamp(drag.b + dt * s.speed, s.in + 0.1, media().duration));
    } else {
      const it = (drag.kind === "text" ? P().texts : P().music)[drag.i], o = drag.item;
      if (drag.kind === "text") {
        const len = o.end - o.start;
        if (drag.handle === "l") it.start = r3(clamp(o.start + dt, 0, o.end - 0.1));
        else if (drag.handle === "r") it.end = r3(clamp(o.end + dt, o.start + 0.1, total));
        else { it.start = r3(clamp(o.start + dt, 0, total - len)); it.end = r3(it.start + len); }
      } else {
        const lib = ED.data.music.find((m) => m.id === it.id), mdur = lib ? lib.duration : o.offset + o.dur;
        if (drag.handle === "l") {
          const d = clamp(dt, -Math.min(o.start, o.offset), o.dur - 0.1);
          it.start = r3(o.start + d); it.offset = r3(o.offset + d); it.dur = r3(o.dur - d);
        } else if (drag.handle === "r") it.dur = r3(clamp(o.dur + dt, 0.1, Math.min(mdur - o.offset, total - o.start)));
        else it.start = r3(clamp(o.start + dt, 0, total - 0.1));
      }
    }
    cancelAnimationFrame(dragFrame);
    dragFrame = requestAnimationFrame(() => {
      ED.captions = null;
      renderTimeline();
      renderOverlays(true);
      if (drag?.kind === "trim") {               // show the frame at the edge being dragged
        const { starts } = timing(), s = P().segments[drag.i];
        seekTo(drag.handle === "l" ? starts[drag.i] : starts[drag.i] + segDur(s) - 0.04);
      }
    });
  });
  const endDrag = () => {
    if (drag?.moved) { ED.lastKey = null; afterChange(); }
    drag = null;
  };
  $("#ed-inner").addEventListener("pointerup", endDrag);
  $("#ed-inner").addEventListener("pointercancel", endDrag);
  $("#ed-scroll").addEventListener("wheel", (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    ED.zoom = clamp(ED.zoom - Math.sign(e.deltaY) * 8, 0, 100);
    $("#ed-zoom").value = ED.zoom;
    renderTimeline();
  }, { passive: false });
  $("#ed-zoom").addEventListener("input", () => { ED.zoom = Number($("#ed-zoom").value); renderTimeline(); });

  // ------------------------------------------------------------------ editing actions
  function select(sel) {
    ED.sel = sel;
    renderTimeline();
    renderOverlays(true);
    renderProps();
    if (sel?.type === "text" && ED.tab === "text") renderPanel();
  }
  function split() {
    const { i, mt } = locate(ED.t), s = P().segments[i];
    if (mt - s.in < 0.1 || s.out - mt < 0.1) { toast("Move the playhead away from the edge of a part to split it."); return; }
    edit((p) => {
      const first = { ...s, out: r3(mt), punch: null }, second = { ...s, in: r3(mt), punch: null };
      if (s.punch) {
        const cut = segDur(first);
        if (s.punch.at < cut) first.punch = s.punch;
        else second.punch = { ...s.punch, at: r3(s.punch.at - cut) };
      }
      p.segments.splice(i, 1, first, second);
    });
    select({ type: "seg", i: i + 1 });
    toast("Split. Select a part and press Delete to cut it out.");
  }
  function removeSelected() {
    const s = ED.sel;
    if (!s) { toast("Select a part, text or sound on the timeline first."); return; }
    if (s.type === "sound") { toast("The video's own sound can't be removed: set its volume to 0 or press M to mute it."); return; }
    if (s.type === "seg" && P().segments.length < 2) { toast("This is the only part left: trim its edges instead."); return; }
    edit((p) => { (s.type === "seg" ? p.segments : s.type === "text" ? p.texts : p.music).splice(s.i, 1); });
    if (s.type === "music") loadMusicAudio();
    select(null);
  }
  function addText(preset = "hook") {
    const pr = PRESETS[preset], { total } = timing();
    const start = r3(Math.min(ED.t, Math.max(0, total - 0.5)));
    const text = { id: uid(), text: pr.text || (ED.data.title || "YOUR HOOK HERE"), start, end: r3(Math.min(total, start + pr.dur)),
      x: 0.5, y: pr.y, size: pr.size, color: pr.color, bg: pr.bg, style: pr.style, anim: pr.anim };
    edit((p) => p.texts.push(text));
    select({ type: "text", i: P().texts.length - 1 });
    setTimeout(() => $("#ed-props textarea")?.select(), 0);
  }
  function switchMedia(kind) {
    if (kind === P().media || !ED.data.media[kind]) return;
    pause();
    edit((p) => {
      const d = structuredClone(ED.data.defaults[kind]);
      p.media = kind; p.segments = d.segments; p.frame = d.frame; p.captions = d.captions;
    }, null, { panel: true });
    ED.t = 0;
    loadMedia();
    toast(kind === "clip" ? "Now editing the 30-second clip (Ctrl+Z to go back)" : "Now editing the 1-minute source (Ctrl+Z to go back)");
  }

  // ------------------------------------------------------------------ left panel
  function selectTab(tab) {
    ED.tab = tab;
    document.querySelectorAll("#ed-rail button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    renderPanel();
  }
  $("#ed-rail").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) selectTab(b.dataset.tab); });

  const slider = (label, path, min, max, step, value, unit = "", scale = 1) =>
    `<label class="ed-field">${label}<span class="row"><input type="range" data-k="${path}" data-scale="${scale}" min="${min}" max="${max}" step="${step}" value="${value / scale}">` +
    `<output data-unit="${unit}">${fmtUnit(value, unit)}</output></span></label>`;
  function fmtUnit(v, unit) {
    if (unit === "%") return `${Math.round(v * 100)}%`;
    if (unit === "s") return `${Number(v).toFixed(1)} s`;
    if (unit === "×") return `${Number(v).toFixed(2)}×`;
    return String(v);
  }
  const segCtl = (path, options, current, disabled = []) => `<div class="ed-seg-ctl">${Object.entries(options).map(([v, label]) =>
    `<button type="button" data-set="${path}" data-val="${v}" aria-pressed="${String(current) === v}"${disabled.includes(v) ? " disabled" : ""}>${label}</button>`).join("")}</div>`;

  function renderPanel() {
    if (!ED.open) return;
    const p = P(), d = ED.data, el = $("#ed-panel");
    let html = "";
    if (ED.tab === "media") {
      const src = d.media.source, clip = d.media.clip;
      html = `<h3>Media</h3><p>Choose what this edit is cut from.</p>` +
        segCtl("media", { source: "1-min source", clip: "30-s clip" }, p.media, [src ? "" : "source", clip ? "" : "clip"]) +
        `<div class="ed-note" style="margin-top:12px">${p.media === "source"
          ? `<b>1-minute source</b> (${fmt(src.duration)}): the clip plus 15 s each side. Any framing, and captions that follow your cuts.`
          : `<b>30-second clip</b> (${fmt(clip.duration)}): the finished clip. Its captions are already in the picture, so they can't be restyled.${src ? "" : " The 1-minute source of this clip has been cleaned up."}`}</div>` +
        `<div class="ed-note"><b>Your original clip is never changed.</b> Every export is a new file in <code>${esc(d.exports[0]?.folder || `clips/${d.channel}/edits`)}</code>.</div>` +
        `<h4>Start over</h4><div class="ed-btns"><button type="button" data-act="reset">${icon("reset")}Reset this edit</button></div>`;
    } else if (ED.tab === "text") {
      html = `<h3>Text</h3><p>Add a layer at the playhead, then drag it on the video.</p><div class="ed-presets">` +
        Object.entries(PRESETS).map(([k, pr]) => `<button type="button" class="ed-preset" data-add-text="${k}">` +
          `<span class="sample" style="color:${pr.color};${pr.style === "band" ? `background:${pr.bg}` : pr.style === "outline" ? `-webkit-text-stroke:3px ${pr.bg};paint-order:stroke fill` : "text-shadow:0 1px 3px #000"}">${esc((pr.text || "HOOK").slice(0, 10))}</span>` +
          `<b>${pr.label}</b><small>${pr.hint}</small></button>`).join("") + `</div>` +
        (p.texts.length ? `<h4>On this edit</h4><ul class="ed-list">${p.texts.map((t, i) =>
          `<li class="${ED.sel?.type === "text" && ED.sel.i === i ? "sel" : ""}"><span class="grow">${esc(t.text.replace(/\n/g, " "))}</span>` +
          `<span class="muted small">${fmt(t.start)}</span><button type="button" data-pick-text="${i}">Edit</button></li>`).join("")}</ul>` : "");
    } else if (ED.tab === "captions") {
      if (p.media !== "source") {
        html = `<h3>Captions</h3><div class="ed-note">This edit is cut from the <b>30-second clip</b>, whose captions are already in the picture.` +
          `${d.media.source ? " Switch to the 1-minute source in <b>Media</b> to restyle them or turn them off." : ""} You can still add text layers.</div>`;
      } else if (!d.words.length) {
        html = `<h3>Captions</h3><div class="ed-note">No transcript was saved for this clip, so there are no captions to show.</div>`;
      } else {
        html = `<h3>Captions</h3><p>Word-by-word captions that follow your cuts and speed changes.</p>` +
          `<label class="ed-field"><span class="row"><input type="checkbox" data-k="captions.on" ${p.captions.on ? "checked" : ""}> Show captions</span></label>` +
          `<label class="ed-field">Style<select data-k="captions.template">${Object.entries(d.templates).map(([k, v]) =>
            `<option value="${k}"${k === p.captions.template ? " selected" : ""}>${esc(v[0])}</option>`).join("")}</select></label>` +
          slider("Words per caption", "captions.words", 1, 8, 1, p.captions.words);
      }
    } else if (ED.tab === "frame") {
      const f = p.frame;
      html = `<h3>Frame</h3><label class="ed-field">Size<select data-k="aspect">${Object.keys(ASPECT).map((a) =>
        `<option value="${a}"${a === p.aspect ? " selected" : ""}>${{ "9:16": "9:16 · Reels / Shorts", "4:5": "4:5 · Instagram feed", "1:1": "1:1 · Square" }[a]}</option>`).join("")}</select></label>`;
      if (p.media === "source") {
        html += `<label class="ed-field">Layout</label>` + segCtl("frame.layout", { blur: "Blurred", crop: "Crop", split: "Facecam split" }, f.layout) +
          `<p style="margin-top:8px">${f.layout === "split" ? `Facecam box: ${f.facecam ? "set" : "<b>not set</b>"} · <button type="button" class="link" data-act="facecam">Draw it…</button>` : ""}</p>`;
      } else {
        html += `<label class="ed-field">Fit</label>` + segCtl("frame.fit", { crop: "Fill (crop)", blur: "Whole video + blur" }, f.fit);
      }
      html += `<h4>Zoom &amp; pan</h4>` + slider("Zoom", "frame.zoom", 1, 3, 0.01, f.zoom, "×") +
        slider("Left ↔ right", "frame.x", 0, 1, 0.01, f.x, "%") + slider("Up ↕ down", "frame.y", 0, 1, 0.01, f.y, "%") +
        `<div class="ed-btns"><button type="button" data-act="unzoom">${icon("reset")}Reset zoom</button></div>`;
    } else if (ED.tab === "audio") {
      const a = p.audio;
      html = `<h3>Audio</h3><h4>Clip sound</h4>` + slider("Volume", "audio.volume", 0, 200, 1, a.volume, "%", 0.01) +
        slider("Fade in", "audio.fade_in", 0, 5, 0.1, a.fade_in, "s") + slider("Fade out", "audio.fade_out", 0, 5, 0.1, a.fade_out, "s") +
        `<div class="ed-btns" style="margin:-4px 0 0"><button type="button" data-act="select-sound">Select the Sound track</button></div>` +
        `<h4>Music &amp; sounds</h4>` +
        `<label class="ed-dropzone">${icon("upload")}<span><b>Drop audio files here</b> or anywhere on the editor, or click to choose` +
        `<br><small>MP3, M4A, WAV or OGG, up to 30 MB</small></span>` +
        `<input type="file" id="ed-upload" accept=".mp3,.m4a,.wav,.ogg,audio/*" hidden></label>` +
        (d.music.length ? `<p class="small" style="margin:12px 0 6px">Drag a sound onto the timeline, or press Add (at the playhead).</p><ul class="ed-list">${d.music.map((m) =>
          `<li draggable="true" data-drag-music="${esc(m.id)}" title="Drag onto the timeline"><span class="grow">${icon("music")} ${esc(m.name)}</span><span class="muted small">${fmt(m.duration)}</span>` +
          `<button type="button" data-add-music="${esc(m.id)}">Add</button></li>`).join("")}</ul>` : `<p class="muted small" style="margin-top:8px">No sounds uploaded yet.</p>`);
    } else if (ED.tab === "exports") {
      html = `<h3>Exports</h3><p>Each export is a new video. The original clip stays as it is.</p>` +
        `<div class="ed-btns"><button type="button" class="primary" data-act="export">${icon("download")}Export now</button></div>` +
        (d.exports.length ? `<ul class="ed-list" style="margin-top:12px">${d.exports.map((x) =>
          `<li><span class="grow" title="${esc(x.path)}">${esc(new Date(x.time).toLocaleString())}<br><span class="muted small">${sizeMB(x.size)}</span></span>` +
          `<a class="button" href="${esc(x.url)}?download=1" title="Download">${icon("download")}</a>` +
          `<button type="button" class="danger" data-del-export="${esc(x.file)}" title="Delete this export">${icon("trash")}</button></li>`).join("")}</ul>`
          : `<p class="muted small" style="margin-top:10px">Nothing exported yet.</p>`);
    }
    el.innerHTML = html;
  }

  // ------------------------------------------------------------------ right panel: the selected item
  function renderProps() {
    if (!ED.open) return;
    const el = $("#ed-props"), s = ED.sel, item = selected();
    if (!s || !item) {
      const { total } = timing();
      el.innerHTML = `<h3>Edit</h3><p>${P().segments.length} part${P().segments.length === 1 ? "" : "s"} · ${fmt(total)} long. ` +
        `Click a part, text or music on the timeline to change it, or the <b>Sound</b> track for the video's own volume. ` +
        `Drop music files anywhere here to add them.</p><h4>Shortcuts</h4><div class="ed-keys">` +
        [["Space", "play / pause"], ["S", "split at the playhead"], ["Del", "delete the selection"], ["T", "add text"], ["M", "mute the video's sound"],
         ["← →", "one frame"], ["Shift ← →", "one second"], ["Ctrl Z", "undo"], ["Ctrl ⇧ Z", "redo"], ["Ctrl wheel", "zoom the timeline"], ["Esc", "close"]]
          .map(([k, v]) => `<kbd>${k}</kbd><span>${v}</span>`).join("") + `</div>`;
      return;
    }
    if (s.type === "sound") {
      const muted = item.volume === 0;
      el.innerHTML = `<h3>${icon("film")} Original sound</h3><p>The sound of the video itself (voices, game audio). ` +
        `It plays under any music you add.</p>` +
        slider("Volume", "sel.volume", 0, 200, 1, item.volume, "%", 0.01) +
        `<div class="ed-btns" style="margin:-4px 0 12px"><button type="button" data-act="mute">${muted ? "Unmute" : "Mute"} (M)</button>` +
        `<button type="button" data-act="vol100">100%</button></div>` +
        slider("Fade in", "sel.fade_in", 0, 5, 0.1, item.fade_in, "s") + slider("Fade out", "sel.fade_out", 0, 5, 0.1, item.fade_out, "s") +
        `<p class="small">Tip: drop music files anywhere on the editor to add them.</p>`;
      return;
    }
    if (s.type === "seg") {
      const len = segDur(item), m = media();
      el.innerHTML = `<h3>Part ${s.i + 1} of ${P().segments.length}</h3><p>${fmt(len)} in the edit · from ${fmt(item.in)} to ${fmt(item.out)} of the ${P().media === "source" ? "1-minute source" : "clip"}.</p>` +
        `<div class="ed-two"><label class="ed-field">Starts at (s)<input type="number" data-k="sel.in" step="0.1" min="0" max="${m.duration}" value="${item.in}"></label>` +
        `<label class="ed-field">Ends at (s)<input type="number" data-k="sel.out" step="0.1" min="0" max="${m.duration}" value="${item.out}"></label></div>` +
        `<h4>${icon("gauge")} Speed</h4><div class="ed-seg-ctl ed-speeds">${SPEEDS.map((v) =>
          `<button type="button" data-set="sel.speed" data-val="${v}" aria-pressed="${item.speed === v}">${v}×</button>`).join("")}</div>` +
        `<h4>${icon("punch")} Zoom punch-in</h4>` +
        (item.punch
          ? slider("Starts after", "sel.punch.at", 0, Math.max(0.05, r3(len - 0.05)), 0.05, item.punch.at, "s") +
            slider("Zoom", "sel.punch.zoom", 1.05, 2, 0.01, item.punch.zoom, "×") + slider("Zoom speed", "sel.punch.dur", 0.05, 3, 0.05, item.punch.dur, "s") +
            `<div class="ed-btns"><button type="button" data-act="punch-off">Remove punch-in</button></div>`
          : `<p>Zoom in on a moment and hold it, the classic reaction effect.</p><div class="ed-btns"><button type="button" data-act="punch-on">${icon("punch")}Add punch-in at the playhead</button></div>`) +
        `<h4>Part</h4><div class="ed-btns"><button type="button" data-act="split">${icon("scissors")}Split here</button>` +
        `<button type="button" class="danger" data-act="delete">${icon("trash")}Cut out</button></div>`;
    } else if (s.type === "text") {
      el.innerHTML = `<h3>Text</h3><label class="ed-field">Text<textarea data-k="sel.text" maxlength="120" rows="3">${esc(item.text)}</textarea></label>` +
        `<label class="ed-field">Style<select data-k="sel.style">${Object.entries(STYLE_NAMES).map(([k, v]) => `<option value="${k}"${k === item.style ? " selected" : ""}>${v}</option>`).join("")}</select></label>` +
        slider("Size", "sel.size", 16, 220, 1, item.size) +
        `<div class="ed-two"><label class="ed-field">Text colour<input type="color" data-k="sel.color" value="${item.color}"></label>` +
        `<label class="ed-field">${item.style === "outline" ? "Outline" : item.style === "band" ? "Box" : "(not used)"} colour<input type="color" data-k="sel.bg" value="${item.bg}"></label></div>` +
        `<label class="ed-field">Animation<select data-k="sel.anim">${[["fade", "Fade in & out"], ["pop", "Pop in"], ["none", "None"]].map(([k, v]) =>
          `<option value="${k}"${k === item.anim ? " selected" : ""}>${v}</option>`).join("")}</select></label>` +
        `<div class="ed-two"><label class="ed-field">Shows from (s)<input type="number" data-k="sel.start" step="0.1" min="0" value="${item.start}"></label>` +
        `<label class="ed-field">Until (s)<input type="number" data-k="sel.end" step="0.1" min="0" value="${item.end}"></label></div>` +
        `<div class="ed-btns"><button type="button" data-act="dup-text">Duplicate</button><button type="button" data-act="center">Centre</button>` +
        `<button type="button" class="danger" data-act="delete">${icon("trash")}Delete</button></div>`;
    } else {
      const lib = ED.data.music.find((m) => m.id === item.id);
      el.innerHTML = `<h3>${icon("music")} ${esc(lib?.name || "Sound")}</h3><p>Plays from ${fmt(item.start)} for ${fmt(item.dur)}.</p>` +
        slider("Volume", "sel.volume", 0, 200, 1, item.volume, "%", 0.01) +
        slider("Fade in", "sel.fade_in", 0, 5, 0.1, item.fade_in, "s") + slider("Fade out", "sel.fade_out", 0, 5, 0.1, item.fade_out, "s") +
        `<div class="ed-two"><label class="ed-field">Starts at (s)<input type="number" data-k="sel.start" step="0.1" min="0" value="${item.start}"></label>` +
        `<label class="ed-field">From the song at (s)<input type="number" data-k="sel.offset" step="0.1" min="0" value="${item.offset}"></label></div>` +
        `<div class="ed-btns"><button type="button" class="danger" data-act="delete">${icon("trash")}Remove</button></div>`;
    }
  }

  // ------------------------------------------------------------------ generic field binding
  function setPath(path, value) {
    let obj = P(), keys = path.split(".");
    if (keys[0] === "sel") { obj = selected(); keys = keys.slice(1); }
    for (const k of keys.slice(0, -1)) obj = obj[k];
    obj[keys.at(-1)] = value;
  }
  function valueOf(el) {
    if (el.type === "checkbox") return el.checked;
    if (el.type === "range" || el.type === "number") return r3(Number(el.value) * Number(el.dataset.scale || 1));
    if (el.dataset.k === "sel.speed") return Number(el.value);
    return el.value;
  }
  function onField(e) {
    const el = e.target.closest("[data-k]");
    if (!el || !ED.open) return;
    const path = el.dataset.k, v = valueOf(el);
    if ((el.type === "number" || el.type === "range") && !Number.isFinite(v)) return;
    const out = el.parentElement.querySelector("output");
    if (out) out.textContent = fmtUnit(v, out.dataset.unit);
    const structural = ["aspect", "sel.style", "sel.in", "sel.out", "captions.on"].includes(path);
    edit(() => {
      setPath(path, v);
      const seg = ED.sel?.type === "seg" ? selected() : null;
      if (seg && (path === "sel.in" || path === "sel.out")) {
        seg.in = r3(clamp(seg.in, 0, media().duration - 0.1)); seg.out = r3(clamp(seg.out, seg.in + 0.1, media().duration));
      }
      if (ED.sel?.type === "text" && path === "sel.end") { const t = selected(); t.end = r3(Math.max(t.end, t.start + 0.1)); }
    }, path, { props: structural && e.type === "change", panel: false });
    if (path === "aspect") { $("#ed-aspect").value = v; layout(); }
    if (path === "captions.on" || path === "captions.template") draw();
    if (path === "sel.style") renderOverlays(true);
  }
  for (const id of ["#ed-panel", "#ed-props"]) {
    $(id).addEventListener("input", (e) => { if (e.target.type !== "file" && e.target.tagName !== "SELECT" && e.target.type !== "checkbox") onField(e); });
    $(id).addEventListener("change", (e) => { if (e.target.tagName === "SELECT" || e.target.type === "checkbox") onField(e); });
    $(id).addEventListener("click", onClick);
  }
  $("#ed-aspect").addEventListener("change", () => {
    edit((p) => { p.aspect = $("#ed-aspect").value; }, null, { panel: ED.tab === "frame" });
    layout();
  });

  async function onClick(e) {
    const t = e.target.closest("button, [data-act]");
    if (!t || !ED.open) return;
    if (t.dataset.set) {
      const path = t.dataset.set, raw = t.dataset.val, v = path === "sel.speed" ? Number(raw) : raw;
      if (path === "media") return switchMedia(v);
      edit(() => setPath(path, v), null, { panel: path.startsWith("frame") });
      if (path === "frame.layout" && v === "split" && !P().frame.facecam) drawFacecam();
      return;
    }
    if (t.dataset.addText) return addText(t.dataset.addText);
    if (t.dataset.pickText) return select({ type: "text", i: Number(t.dataset.pickText) });
    if (t.dataset.addMusic) return addMusic(t.dataset.addMusic);
    if (t.dataset.delExport) return deleteExport(t, t.dataset.delExport);
    const act = t.dataset.act;
    if (act === "split") split();
    else if (act === "delete") removeSelected();
    else if (act === "export") startExport();
    else if (act === "facecam") drawFacecam();
    else if (act === "unzoom") edit((p) => Object.assign(p.frame, { zoom: 1, x: 0.5, y: 0.5 }), null, { panel: true });
    else if (act === "mute") toggleMute();
    else if (act === "select-sound") select({ type: "sound" });
    else if (act === "vol100") edit((p) => { p.audio.volume = 1; });
    else if (act === "punch-on") {
      const { starts } = timing(), i = ED.sel.i, s = P().segments[i];
      const at = r3(clamp(ED.t - starts[i], 0, Math.max(0, segDur(s) - 0.3)));
      edit((p) => { p.segments[i].punch = { at, zoom: 1.3, dur: 0.25 }; });
    } else if (act === "punch-off") edit((p) => { p.segments[ED.sel.i].punch = null; });
    else if (act === "dup-text") {
      const copy = { ...selected(), id: uid(), y: r3(clamp(selected().y + 0.06, 0.02, 0.98)) };
      edit((p) => p.texts.push(copy));
      select({ type: "text", i: P().texts.length - 1 });
    } else if (act === "center") edit(() => { selected().x = 0.5; });
    else if (act === "reset") {
      if (!t.classList.contains("confirm")) { t.classList.add("confirm", "danger"); t.lastChild.textContent = "Click again to reset"; return; }
      edit((p) => { Object.assign(p, structuredClone(ED.data.defaults[p.media])); }, null, { panel: true });
      loadMusicAudio();
      select(null);
      toast("Edit reset (Ctrl+Z to undo)");
    }
  }
  function toggleMute() {
    const muting = P().audio.volume > 0;
    edit((p) => {
      if (muting) { ED.unmuteTo = p.audio.volume; p.audio.volume = 0; } else p.audio.volume = ED.unmuteTo || 1;
    });
    toast(muting ? "Original sound muted (M to unmute)" : `Original sound back at ${Math.round(P().audio.volume * 100)}%`);
  }
  $(".ed-labels").addEventListener("click", (e) => { if (e.target.closest(".ed-lab.w")) select({ type: "sound" }); });
  function drawFacecam() {
    openFacecam(ED.data.channel, P().frame.facecam, (box) => {
      edit((p) => { p.frame.facecam = box; if (!box && p.frame.layout === "split") p.frame.layout = "blur"; }, null, { panel: true });
    });
  }

  // ------------------------------------------------------------------ music
  async function uploadMusic(file, at = null) {
    if (!file) return;
    if (file.size > 30e6) { toast("Choose an audio file under 30 MB."); return; }
    toast(`Uploading ${file.name}…`);
    try {
      const res = await fetch("/api/music", { method: "POST", body: file,
        headers: { "Content-Type": "application/octet-stream", "X-Filename": encodeURIComponent(file.name) } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Upload failed (${res.status})`);
      ED.data.music = [...ED.data.music.filter((m) => m.id !== data.id), data];
      if (ED.tab === "audio") renderPanel();
      addMusic(data.id, at);
    } catch (ex) { toast(ex.message); }
  }
  $("#ed-panel").addEventListener("change", (e) => { if (e.target.id === "ed-upload") uploadMusic(e.target.files[0]); });
  function addMusic(id, at = null) {
    const lib = ED.data.music.find((m) => m.id === id), { total } = timing();
    if (!lib) return;
    const start = r3(at != null ? clamp(at, 0, Math.max(0, total - 0.1)) : ED.t >= total - 0.2 ? 0 : ED.t);
    edit((p) => p.music.push({ id, start, offset: 0, dur: r3(Math.min(lib.duration, total - start)), volume: 0.4, fade_in: 0, fade_out: 1 }));
    loadMusicAudio();
    select({ type: "music", i: P().music.length - 1 });
    toast(`Added ${lib.name}. Drag it on the Music track; its edges trim it.`);
  }

  // ------------------------------------------------------------------ drag & drop audio
  // Files from the PC, or sounds dragged from the Audio panel: dropped on the timeline they start at
  // that moment, anywhere else at the playhead. Listens on the window, so a toast or dialog under the
  // pointer can't swallow the drop.
  const MUSIC_TYPE = "application/x-clipwatcher-music";
  const AUDIO_FILE = /\.(mp3|m4a|wav|ogg)$/i;
  let dropDepth = 0;
  const dragKinds = (e) => [...(e.dataTransfer?.types || [])];
  const isAudioDrag = (e) => dragKinds(e).includes("Files") || dragKinds(e).includes(MUSIC_TYPE);
  function dropTime(e) {
    const sc = $("#ed-scroll").getBoundingClientRect(), { total } = timing();
    if (e.clientY < sc.top || e.clientY > sc.bottom || e.clientX < sc.left) return null;
    return clamp((e.clientX - $("#ed-inner").getBoundingClientRect().left) / ED.pps, 0, Math.max(0, total - 0.1));
  }
  function showDrop(on, e) {
    $("#ed-drop").hidden = !on;
    const line = $("#ed-dropline"), t = on && e ? dropTime(e) : null;
    line.hidden = t == null;
    if (t != null) { line.style.left = `${t * ED.pps}px`; $("#ed-drop-at").textContent = `at ${fmt(t)}`; }
    else $("#ed-drop-at").textContent = "at the playhead";
    $("#ed-lane-music").classList.toggle("drop-target", t != null);
  }
  window.addEventListener("dragenter", (e) => {
    if (!ED.open || !isAudioDrag(e)) return;
    e.preventDefault();
    dropDepth += 1;
    showDrop(true, e);
  });
  window.addEventListener("dragover", (e) => {
    if (!ED.open || !isAudioDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    showDrop(true, e);
  });
  window.addEventListener("dragleave", () => {
    if (!ED.open) return;
    dropDepth = Math.max(0, dropDepth - 1);
    if (!dropDepth) showDrop(false);
  });
  window.addEventListener("drop", async (e) => {
    if (!ED.open || !isAudioDrag(e)) return;
    e.preventDefault();
    const at = dropTime(e);
    dropDepth = 0;
    showDrop(false);
    const id = e.dataTransfer.getData(MUSIC_TYPE);
    if (id) { addMusic(id, at); return; }
    const files = [...e.dataTransfer.files].filter((f) => AUDIO_FILE.test(f.name) || f.type.startsWith("audio/"));
    if (!files.length) { toast("Drop an MP3, M4A, WAV or OGG file to add it as music."); return; }
    for (const f of files) await uploadMusic(f, at);
  });
  $("#ed-panel").addEventListener("dragstart", (e) => {
    const li = e.target.closest("[data-drag-music]");
    if (!li) return;
    e.dataTransfer.setData(MUSIC_TYPE, li.dataset.dragMusic);
    e.dataTransfer.effectAllowed = "copy";
  });
  // any other file dropped while the editor is open must not make the browser leave the page
  for (const type of ["dragover", "drop"]) {
    window.addEventListener(type, (e) => { if (ED.open && dragKinds(e).includes("Files")) e.preventDefault(); });
  }

  // ------------------------------------------------------------------ export
  async function startExport() {
    pause();
    clearTimeout(ED.saveTimer);
    const dlg = $("#ed-dlg");
    $("#ed-dlg-title").textContent = "Exporting…";
    $("#ed-dlg-bar").style.width = "0%";
    $("#ed-dlg-text").textContent = "Preparing. The original clip is not changed.";
    $("#ed-dlg-actions").innerHTML = `<button type="button" data-dlg="hide">Keep editing</button>`;
    dlg.hidden = false;
    try {
      const { job } = await api("POST", `/api/clips/${enc()}/export`, P());
      ED.dirty = false;
      setStatus("All changes saved · the original clip is never changed");
      ED.job = job.id;
      pollExport(job.id);
    } catch (ex) {
      $("#ed-dlg-title").textContent = "Couldn't export";
      $("#ed-dlg-text").textContent = ex.message;
      $("#ed-dlg-actions").innerHTML = `<button type="button" data-dlg="hide">Close</button>`;
    }
  }
  async function pollExport(id) {
    if (ED.job !== id) return;
    let job;
    try { job = await api("GET", `/api/exports/${id}`); } catch { setTimeout(() => pollExport(id), 1500); return; }
    $("#ed-dlg-bar").style.width = `${Math.round(job.progress * 100)}%`;
    if (job.status === "queued") $("#ed-dlg-text").textContent = "Waiting for another clip or export to finish…";
    else if (job.status === "running") $("#ed-dlg-text").textContent = `${Math.round(job.progress * 100)}% · rendering in the background at low priority`;
    if (job.status === "done") {
      const f = job.file;
      ED.data.exports = [f, ...ED.data.exports.filter((x) => x.file !== f.file)];
      if (ED.tab === "exports") renderPanel();
      $("#ed-dlg-title").textContent = "Exported";
      $("#ed-dlg-text").textContent = `Saved as ${f.folder}/${f.file} (${sizeMB(f.size)}). The original clip is unchanged.`;
      $("#ed-dlg-actions").innerHTML = `<a class="button primary" href="${esc(f.url)}?download=1">${icon("download")}Download</a>` +
        `<a class="button" href="${esc(f.url)}" target="_blank" rel="noopener">Watch</a><button type="button" data-dlg="hide">Keep editing</button>`;
      return;
    }
    if (job.status === "failed") {
      $("#ed-dlg-title").textContent = "Export failed";
      $("#ed-dlg-text").textContent = job.error || "Unknown error";
      $("#ed-dlg-actions").innerHTML = `<button type="button" data-dlg="hide">Close</button>`;
      return;
    }
    setTimeout(() => pollExport(id), 700);
  }
  $("#ed-dlg").addEventListener("click", (e) => { if (e.target.closest("[data-dlg]")) $("#ed-dlg").hidden = true; });
  async function deleteExport(btn, file) {
    if (!btn.classList.contains("confirm")) { btn.classList.add("confirm"); btn.title = "Click again to delete"; toast("Click the bin again to delete this export"); return; }
    try {
      await api("DELETE", `/api/edits/${encodeURIComponent(ED.data.channel)}/${encodeURIComponent(file)}`);
      ED.data.exports = ED.data.exports.filter((x) => x.file !== file);
      renderPanel();
      toast("Export deleted");
    } catch (ex) { toast(ex.message); }
  }

  // ------------------------------------------------------------------ wiring
  function renderAll() {
    $("#ed-aspect").value = P().aspect;
    layout();
    renderTimeline();
    renderPanel();
    renderProps();
    renderOverlays(true);
    seekTo(ED.t, true);
    updateHistory();
    updatePlayButton();
  }
  $("#ed-play").addEventListener("click", () => (ED.playing ? pause() : play()));
  $("#ed-tostart").addEventListener("click", () => { pause(); seekTo(0, true); });
  $("#ed-back").addEventListener("click", closeEditor);
  $("#ed-undo").addEventListener("click", () => undo(false));
  $("#ed-redo").addEventListener("click", () => undo(true));
  $("#ed-split").addEventListener("click", split);
  $("#ed-delete").addEventListener("click", removeSelected);
  $("#ed-add-text").addEventListener("click", () => addText("hook"));
  $("#ed-export").addEventListener("click", startExport);
  window.addEventListener("beforeunload", () => { if (ED.open && ED.dirty) save(); });

  document.addEventListener("keydown", (e) => {
    if (!ED.open || !$("#facecam").hidden) return;
    const typing = e.target.closest("input, textarea, select");
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); undo(e.shiftKey); return; }
    if (mod && e.key.toLowerCase() === "y" && !typing) { e.preventDefault(); undo(true); return; }
    if (e.key === "Escape") {
      e.preventDefault();
      if (!$("#ed-dlg").hidden) $("#ed-dlg").hidden = true;
      else if (typing) e.target.blur();
      else if (ED.sel) select(null);
      else closeEditor();
      return;
    }
    if (typing || mod || e.altKey) return;
    const frame = 1 / 30;
    if (e.key === " ") { e.preventDefault(); ED.playing ? pause() : play(); }
    else if (e.key === "s" || e.key === "S") { e.preventDefault(); split(); }
    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeSelected(); }
    else if (e.key === "t" || e.key === "T") { e.preventDefault(); addText("hook"); }
    else if (e.key === "m" || e.key === "M") { e.preventDefault(); toggleMute(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); pause(); seekTo(ED.t - (e.shiftKey ? 1 : frame)); }
    else if (e.key === "ArrowRight") { e.preventDefault(); pause(); seekTo(ED.t + (e.shiftKey ? 1 : frame)); }
    else if (e.key === "Home") { e.preventDefault(); pause(); seekTo(0, true); }
    else if (e.key === "End") { e.preventDefault(); pause(); seekTo(timing().total); }
  });

  window.openEditor = openEditor;
  window.editorIsOpen = () => ED.open;
})();

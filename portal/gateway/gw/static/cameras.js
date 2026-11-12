(function () {
  "use strict";
  // Task 34b (round 2): the cameras page has two tabs, «Сейчас» and «Записи».
  //   Сейчас  all three lenses at once (a 2x2 grid of sub streams). Playback starts for ALL of them together, only when every lens has data (or after a time-out).
  //   Записи  the player (three lenses in step + shared controls) is at the TOP, under it the day and ONE list of events: clips of all lenses that start within a few
  //           seconds of each other are one event. Choosing an event loads the three clips, waits until all can play and starts them together.
  // Everything from the server goes in as text, never as markup; no inline style.
  var I = {};
  try { I = JSON.parse(document.getElementById("i18n").textContent); } catch (e) { I = { S: {}, E: {} }; }
  function T(key, vars) {
    var s = (I.S && I.S[key]) || key;
    if (vars) { Object.keys(vars).forEach(function (k) { s = s.split("{" + k + "}").join(vars[k]); }); }
    return s;
  }
  var root = document.getElementById("cam-root");
  var noteEl = document.getElementById("cam-note");
  if (!root) { return; }
  var cams = [], tiles = {}, hdOk = false, status = null, pageGen = 0, pane = "live";
  var live = { released: true, timer: null, note: null };
  var archive = { days: [], day: "", on: {}, clips: [], events: [], current: null, box: null, lenses: [], timers: [], gen: 0, dragging: false, span: 0, playing: false };
  var panes = {}, tabs = {};
  var STAGGER_MS = 1500, RETRY_MS = 5000, MAX_TRIES = 3, LIVE_WAIT_MS = 45000, SYNC_WAIT_MS = 15000, EVENT_GAP_MS = 10000;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = text; }
    return e;
  }
  function btn(text, onclick, cls) {
    var b = el("button", cls || "", text);
    b.type = "button";
    b.addEventListener("click", onclick);
    return b;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function note(text) { noteEl.textContent = text || ""; noteEl.hidden = !text; }
  function errText(j) {
    var c = j && j.error && j.error.code;
    return (c && I.E && I.E[c]) || T("cam_err_generic");
  }
  function getJson(path) {
    return fetch(path, { credentials: "same-origin", headers: { "Accept": "application/json" } }).then(function (r) {
      if (r.status === 401) { location.href = "/login"; return new Promise(function () {}); }
      return r.json().then(function (j) { return { ok: r.ok, json: j }; }, function () { return { ok: false, json: null }; });
    }, function () { return { ok: false, json: { error: { code: "network" } } }; });
  }
  function hhmmss(iso) { return iso ? iso.slice(11, 19) : ""; }
  function dm(iso) { return iso.slice(8, 10) + "." + iso.slice(5, 7); }
  function ms(iso) { return Date.parse(iso.replace(" ", "T") + "Z"); }
  function camName(id) { for (var i = 0; i < cams.length; i++) { if (cams[i].id === id) { return cams[i].name; } } return id; }
  function liveUrl(id, hd) { return "/v1/cameras/" + id + "/live?codec=h264" + (hd ? "&quality=hd" : "") + "&t=" + Date.now(); }
  function noop() {}
  function duration(s) {
    s = Math.max(1, Math.round(s));
    if (s < 60) { return T("cam_dur_s", { s: s }); }
    var m = Math.floor(s / 60), r = s % 60;
    return r ? T("cam_dur_ms", { m: m, s: r }) : T("cam_dur_m", { m: m });
  }

  // ── tabs ──────────────────────────────────────────────────────────────────
  function showPane(name) {
    pane = name;
    Object.keys(panes).forEach(function (k) { panes[k].hidden = k !== name; tabs[k].setAttribute("aria-pressed", k === name ? "true" : "false"); });
    if (name === "archive") { stopAll(); } else { stopSync(); if (!document.hidden) { startAll(); } }
  }

  // ── live grid: start all three together ───────────────────────────────────
  function setState(t, text, isErr) {
    t.status.textContent = text || "";
    t.status.hidden = !text;
    t.status.className = "cam-status" + (isErr ? " note err" : " meta");
  }
  function stopTile(t) {
    t.gen = (t.gen || 0) + 1;
    if (t.timer) { clearTimeout(t.timer); t.timer = null; }
    t.ready = false;
    t.video.pause();
    t.video.removeAttribute("src");
    t.video.load();
  }
  function liveNote(text) { if (live.note) { live.note.textContent = text || ""; live.note.hidden = !text; } }
  function readyCount() { return cams.filter(function (c) { return tiles[c.id].ready; }).length; }
  function releaseLive(partial) {
    if (live.released) { return; }
    live.released = true;
    clearTimeout(live.timer);
    cams.forEach(function (c) {
      var t = tiles[c.id];
      t.holding = false;
      if (t.video.getAttribute("src")) { t.video.play().catch(noop); }
    });
    liveNote(partial ? T("cam_live_partial") : "");
  }
  function checkLive() {
    if (live.released) { return; }
    var n = readyCount();
    if (n >= cams.length) { releaseLive(false); } else { liveNote(T("cam_live_wait", { n: n, m: cams.length })); }
  }
  // hold = wait for the other lenses (the first start of the page); a single restart (retry, "Чётче", "Продолжить") plays at once
  function startTile(t, hd, hold) {
    stopTile(t);
    t.hd = !!hd;
    t.holding = !!hold;
    t.again.hidden = true;
    setState(t, t.tries > 0 ? T("cam_retry_n", { n: t.tries + 1 }) : T("cam_wait_one"), false);
    t.video.src = liveUrl(t.cam.id, t.hd);
    t.video.load();
    t.hdBtn.textContent = t.hd ? T("cam_hd_off") : T("cam_hd");
  }
  function startAll() {
    var gen = ++pageGen;
    clearTimeout(live.timer);
    live.released = false;
    live.timer = setTimeout(function () { if (gen === pageGen) { releaseLive(true); } }, LIVE_WAIT_MS);
    liveNote(T("cam_live_wait", { n: 0, m: cams.length }));
    cams.forEach(function (c, i) {
      var t = tiles[c.id];
      t.tries = 0;
      t.timer = setTimeout(function () { if (gen === pageGen && !document.hidden && pane === "live") { startTile(t, false, true); } }, i * STAGGER_MS);
    });
  }
  function stopAll() {
    pageGen++;
    clearTimeout(live.timer);
    live.released = true;
    liveNote("");
    Object.keys(tiles).forEach(function (k) { stopTile(tiles[k]); });
  }
  function buildTile(c) {
    var t = { cam: c, big: false, hd: false, gen: 0, timer: null, tries: 0, ready: false, holding: false };
    var fig = el("figure", "cam-tile");
    fig.dataset.camera = c.id;
    var video = document.createElement("video");
    video.className = "cam-video";
    video.muted = true; video.preload = "auto"; video.setAttribute("playsinline", "");
    var st = el("p", "cam-status meta", T("cam_wait_one"));
    var cap = el("figcaption", "cam-cap");
    cap.appendChild(el("span", "cam-name", c.name));
    var expand = btn(T("cam_full"), function () { toggleBig(t); }, "cam-expand");
    var hdBtn = btn(T("cam_hd"), function () { t.tries = 0; t.hdFailed = false; startTile(t, !t.hd, false); }, "cam-hdbtn");
    hdBtn.hidden = true;
    var again = btn(T("cam_continue"), function () { t.tries = 0; startTile(t, t.hd, false); }, "cam-continue");
    again.hidden = true;
    cap.appendChild(expand); cap.appendChild(hdBtn);
    fig.appendChild(video); fig.appendChild(st); fig.appendChild(again); fig.appendChild(cap);
    t.fig = fig; t.video = video; t.status = st; t.hdBtn = hdBtn; t.again = again; t.expand = expand;
    video.addEventListener("canplay", function () {
      if (!video.getAttribute("src") || t.ready) { return; }
      t.ready = true;
      if (t.holding && !live.released) { setState(t, T("cam_ready_wait"), false); checkLive(); } else { video.play().catch(noop); }
    });
    video.addEventListener("playing", function () { setState(t, t.hdFailed ? T("cam_hd_failed") : "", false); t.tries = 0; });
    video.addEventListener("click", function () { toggleBig(t); });
    video.addEventListener("error", function () {
      if (!video.getAttribute("src")) { return; }                       // we removed the source ourselves
      t.ready = false;
      if (t.hd) {                                                       // the main stream gave up (the camera does not always deliver it): fall back to the light stream, say so
        t.hd = false; t.tries = 0; t.hdFailed = true;
        startTile(t, false, false);
        return;
      }
      if (t.tries < MAX_TRIES - 1 && !document.hidden) {                // quiet automatic retries: a cold camera source needs a moment
        t.tries += 1;
        setState(t, T("cam_retry_n", { n: t.tries + 1 }), false);
        var g = t.gen;
        t.timer = setTimeout(function () { if (g === t.gen && !document.hidden && pane === "live") { startTile(t, t.hd, !live.released); } }, RETRY_MS);
        return;
      }
      setState(t, T("cam_no_link"), true);
      t.again.hidden = false;
    });
    video.addEventListener("ended", function () { setState(t, T("cam_stopped"), false); t.again.hidden = false; });
    return t;
  }
  function toggleBig(t) {
    t.big = !t.big;
    t.fig.classList.toggle("cam-big", t.big);
    document.body.classList.toggle("cam-has-big", t.big);
    t.expand.textContent = t.big ? T("cam_close_big") : T("cam_full");
    t.hdBtn.hidden = !(t.big && hdOk);
    if (!t.big) { t.hdFailed = false; }
    if (!t.big && t.hd) { t.tries = 0; startTile(t, false, false); }     // leaving the big view returns to the light stream
  }
  function renderLive(box) {
    live.note = el("p", "note", "");
    live.note.hidden = true;
    live.note.setAttribute("role", "status");
    box.appendChild(live.note);
    box.appendChild(el("p", "meta", T("cam_grid_hint")));
    var grid = el("div", "cam-grid");
    tiles = {};
    cams.forEach(function (c) { var t = buildTile(c); tiles[c.id] = t; grid.appendChild(t.fig); });
    var more = el("div", "cam-tile cam-more");
    more.appendChild(el("p", "meta", T("cam_hd_note")));
    more.appendChild(btn(T("cam_archive"), function () { showPane("archive"); window.scrollTo(0, 0); }, "cam-to-archive"));
    grid.appendChild(more);
    box.appendChild(grid);
  }

  // ── archive: events = clips of all lenses that start within a few seconds of each other ──
  function computeEvents() {
    var rows = archive.clips.filter(function (c) { return archive.on[c.camera]; }).map(function (c) { return { c: c, b: ms(c.begin), e: ms(c.end) }; });
    rows.sort(function (a, b) { return a.b - b.b; });
    var out = [];
    rows.forEach(function (r) {
      var last = out[out.length - 1];
      if (last && r.b <= last.endMs + EVENT_GAP_MS) {
        last.endMs = Math.max(last.endMs, r.e);
        if (last.cams.indexOf(r.c.camera) < 0) { last.cams.push(r.c.camera); }
      } else {
        out.push({ begin: r.c.begin, beginMs: r.b, endMs: r.e, cams: [r.c.camera] });
      }
    });
    return out;
  }
  function eventLabel(ev) {
    var names = cams.filter(function (c) { return ev.cams.indexOf(c.id) >= 0; }).map(function (c) { return c.name; });
    return T("cam_event_row", { t: hhmmss(ev.begin), d: duration((ev.endMs - ev.beginMs) / 1000), c: names.join(", ") });
  }
  function fillList() {
    var list = archive.box && archive.box.querySelector("#cam-clips");
    if (!list) { return; }
    clear(list);
    archive.events = computeEvents();
    if (!archive.events.length) { list.appendChild(el("li", "meta", T("cam_archive_none"))); return; }
    archive.events.slice().reverse().forEach(function (ev) {                    // the newest event first
      var li = el("li", "cam-clip");
      var b = btn(eventLabel(ev), function () { showEvent(ev, true); }, "cam-clip-btn");
      if (archive.current && archive.current.begin === ev.begin) { b.setAttribute("aria-current", "true"); }
      li.appendChild(b);
      list.appendChild(li);
    });
  }
  function loadDay(then) {
    getJson("/v1/cameras/archive/clips?day=" + encodeURIComponent(archive.day)).then(function (r) {
      archive.clips = r.ok ? r.json.clips : [];
      fillList();
      if (then) { then(); }
    });
  }
  function renderArchive(box) {
    clear(box);
    archive.box = box;
    box.appendChild(el("h2", "", T("cam_archive_all")));
    if (status && status.fresh_ok_hhmm) { box.appendChild(el("p", "meta cam-updated", T("cam_updated", { t: status.fresh_ok_hhmm }))); }
    if (status && status.until_label) {
      var upto = T("cam_until", { t: status.until_label });
      if (status.fresh_backlog > 0) { upto += " \u00b7 " + T("cam_queue", { n: status.fresh_backlog }); }
      box.appendChild(el("p", "meta cam-until", upto));
    }
    if (status && status.loading) { box.appendChild(el("p", "note", T("cam_loading_now", { t: status.next_pull }))); }
    // the player first: it is always at the top of the tab
    var player = el("section", "cam-player");
    player.id = "cam-sync";
    archive.player = player;
    player.hidden = true;                                   // shown (at the top of the tab) when an event is chosen
    player.appendChild(el("p", "cam-event-title", ""));
    var sn = el("p", "note cam-sync-note", ""); sn.hidden = true; sn.setAttribute("role", "status");
    player.appendChild(sn);
    var grid = el("div", "cam-grid cam-syncgrid");
    grid.id = "cam-syncgrid";
    grid.appendChild(buildControls());                      // the controls are the fourth cell of the grid; the three lenses are inserted before it
    player.appendChild(grid);
    box.appendChild(player);
    if (!archive.days.length) { box.appendChild(el("p", "meta", T("cam_archive_none"))); return; }
    var sel = document.createElement("select");
    sel.setAttribute("aria-label", T("cam_pick_day"));
    archive.days.forEach(function (d) {
      var o = document.createElement("option");
      o.value = d.day; o.textContent = dm(d.day) + " · " + d.clips;
      if (d.day === archive.day) { o.selected = true; }
      sel.appendChild(o);
    });
    sel.addEventListener("change", function () { archive.day = sel.value; archive.clips = []; loadDay(); });
    box.appendChild(sel);
    var chips = el("div", "daychips cam-chips");
    cams.forEach(function (c) {
      var b = btn(c.name, function () { archive.on[c.id] = !archive.on[c.id]; b.setAttribute("aria-pressed", archive.on[c.id] ? "true" : "false"); fillList(); }, "daychip wide");
      b.setAttribute("aria-pressed", archive.on[c.id] ? "true" : "false");
      chips.appendChild(b);
    });
    box.appendChild(chips);
    box.appendChild(el("h3", "", T("cam_events")));
    box.appendChild(el("p", "meta", T("cam_sync_pick")));
    var list = el("ul", "cam-clips"); list.id = "cam-clips";
    box.appendChild(list);
    loadDay();
  }

  // ── the synchronised player ───────────────────────────────────────────────
  function buildControls() {
    var c = el("div", "cam-tile cam-more cam-controls");
    var pp = btn(T("cam_pause"), function () { togglePlay(); }, "cam-playpause");
    pp.disabled = true;
    var row = el("div", "cam-actions");
    var prev = btn(T("cam_prev_event"), function () { stepEvent("prev"); }, "cam-prev");
    var next = btn(T("cam_next_event"), function () { stepEvent("next"); }, "cam-next");
    row.appendChild(prev); row.appendChild(next);
    var rng = document.createElement("input");
    rng.type = "range"; rng.min = "0"; rng.max = "0"; rng.step = "1"; rng.value = "0"; rng.disabled = true;
    rng.className = "cam-slider";
    rng.setAttribute("aria-label", T("cam_slider"));
    rng.addEventListener("input", function () { archive.dragging = true; });
    rng.addEventListener("change", function () { archive.dragging = false; seekAll(parseFloat(rng.value)); });
    var msg = el("p", "meta cam-stepnote", ""); msg.hidden = true;
    c.appendChild(pp); c.appendChild(rng); c.appendChild(row); c.appendChild(msg);
    archive.ctl = { box: c, pp: pp, rng: rng, msg: msg };
    return c;
  }
  function clearTimers() { archive.timers.forEach(clearTimeout); archive.timers = []; }
  function stopSync() {
    archive.gen++;
    clearTimers();
    archive.lenses.forEach(function (l) { l.v.pause(); l.v.removeAttribute("src"); l.v.load(); });
    archive.lenses = [];
    archive.playing = false;
    document.body.classList.remove("cam-has-big");
    var g = document.getElementById("cam-syncgrid");
    if (g) { Array.prototype.slice.call(g.querySelectorAll(".cam-lens")).forEach(function (n) { g.removeChild(n); }); }
  }
  function syncNote(text) {
    var n = archive.player && archive.player.querySelector(".cam-sync-note");
    if (n) { n.textContent = text || ""; n.hidden = !text; }
  }
  // p = seconds from the event's start; each lens shows p - delay + offset of its own clip (a lens that starts later waits)
  function seekAll(p) {
    clearTimers();
    archive.lenses.forEach(function (l) {
      var target = p - l.delay + l.off;
      if (target < 0) {
        try { l.v.currentTime = 0; } catch (e) { /* the player decides */ }
        l.v.pause();
        archive.timers.push(setTimeout(function () { l.v.play().catch(noop); }, -target * 1000));
      } else {
        try { l.v.currentTime = Math.min(target, Math.max(0, (l.dur || target) - 0.2)); } catch (e) { /* the player decides */ }
        l.v.play().catch(noop);
      }
    });
    setPlaying(true);
  }
  function setPlaying(on) {
    archive.playing = on;
    archive.ctl.pp.textContent = on ? T("cam_pause") : T("cam_play");
  }
  function position() {
    for (var i = 0; i < archive.lenses.length; i++) {
      var l = archive.lenses[i];
      if (!l.v.paused || l.v.currentTime > 0) { return l.v.currentTime + l.delay - l.off; }
    }
    return 0;
  }
  function togglePlay() {
    if (!archive.lenses.length) { return; }
    if (archive.playing) {
      clearTimers();
      archive.lenses.forEach(function (l) { l.v.pause(); });
      setPlaying(false);
    } else {
      seekAll(Math.max(0, position()));
    }
  }
  function lensesReady() { return archive.lenses.filter(function (l) { return l.ready; }).length; }
  function releaseSync(gen, partial) {
    if (gen !== archive.gen || archive.released) { return; }
    archive.released = true;
    syncNote(partial ? T("cam_sync_partial") : "");
    seekAll(0);
  }
  function checkSync(gen) {
    if (gen !== archive.gen || archive.released) { return; }
    var n = lensesReady();
    if (n >= archive.lenses.length) { releaseSync(gen, false); } else { syncNote(T("cam_sync_loading", { n: n, m: archive.lenses.length })); }
  }
  function toggleBigLens(fig, v) {
    var on = !fig.classList.contains("cam-big");
    fig.classList.toggle("cam-big", on);
    document.body.classList.toggle("cam-has-big", on);
    v.controls = on;
  }
  function showEvent(ev, scroll) {
    archive.current = ev;
    fillList();
    stopSync();
    var gen = archive.gen;
    archive.released = false;
    archive.player.hidden = false;
    var title = archive.player.querySelector(".cam-event-title");
    title.textContent = T("cam_event_title", { t: dm(ev.begin) + " " + hhmmss(ev.begin) });
    syncNote(T("cam_sync_loading", { n: 0, m: cams.length }));
    archive.ctl.msg.hidden = true;
    if (scroll) { archive.player.scrollIntoView({ block: "start", behavior: "smooth" }); }
    getJson("/v1/cameras/archive/at?time=" + encodeURIComponent(ev.begin)).then(function (r) {
      if (gen !== archive.gen) { return; }
      var grid = document.getElementById("cam-syncgrid");
      if (!r.ok) { syncNote(errText(r.json)); return; }
      var any = false;
      r.json.lenses.forEach(function (l) {
        var cell = el("figure", "cam-tile cam-lens");
        cell.dataset.camera = l.camera;
        if (l.clip) {
          any = true;
          var v = document.createElement("video");
          v.className = "cam-video"; v.muted = true; v.preload = "auto"; v.setAttribute("playsinline", "");
          var lens = { v: v, delay: l.clip.delay_s || 0, off: l.clip.offset_s || 0, dur: l.clip.duration_s || 0, ready: false };
          var ok = function () { if (!lens.ready && v.readyState >= 3 && !v.seeking) { lens.ready = true; checkSync(gen); } };
          v.addEventListener("loadedmetadata", function () { if (lens.off > 0) { try { v.currentTime = lens.off; } catch (e) { /* the player decides */ } } });
          v.addEventListener("canplay", ok);
          v.addEventListener("seeked", ok);
          v.addEventListener("timeupdate", function () { if (!archive.dragging && archive.lenses[0] === lens) { archive.ctl.rng.value = String(Math.max(0, Math.round(position()))); } });
          v.addEventListener("ended", function () {
            if (archive.lenses.every(function (x) { return x.v.ended || x.v.paused; })) { setPlaying(false); }
          });
          v.addEventListener("error", function () { if (v.getAttribute("src")) { lens.ready = true; lens.failed = true; cell.appendChild(el("p", "note err", T("cam_clip_failed"))); checkSync(gen); } });
          cell.appendChild(v);
          var fig = cell;
          var capb = el("figcaption", "cam-cap");
          capb.appendChild(el("span", "cam-name", camName(l.camera)));
          capb.appendChild(btn(T("cam_full"), function () { toggleBigLens(fig, v); capb.lastChild.textContent = fig.classList.contains("cam-big") ? T("cam_close_big") : T("cam_full"); }, "cam-expand"));
          cell.appendChild(capb);
          v.src = "/v1/cameras/archive/clips/" + l.clip.id + "/video";
          archive.lenses.push(lens);
        } else {
          cell.appendChild(el("p", "cam-none", (status && status.loading) ? T("cam_not_loaded") : T("cam_no_motion")));
          cell.appendChild(el("figcaption", "cam-cap", camName(l.camera)));
        }
        grid.insertBefore(cell, archive.ctl.box);
      });
      var span = 0;
      archive.lenses.forEach(function (l) { span = Math.max(span, l.delay - l.off + l.dur); });
      archive.ctl.rng.max = String(Math.max(1, Math.round(span)));
      archive.ctl.rng.value = "0";
      archive.ctl.rng.disabled = !any;
      archive.ctl.pp.disabled = !any;
      if (!any) { syncNote(T("cam_not_loaded")); return; }
      archive.timers.push(setTimeout(function () { releaseSync(gen, true); }, SYNC_WAIT_MS));     // the time-out: show what is ready instead of waiting for ever
      checkSync(gen);
    });
  }
  function stepEvent(dir) {
    var cur = archive.current;
    if (!cur) { return; }
    var idx = -1;
    archive.events.forEach(function (e, i) { if (e.begin === cur.begin) { idx = i; } });
    var target = idx >= 0 ? archive.events[idx + (dir === "next" ? 1 : -1)] : null;
    if (target) { showEvent(target, false); return; }
    getJson("/v1/cameras/archive/step?time=" + encodeURIComponent(cur.begin) + "&dir=" + dir).then(function (r) {
      var t = r.ok ? r.json.time : null;
      if (!t || t.slice(0, 10) === archive.day) {
        archive.ctl.msg.hidden = false;
        archive.ctl.msg.textContent = T(dir === "next" ? "cam_no_next" : "cam_no_prev");
        return;
      }
      archive.day = t.slice(0, 10);
      var sel = archive.box.querySelector("select");
      if (sel) { sel.value = archive.day; }
      loadDay(function () {
        var tm = ms(t), hit = null;
        archive.events.forEach(function (e) { if (e.beginMs - EVENT_GAP_MS <= tm && tm <= e.endMs + EVENT_GAP_MS) { hit = e; } });
        if (hit) { showEvent(hit, false); }
      });
    });
  }

  // ── start ─────────────────────────────────────────────────────────────────
  window.addEventListener("pagehide", function () { stopAll(); stopSync(); });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stopAll(); note(T("cam_paused")); } else { note(""); if (root.dataset.ready && pane === "live") { startAll(); } }
  });
  getJson("/v1/cameras").then(function (r) {
    clear(root);
    if (!r.ok) { note(errText(r.json)); return; }
    cams = r.json.cameras;
    hdOk = !!r.json.hd_available;
    status = r.json.archive ? r.json.archive.status : null;
    if (!r.json.live) { note(T("cam_no_link")); }
    cams.forEach(function (c) { archive.on[c.id] = true; });
    var bar = el("div", "daychips cam-tabs");
    bar.setAttribute("role", "group");
    [["live", T("cam_tab_live")], ["archive", T("cam_tab_archive")]].forEach(function (x) {
      var b = btn(x[1], function () { showPane(x[0]); }, "daychip wide cam-tab");
      b.setAttribute("aria-pressed", x[0] === "live" ? "true" : "false");
      b.dataset.tab = x[0];
      tabs[x[0]] = b;
      bar.appendChild(b);
    });
    root.appendChild(bar);
    panes.live = el("section", "cam-pane");
    panes.archive = el("section", "card cam-archive");
    panes.archive.id = "cam-archive";
    panes.archive.hidden = true;
    root.appendChild(panes.live);
    root.appendChild(panes.archive);
    renderLive(panes.live);
    root.dataset.ready = "yes";
    getJson("/v1/cameras/archive/days").then(function (d) {
      archive.days = d.ok ? d.json.days : [];
      if (archive.days.length) { archive.day = archive.days[0].day; }
      renderArchive(panes.archive);
    });
    if (location.hash === "#archive") { showPane("archive"); } else if (!document.hidden) { startAll(); }
  });
}());

(function () {
  "use strict";
  // Task 34b: the cameras page. All three lenses play at once (a 2x2 grid, sub streams), a tap expands one (and "Чётче" switches it to the main stream, at home only);
  // the archive shows ONE merged list of events of all lenses for a day and a synchronised view: pick an event and the three lenses show what they recorded at that moment.
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
  var cams = [], tiles = {}, hdOk = false, status = null, pageGen = 0;
  var archive = { days: [], day: "", on: {}, clips: [], moment: "" };
  var STAGGER_MS = 1500, RETRY_MS = 6000;

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
  function hhmm(iso) { return iso ? iso.slice(11, 16) : ""; }
  function hhmmss(iso) { return iso ? iso.slice(11, 19) : ""; }
  function camName(id) { for (var i = 0; i < cams.length; i++) { if (cams[i].id === id) { return cams[i].name; } } return id; }
  function liveUrl(id, hd) { return "/v1/cameras/" + id + "/live?codec=h264" + (hd ? "&quality=hd" : "") + "&t=" + Date.now(); }

  // ── live grid ─────────────────────────────────────────────────────────────
  function setState(t, text, isErr) {
    t.status.textContent = text || "";
    t.status.hidden = !text;
    t.status.className = "cam-status" + (isErr ? " note err" : " meta");
  }
  function stopTile(t) {
    t.gen = (t.gen || 0) + 1;
    if (t.timer) { clearTimeout(t.timer); t.timer = null; }
    t.video.pause();
    t.video.removeAttribute("src");
    t.video.load();
  }
  function startTile(t, hd) {
    stopTile(t);
    var gen = t.gen;
    t.hd = !!hd;
    setState(t, T("cam_wait_one"), false);
    t.again.hidden = true;
    t.video.src = liveUrl(t.cam.id, t.hd);
    t.video.play().catch(function () {});
    t.hdBtn.textContent = t.hd ? T("cam_hd_off") : T("cam_hd");
  }
  function startAll() {
    var gen = ++pageGen;
    var order = cams.map(function (c) { return tiles[c.id]; });
    order.forEach(function (t, i) {
      if (t.big) { return; }
      t.timer = setTimeout(function () { if (gen === pageGen && !document.hidden) { startTile(t, false); } }, i * STAGGER_MS);
    });
  }
  function stopAll() { pageGen++; Object.keys(tiles).forEach(function (k) { stopTile(tiles[k]); }); }
  function buildTile(c) {
    var t = { cam: c, big: false, hd: false, gen: 0, timer: null, retried: false };
    var fig = el("figure", "cam-tile");
    fig.dataset.camera = c.id;
    var video = document.createElement("video");
    video.className = "cam-video";
    video.muted = true; video.autoplay = true; video.setAttribute("playsinline", "");
    var status = el("p", "cam-status meta", T("cam_wait_one"));
    var cap = el("figcaption", "cam-cap");
    cap.appendChild(el("span", "cam-name", c.name));
    var expand = btn(T("cam_full"), function () { toggleBig(t); }, "cam-expand");
    var hdBtn = btn(T("cam_hd"), function () { startTile(t, !t.hd); }, "cam-hdbtn");
    hdBtn.hidden = true;
    var again = btn(T("cam_continue"), function () { t.retried = false; startTile(t, t.hd); }, "cam-continue");
    again.hidden = true;
    cap.appendChild(expand); cap.appendChild(hdBtn);
    fig.appendChild(video); fig.appendChild(status); fig.appendChild(again); fig.appendChild(cap);
    t.fig = fig; t.video = video; t.status = status; t.hdBtn = hdBtn; t.again = again; t.expand = expand;
    video.addEventListener("playing", function () { setState(t, "", false); t.retried = false; });
    video.addEventListener("click", function () { toggleBig(t); });
    video.addEventListener("error", function () {
      if (!video.getAttribute("src")) { return; }                       // we removed the source ourselves
      setState(t, T("cam_no_link"), true);
      t.again.hidden = false;
      if (!t.retried && !document.hidden) {                             // one quiet automatic retry (a cold camera source needs a moment)
        t.retried = true;
        var g = t.gen;
        t.timer = setTimeout(function () { if (g === t.gen && !document.hidden) { startTile(t, t.hd); } }, RETRY_MS);
      }
    });
    video.addEventListener("ended", function () { setState(t, T("cam_stopped"), false); t.again.hidden = false; });
    return t;
  }
  function toggleBig(t) {
    t.big = !t.big;
    t.fig.classList.toggle("cam-big", t.big);
    document.body.classList.toggle("cam-has-big", t.big);
    t.expand.textContent = t.big ? T("cam_back") : T("cam_full");
    t.hdBtn.hidden = !(t.big && hdOk);
    if (!t.big && t.hd) { startTile(t, false); }                        // leaving the big view returns to the light stream
  }
  function renderGrid(box) {
    var hint = el("p", "meta", T("cam_grid_hint"));
    box.appendChild(hint);
    var grid = el("div", "cam-grid");
    tiles = {};
    cams.forEach(function (c) { var t = buildTile(c); tiles[c.id] = t; grid.appendChild(t.fig); });
    var more = el("div", "cam-tile cam-more");
    more.appendChild(el("p", "meta", T("cam_hd_note")));
    more.appendChild(btn(T("cam_archive"), function () { var a = document.getElementById("cam-archive"); if (a) { a.scrollIntoView({ block: "start" }); } }, "cam-to-archive"));
    grid.appendChild(more);
    box.appendChild(grid);
  }

  // ── archive: one merged list of all lenses, and the synchronised view ─────
  function activeCams() { return cams.filter(function (c) { return archive.on[c.id]; }).map(function (c) { return c.id; }); }
  function renderArchive(box) {
    clear(box);
    box.appendChild(el("h2", "", T("cam_archive_all")));
    if (status && status.loading) { box.appendChild(el("p", "note", T("cam_loading_now", { t: status.next_pull }))); }
    if (!archive.days.length) { box.appendChild(el("p", "meta", T("cam_archive_none"))); return; }
    var sel = document.createElement("select");
    sel.setAttribute("aria-label", T("cam_pick_day"));
    archive.days.forEach(function (d) {
      var o = document.createElement("option");
      o.value = d.day; o.textContent = d.day.slice(8, 10) + "." + d.day.slice(5, 7) + " · " + d.clips;
      if (d.day === archive.day) { o.selected = true; }
      sel.appendChild(o);
    });
    sel.addEventListener("change", function () { archive.day = sel.value; archive.clips = []; loadDay(box); });
    box.appendChild(sel);
    var chips = el("div", "daychips cam-chips");
    cams.forEach(function (c) {
      var b = btn(c.name, function () { archive.on[c.id] = !archive.on[c.id]; b.setAttribute("aria-pressed", archive.on[c.id] ? "true" : "false"); fillList(box); }, "daychip wide");
      b.setAttribute("aria-pressed", archive.on[c.id] ? "true" : "false");
      chips.appendChild(b);
    });
    box.appendChild(chips);
    box.appendChild(el("h3", "", T("cam_events")));
    var list = el("ul", "cam-clips"); list.id = "cam-clips";
    box.appendChild(list);
    var sync = el("div", "cam-sync"); sync.id = "cam-sync";
    box.appendChild(sync);
    if (archive.clips.length) { fillList(box); } else { loadDay(box); }
    if (archive.moment) { showMoment(box, archive.moment); } else { sync.appendChild(el("p", "meta", T("cam_sync_pick"))); }
  }
  function loadDay(box) {
    getJson("/v1/cameras/archive/clips?day=" + encodeURIComponent(archive.day)).then(function (r) {
      archive.clips = r.ok ? r.json.clips : [];
      fillList(box);
    });
  }
  function fillList(box) {
    var list = box.querySelector("#cam-clips");
    if (!list) { return; }
    clear(list);
    var on = activeCams();
    var rows = archive.clips.filter(function (c) { return on.indexOf(c.camera) >= 0; });
    if (!rows.length) { list.appendChild(el("li", "meta", T("cam_archive_none"))); return; }
    rows.forEach(function (c) {
      var li = el("li", "cam-clip");
      li.appendChild(btn(T("cam_event_line", { t: hhmmss(c.begin), c: camName(c.camera) }), function () { archive.moment = c.begin; showMoment(box, c.begin); }, "cam-clip-btn"));
      list.appendChild(li);
    });
  }
  function showMoment(box, moment) {
    var sync = box.querySelector("#cam-sync");
    clear(sync);
    sync.appendChild(el("h3", "", T("cam_sync")));
    sync.appendChild(el("p", "meta", T("cam_moment", { t: moment.slice(8, 10) + "." + moment.slice(5, 7) + " " + hhmmss(moment) })));
    var nav = el("div", "cam-actions");
    nav.appendChild(btn(T("cam_prev_event"), function () { stepEvent(box, "prev"); }, "cam-prev"));
    nav.appendChild(btn(T("cam_next_event"), function () { stepEvent(box, "next"); }, "cam-next"));
    sync.appendChild(nav);
    var grid = el("div", "cam-grid cam-syncgrid");
    sync.appendChild(grid);
    var msg = el("p", "meta cam-stepnote"); msg.hidden = true;
    sync.appendChild(msg);
    getJson("/v1/cameras/archive/at?time=" + encodeURIComponent(moment)).then(function (r) {
      if (!r.ok) { grid.appendChild(el("p", "note err", errText(r.json))); return; }
      r.json.lenses.forEach(function (l) {
        var cell = el("figure", "cam-tile");
        cell.dataset.camera = l.camera;
        if (l.clip) {
          var v = document.createElement("video");
          v.className = "cam-video"; v.controls = true; v.muted = true; v.autoplay = true; v.setAttribute("playsinline", "");
          v.src = "/v1/cameras/archive/clips/" + l.clip.id + "/video";
          v.addEventListener("loadedmetadata", function () { try { v.currentTime = l.clip.offset_s || 0; } catch (e) { /* the player decides */ } });
          cell.appendChild(v);
        } else {
          cell.appendChild(el("p", "cam-none", (status && status.loading) ? T("cam_not_loaded") : T("cam_no_motion")));
        }
        cell.appendChild(el("figcaption", "cam-cap", camName(l.camera)));
        grid.appendChild(cell);
      });
    });
  }
  function stepEvent(box, dir) {
    getJson("/v1/cameras/archive/step?time=" + encodeURIComponent(archive.moment) + "&dir=" + dir).then(function (r) {
      var next = r.ok ? r.json.time : null;
      if (!next) {
        var m = box.querySelector(".cam-stepnote");
        if (m) { m.hidden = false; m.textContent = T(dir === "next" ? "cam_no_next" : "cam_no_prev"); }
        return;
      }
      archive.moment = next;
      if (next.slice(0, 10) !== archive.day) { archive.day = next.slice(0, 10); archive.clips = []; renderArchive(box); } else { showMoment(box, next); }
    });
  }

  // ── start ─────────────────────────────────────────────────────────────────
  function closeAll() { stopAll(); }
  window.addEventListener("pagehide", closeAll);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { stopAll(); note(T("cam_paused")); } else { note(""); if (root.dataset.ready) { startAll(); } }
  });
  getJson("/v1/cameras").then(function (r) {
    clear(root);
    if (!r.ok) { note(errText(r.json)); return; }
    cams = r.json.cameras;
    hdOk = !!r.json.hd_available;
    status = r.json.archive ? r.json.archive.status : null;
    if (!r.json.live) { note(T("cam_no_link")); }
    cams.forEach(function (c) { archive.on[c.id] = true; });
    renderGrid(root);
    var arch = el("section", "card cam-archive");
    arch.id = "cam-archive";
    root.appendChild(arch);
    root.dataset.ready = "yes";
    getJson("/v1/cameras/archive/days").then(function (d) {
      archive.days = d.ok ? d.json.days : [];
      if (archive.days.length) { archive.day = archive.days[0].day; }
      renderArchive(arch);
    });
    if (!document.hidden) { startAll(); }
  });
}());

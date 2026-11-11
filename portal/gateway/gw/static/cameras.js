(function () {
  "use strict";
  // Task 34: the cameras page. Three tiles (a still image refreshed every few seconds, which also keeps the camera connection warm),
  // a live view of one lens, and the archive of motion clips. Everything from the server goes in as text, never as markup; no inline style.
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
  var cams = [], tileTimers = [], tilesLive = false, viewer = null, archive = { days: [], day: "", cam: "" };
  var REFRESH_MS = 5000, RETRY_MS = 15000;

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
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function hhmm(iso) { return iso ? iso.slice(11, 16) : ""; }
  function camName(id) { for (var i = 0; i < cams.length; i++) { if (cams[i].id === id) { return cams[i].name; } } return id; }

  // ── tiles ─────────────────────────────────────────────────────────────────
  function stopTiles() { tilesLive = false; tileTimers.forEach(function (t) { clearTimeout(t); }); tileTimers = []; }
  function startTile(c, img, status) {
    function tick() {
      if (!tilesLive || !img.isConnected) { return; }
      if (document.hidden) { tileTimers.push(setTimeout(tick, REFRESH_MS)); return; }
      var probe = new Image();
      probe.onload = function () { img.src = probe.src; status.hidden = true; tileTimers.push(setTimeout(tick, REFRESH_MS)); };
      probe.onerror = function () { status.hidden = false; tileTimers.push(setTimeout(tick, RETRY_MS)); };
      probe.src = "/v1/cameras/" + c.id + "/snapshot?t=" + Date.now();
    }
    tick();
  }
  function showTiles() {
    stopTiles();
    clear(root);
    root.dataset.mode = "tiles";
    var grid = el("div", "cam-grid");
    tilesLive = true;
    cams.forEach(function (c) {
      var fig = el("figure", "cam-tile");
      fig.dataset.camera = c.id;
      var img = el("img", "cam-snap");
      img.alt = c.name;
      var status = el("p", "cam-status note err", T("cam_no_link"));
      status.hidden = true;
      var cap = el("figcaption", "cam-cap");
      cap.appendChild(el("span", "cam-name", c.name));
      cap.appendChild(btn(T("cam_open_live"), function () { showLive(c.id); }, "cam-open"));
      fig.appendChild(img); fig.appendChild(status); fig.appendChild(cap);
      grid.appendChild(fig);
      startTile(c, img, status);
    });
    root.appendChild(grid);
    var arch = el("section", "card cam-archive");
    arch.id = "cam-archive";
    root.appendChild(arch);
    renderArchive(arch);
  }

  // ── live ──────────────────────────────────────────────────────────────────
  function closeViewer() {
    if (viewer && viewer.video) { viewer.video.pause(); viewer.video.removeAttribute("src"); viewer.video.load(); }
    viewer = null;
  }
  function showLive(id, codec) {
    stopTiles();
    closeViewer();
    clear(root);
    root.dataset.mode = "live";
    codec = codec || "h264";
    var c = { id: id };
    var top = el("div", "cam-top");
    top.appendChild(btn(T("cam_back"), function () { closeViewer(); showTiles(); }, "back"));
    top.appendChild(el("h2", "cam-live-name", camName(id)));
    var video = document.createElement("video");
    video.className = "cam-video";
    video.muted = true; video.autoplay = true; video.controls = true; video.setAttribute("playsinline", "");
    var status = el("p", "meta cam-connecting", T("cam_connecting"));
    var actions = el("div", "cam-actions");
    var again = btn(T("cam_continue"), function () { showLive(id, codec); }, "cam-continue");
    again.hidden = true;
    var other = btn(T("cam_alt_mode"), function () { showLive(id, codec === "h264" ? "h265" : "h264"); }, "cam-alt");
    actions.appendChild(again); actions.appendChild(other);
    root.appendChild(top); root.appendChild(video); root.appendChild(status); root.appendChild(actions);
    video.addEventListener("playing", function () { status.hidden = true; });
    video.addEventListener("error", function () {
      status.hidden = false;
      status.textContent = T("cam_live_failed");
      status.className = "note err cam-connecting";
      again.hidden = false;
    });
    video.addEventListener("ended", function () {
      status.hidden = false;
      status.textContent = T("cam_stopped");
      again.hidden = false;
    });
    video.src = "/v1/cameras/" + c.id + "/live?codec=" + codec + "&t=" + Date.now();
    viewer = { video: video };
  }

  // ── archive ───────────────────────────────────────────────────────────────
  function renderArchive(box) {
    clear(box);
    box.appendChild(el("h2", "", T("cam_archive")));
    if (!archive.days.length) { box.appendChild(el("p", "meta", T("cam_archive_none"))); return; }
    var sel = document.createElement("select");
    sel.setAttribute("aria-label", T("cam_day"));
    archive.days.forEach(function (d) {
      var o = document.createElement("option");
      o.value = d.day; o.textContent = d.day.slice(8, 10) + "." + d.day.slice(5, 7) + " · " + d.clips;
      if (d.day === archive.day) { o.selected = true; }
      sel.appendChild(o);
    });
    sel.addEventListener("change", function () { archive.day = sel.value; loadClips(box); });
    var chips = el("div", "daychips cam-chips");
    var all = [{ id: "", name: T("cam_all_cams") }].concat(cams);
    all.forEach(function (c) {
      var b = btn(c.name, function () { archive.cam = c.id; loadClips(box); }, "daychip wide");
      b.setAttribute("aria-pressed", archive.cam === c.id ? "true" : "false");
      chips.appendChild(b);
    });
    box.appendChild(sel); box.appendChild(chips);
    var list = el("ul", "cam-clips"); list.id = "cam-clips";
    box.appendChild(list);
    var player = el("div", "cam-player"); player.id = "cam-player";
    box.appendChild(player);
    box.appendChild(el("p", "meta", T("cam_archive_note")));
    fillClips(box);
  }
  function loadClips(box) { renderArchive(box); }
  function fillClips(box) {
    var list = box.querySelector("#cam-clips");
    var path = "/v1/cameras/archive/clips?day=" + encodeURIComponent(archive.day) + (archive.cam ? "&camera=" + encodeURIComponent(archive.cam) : "");
    getJson(path).then(function (r) {
      clear(list);
      if (!r.ok) { list.appendChild(el("li", "note err", errText(r.json))); return; }
      var clips = r.json.clips;
      if (!clips.length) { list.appendChild(el("li", "meta", T("cam_archive_none"))); return; }
      clips.forEach(function (c) {
        var li = el("li", "cam-clip");
        li.appendChild(btn(T("cam_clip_line", { t: hhmm(c.begin), d: Math.round(c.duration_s), c: camName(c.camera) }), function () { playClip(box, c); }, "cam-clip-btn"));
        list.appendChild(li);
      });
    });
  }
  function playClip(box, c) {
    var player = box.querySelector("#cam-player");
    clear(player);
    var v = document.createElement("video");
    v.className = "cam-video";
    v.controls = true; v.autoplay = true; v.setAttribute("playsinline", "");
    v.src = "/v1/cameras/archive/clips/" + c.id + "/video";
    v.addEventListener("error", function () { player.appendChild(el("p", "note err", T("cam_clip_failed"))); });
    player.appendChild(el("h3", "", T("cam_play_title", { c: camName(c.camera), t: hhmm(c.begin) })));
    player.appendChild(v);
    player.scrollIntoView({ block: "nearest" });
  }

  // ── start ─────────────────────────────────────────────────────────────────
  window.addEventListener("pagehide", function () { stopTiles(); closeViewer(); });
  getJson("/v1/cameras").then(function (r) {
    if (!r.ok) { clear(root); note(errText(r.json)); return; }
    cams = r.json.cameras;
    if (!r.json.live) { note(T("cam_no_link")); }
    getJson("/v1/cameras/archive/days").then(function (d) {
      archive.days = d.ok ? d.json.days : [];
      if (archive.days.length) { archive.day = archive.days[0].day; }
      showTiles();
    });
  });
}());

(function () {
  "use strict";
  var I = {};
  try { I = JSON.parse(document.getElementById("i18n").textContent); } catch (e) { I = { S: {}, E: {} }; }
  function T(key, vars) {
    var s = (I.S && I.S[key]) || key;
    if (vars) { Object.keys(vars).forEach(function (k) { s = s.split("{" + k + "}").join(vars[k]); }); }
    return s;
  }
  function errText(e) {
    if (!e) { return T("err_generic", { m: "?" }); }
    return (I.E && I.E[e.code]) || e.message || e.code;
  }
  function csrf() { var m = document.querySelector('meta[name="csrf"]'); return m ? m.content : ""; }
  function uuid() { return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2); }
  function api(method, path, body, extra) {
    var h = { "Accept": "application/json", "X-CSRF-Token": csrf() };
    if (body !== undefined) { h["Content-Type"] = "application/json"; }
    if (method !== "GET") { h["Idempotency-Key"] = uuid(); }
    Object.keys(extra || {}).forEach(function (k) { h[k] = extra[k]; });
    return fetch(path, { method: method, headers: h, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" })
      .then(function (r) {
        netBanner(false);
        if (r.status === 401) { location.href = "/login"; return new Promise(function () {}); }
        if (r.status === 204) { return { ok: true, status: 204, json: null, headers: r.headers }; }
        return r.json().then(function (j) { return { ok: r.ok, status: r.status, json: j, headers: r.headers }; }, function () { return { ok: r.ok, status: r.status, json: null, headers: r.headers }; });
      }, function () {                                    // fetch itself failed: no route to the house (Tailscale off, server down) - say so, never freeze
        netBanner(true);
        return { ok: false, status: 0, json: { error: { code: "network", message: T("net_down_short") } }, headers: new Headers() };
      });
  }
  function netBanner(on) { var b = document.getElementById("net-banner"); if (b) { b.hidden = !on; } }
  (function lifecycle() {                               // Task 30: a phone sleeps and wakes; never show a frozen, stale page
    var hiddenAt = 0;
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { hiddenAt = Date.now(); return; }
      if (hiddenAt && Date.now() - hiddenAt > 30000) { location.reload(); }          // the event stream may be dead and the states old: reload what the page shows
    });
    window.addEventListener("offline", function () { netBanner(true); });
    window.addEventListener("online", function () { location.reload(); });
    document.addEventListener("click", function (ev) {
      if (ev.target.closest("#refresh,#net-retry")) { location.reload(); }
    });
  }());
  function q(sel, root) { return (root || document).querySelector(sel); }
  function qa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function hhmm(iso) { return iso ? iso.slice(11, 16) : ""; }
  function note(card, text, isErr) {
    var n = q(".note", card);
    if (!n) { return; }
    n.textContent = text || "";
    n.hidden = !text;
    n.className = "note" + (isErr ? " err" : "");
  }

  // ── home: device cards ─────────────────────────────────────────────────────
  function renderDevice(d) {
    var card = q('[data-device="' + d.id + '"]');
    if (!card) { return; }
    card.dataset.stale = d.stale ? "yes" : "no";
    if (card.dataset.mode === "timed") {                                // the card structure depends on the state and the run: redraw the page
      if (d.state !== card.dataset.state || (d.run ? d.run.id : "") !== (card.dataset.run || "")) { reloadSoon(); return; }
    }
    card.dataset.state = d.state;
    var b = q('[data-action="toggle"]', card);
    if (!b) { return; }
    b.className = "big " + (d.state === "on" ? "is-on" : "is-off");
    b.dataset.on = d.state === "on" ? "true" : "false";
    b.disabled = !d.can_switch;
    b.removeAttribute("aria-busy");
    q(".state", b).textContent = T("state_" + d.state);
    q(".act", b).textContent = d.state === "on" ? T("turn_off") : T("turn_on");
    var src = d.source === "manual" ? (d.manual_until ? T("manual_until", { t: hhmm(d.manual_until) }) : T("source_manual")) : (d.source === "schedule" ? T("source_schedule") : T("source_unknown"));
    q(".src", card).textContent = src;
    var edge = q(".edge", card);
    if (edge) { edge.textContent = d.next_edge ? " · " + T("next_edge_" + d.next_edge.action, { t: hhmm(d.next_edge.at) }) : ""; }
    var n = q(".note", card);
    if (d.stale) { note(card, T("stale"), false); } else if (n && n.dataset.keep !== "1") { note(card, ""); }
  }
  function refreshDevice(id) {
    return api("GET", "/v1/devices/" + id).then(function (r) { if (r.ok) { renderDevice(r.json); } });
  }
  function onToggle(btn) {
    var card = btn.closest("[data-device]");
    var id = card.dataset.device;
    var want = btn.dataset.on !== "true";
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    q(".act", btn).textContent = T("switching");
    api("POST", "/v1/devices/" + id + "/switch", { on: want }).then(function (r) {
      var n = q(".note", card);
      if (r.ok && r.json.outcome === "confirmed") { n.dataset.keep = "0"; note(card, ""); }
      else if (r.ok) { n.dataset.keep = "1"; note(card, T("unconfirmed", { s: T("state_" + r.json.observed) }), true); }
      else { n.dataset.keep = "1"; note(card, errText(r.json && r.json.error), true); }
      return refreshDevice(id);
    });
  }

  // ── timed runs (Task 29): "Включить на…", "+30 мин"; "Выключить" is the ordinary switch-off, which also closes the run ──
  var devReload = null;
  function reloadSoon() { clearTimeout(devReload); devReload = setTimeout(function () { location.reload(); }, 1200); }
  function runProblems(e) {
    var ps = e && e.details && e.details.problems;
    if (ps && ps.length) { return ps.map(function (p) { var k = "run_p_" + p.code; return (I.S && I.S[k]) ? T(k) : p.message; }).join(" "); }
    return errText(e);
  }
  function onRun(btn) {
    var card = btn.closest("[data-device]");
    var id = card.dataset.device;
    var act = btn.dataset.action;
    if (act === "run-menu") { var pr = q(".presets", card); if (pr) { pr.hidden = !pr.hidden; } return; }
    var path = "/v1/devices/" + id + (act === "run-start" ? "/run" : "/run/extend");
    qa("button", card).forEach(function (x) { x.disabled = true; });
    note(card, T("run_starting"), false);
    api("POST", path, { minutes: parseInt(btn.dataset.min, 10) }).then(function (r) {
      if (r.ok && r.json.outcome === "confirmed") { note(card, T("confirmed"), false); }
      else if (r.ok) { note(card, T("unconfirmed", { s: T("state_" + r.json.observed) }), true); }
      else { note(card, runProblems(r.json && r.json.error), true); }
      setTimeout(function () { location.reload(); }, r.ok ? 800 : 2500);
    });
  }
  function tickRuns() {
    qa(".run-line").forEach(function (el) {
      var left = Math.max(0, Math.floor((Date.parse(el.dataset.ends) - Date.now()) / 1000));
      var txt = left >= 300 ? (Math.floor(left / 60) + 1) + " мин" : Math.floor(left / 60) + ":" + ("0" + (left % 60)).slice(-2);
      var s = q(".remain", el);
      if (s) { s.textContent = T("run_left", { r: txt }); }
      if (left === 0) { reloadSoon(); }
    });
  }
  setInterval(tickRuns, 1000);

  // ── home: vacuums ──────────────────────────────────────────────────────────
  var vacReload = null;
  function onVacuum(btn) {
    var card = btn.closest("[data-vacuum]");
    var id = card.dataset.vacuum;
    var act = btn.dataset.action;
    var path = act === "vac-start" ? "/v1/vacuums/" + id + "/scenarios/" + btn.dataset.scenario + "/start" : "/v1/vacuums/" + id + (act === "vac-pause" ? "/pause" : act === "vac-resume" ? "/resume" : "/dock");
    btn.disabled = true;
    api("POST", path).then(function (r) {
      if (r.ok && act === "vac-pause" && r.json.action === "dock") { note(card, T("plan_cancelled"), true); }
      else if (r.ok && r.json.outcome === "confirmed") { note(card, T("confirmed"), false); }
      else if (r.ok) { note(card, T("unconfirmed", { s: JSON.stringify(r.json.observed) }), true); }
      else { note(card, errText(r.json && r.json.error), true); }
      setTimeout(function () { location.reload(); }, 1500);
    });
  }

  // ── events (SSE) ───────────────────────────────────────────────────────────
  function listen(handlers) {
    if (!window.EventSource) { return; }
    var es = new EventSource("/v1/events");
    es.onopen = function () { netBanner(false); };
    es.onerror = function () {                                         // the browser retries by itself (retry: 3000); until it succeeds the page says it is not live
      netBanner(true);
      if (es.readyState === 2) { api("GET", "/v1/capabilities"); }       // closed for good: an expired or revoked session answers 401 here and sends the page to /login
    };
    Object.keys(handlers).forEach(function (t) { es.addEventListener(t, function (ev) { try { handlers[t](JSON.parse(ev.data)); } catch (e) { /* ignore a malformed event */ } }); });
    return es;
  }

  // ── people ─────────────────────────────────────────────────────────────────
  function showOut(card, html) { var o = q(".out", card); o.hidden = false; o.innerHTML = html; }
  function esc(s) { var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }
  function onPeople(btn) {
    var act = btn.dataset.action;
    var card = btn.closest("[data-person],[data-phone]");
    if (act === "revoke") {
      api("DELETE", "/v1/phones/" + card.dataset.phone).then(function (r) { if (r.ok) { card.remove(); } else { alert(errText(r.json && r.json.error)); } });
      return;
    }
    var pid = card.dataset.person;
    if (act === "enrol-code") {
      api("POST", "/v1/people/" + pid + "/enrolment-codes").then(function (r) {
        if (!r.ok) { return showOut(card, esc(errText(r.json && r.json.error))); }
        showOut(card, '<p class="code">' + esc(r.json.code) + '</p><p>' + esc(T("enrol_ttl")) + '</p><div class="qr">' + r.json.qr_svg + '</div>');
      });
    } else if (act === "setup-link") {
      api("POST", "/v1/people/" + pid + "/setup-link").then(function (r) {
        showOut(card, r.ok ? '<p class="seed">' + esc(r.json.url) + '</p>' : esc(errText(r.json && r.json.error)));
      });
    } else if (act === "toggle-web") {
      api("PATCH", "/v1/people/" + pid, { web_login_enabled: btn.dataset.on !== "true" }).then(function (r) { if (r.ok) { location.reload(); } else { alert(errText(r.json && r.json.error)); } });
    }
  }

  // ── schedule editor of a device (plug, vacuum plan): plain words, day chips, times in 15-minute steps (Task 31 Part D) ─────────────────────────────
  // Behaviour and API are unchanged: the same PUT with If-Match, preview and undo; the guardrails are still enforced by the service, the controls just do not offer what it would refuse.
  function initEditor() {
    var doc = JSON.parse(document.getElementById("doc").textContent);
    var isPlug = doc.kind === "plug";
    var rulesBox = q("#rules");
    var msg = q("#editor-msg");
    var etag = doc.etag;
    var readonly = !!q("#editor[data-readonly]");
    var rules = JSON.parse(JSON.stringify(doc.schedule.rules));
    var g = doc.schedule.guardrails || {};
    var name = (q("#editor") && q("#editor").dataset.name) || T("hh_name_fallback");
    function mins(hm) { return parseInt(hm.slice(0, 2), 10) * 60 + parseInt(hm.slice(3), 10); }
    function fmt(m) { return (m < 600 ? "0" : "") + Math.floor(m / 60) + ":" + (m % 60 < 10 ? "0" : "") + (m % 60); }
    function plural(n, f) { var a = Math.abs(n) % 100, b = a % 10; if (a > 10 && a < 20) { return f[2]; } if (b > 1 && b < 5) { return f[1]; } if (b === 1) { return f[0]; } return f[2]; }
    function span(m) {                                                    // 120 -> "2 часа", 90 -> "1 час 30 минут"
      var h = Math.floor(m / 60), r = m % 60, out = [];
      if (h) { out.push(h + " " + plural(h, I.S.hh_unit_hour)); }
      if (r || !h) { out.push(r + " " + plural(r, I.S.hh_unit_min)); }
      return out.join(" ");
    }
    var lo = g.allowed_from ? mins(g.allowed_from) : 0, hi = g.allowed_to ? mins(g.allowed_to) : 1425;
    var parts = [];                                                       // the guardrails as plain sentences
    if (g.max_on_minutes) { parts.push(T("hh_guard_max", { name: name, m: span(g.max_on_minutes) })); }
    if (g.min_gap_minutes) { parts.push(T("hh_guard_gap", { g: span(g.min_gap_minutes) })); }
    if (g.allowed_from) { parts.push(T("hh_guard_range", { a: g.allowed_from, b: g.allowed_to })); }
    if (g.max_triggers_per_day) { parts.push(T("hh_guard_trig", { n: g.max_triggers_per_day })); }
    q("#guard").textContent = parts.join(" ");
    function say(text, kind) { msg.hidden = !text; msg.textContent = text || ""; msg.className = "msg" + (kind ? " " + kind : ""); }
    function options(current) {
      var out = [];
      for (var m = lo; m <= hi; m += 15) { if (m >= 120 && m < 240) { continue; } out.push(m); }          // 02:00-03:59 is never used (clock changes)
      if (current !== null && out.indexOf(current) < 0) { out.push(current); out.sort(function (a, b) { return a - b; }); }
      return out;
    }
    function timeField(label, key, r) {
      var cur = r[key] ? mins(r[key]) : null;
      var lab = document.createElement("label");
      lab.appendChild(document.createTextNode(label));
      var sel = document.createElement("select");
      options(cur).forEach(function (m) { var o = document.createElement("option"); o.value = fmt(m); o.textContent = fmt(m); sel.appendChild(o); });
      if (cur !== null) { sel.value = fmt(cur); }
      sel.disabled = readonly;
      sel.addEventListener("change", function () { r[key] = sel.value; });
      lab.appendChild(sel);
      return lab;
    }
    function render() {
      rulesBox.textContent = "";
      rules.forEach(function (r, i) {
        var d = document.createElement("div");
        d.className = "rule";
        if (rules.length > 1) { var h = document.createElement("strong"); h.textContent = T("hh_window_n", { n: i + 1 }); d.appendChild(h); }
        var chips = document.createElement("div");
        chips.className = "daychips";
        chips.setAttribute("role", "group");
        chips.setAttribute("aria-label", T("hh_days"));
        I.S.dow.forEach(function (n, k) {
          var b = document.createElement("button");
          b.type = "button"; b.className = "daychip"; b.textContent = n; b.disabled = readonly;
          b.setAttribute("aria-pressed", r.days.indexOf(k + 1) >= 0 ? "true" : "false");
          b.addEventListener("click", function () {
            var j = r.days.indexOf(k + 1);
            if (j >= 0) { r.days.splice(j, 1); } else { r.days.push(k + 1); r.days.sort(function (a, c) { return a - c; }); }
            b.setAttribute("aria-pressed", r.days.indexOf(k + 1) >= 0 ? "true" : "false");
          });
          chips.appendChild(b);
        });
        d.appendChild(chips);
        var times = document.createElement("div");
        times.className = "times";
        times.appendChild(timeField(T(isPlug ? "hh_on" : "hh_trigger"), "start", r));
        if (isPlug) { times.appendChild(timeField(T("hh_off"), "end", r)); }
        d.appendChild(times);
        if (!readonly && rules.length > 1) {
          var rm = document.createElement("button");
          rm.type = "button"; rm.className = "danger"; rm.textContent = T("hh_remove"); rm.dataset.del = String(i);
          d.appendChild(rm);
        }
        rulesBox.appendChild(d);
      });
    }
    function problems(e) {
      if (!e) { return T("err_generic", { m: "?" }); }
      var list = (e.details && e.details.problems) || [];
      var head = (I.E && I.E[e.code]) || e.message;
      return head + (list.length ? " " + list.map(function (p) { return ((I.E && I.E[p.code]) || "") + " (" + p.message + ")"; }).join(" ") : "");
    }
    function check() {
      for (var i = 0; i < rules.length; i++) {
        if (!rules[i].days.length) { return T("sch_err_days"); }
        if (isPlug && mins(rules[i].end) <= mins(rules[i].start)) { return T("sch_err_range"); }
      }
      return null;
    }
    rulesBox.addEventListener("click", function (ev) {
      var del = ev.target.closest("[data-del]");
      if (del) { rules.splice(parseInt(del.dataset.del, 10), 1); render(); }
    });
    q("#add-rule").addEventListener("click", function () {
      var n = 1;
      while (rules.some(function (r) { return r.id === "r" + n; })) { n++; }
      var s = Math.max(lo, 8 * 60);
      rules.push({ id: "r" + n, days: [], start: fmt(s), end: isPlug ? fmt(Math.min(hi, s + 60)) : null, level: null, variables: null });
      render();
    });
    q("#preview").addEventListener("click", function () {
      var bad = check(); if (bad) { say(bad, "err"); return; }
      api("POST", "/v1/schedules/" + doc.id + "/preview", { rules: rules }).then(function (r) {
        var box = q("#preview-box"), list = q("#preview-list");
        if (!r.ok) { say(problems(r.json && r.json.error), "err"); box.hidden = true; return; }
        if (!r.json.valid) { say(r.json.problems.map(function (p) { return (I.E[p.code] || "") + " " + p.message; }).join(" "), "err"); box.hidden = true; return; }
        say("");
        list.textContent = "";
        r.json.next_7_days.forEach(function (e) {
          var li = document.createElement("li");
          var day = I.S.dow[(new Date(e.at.slice(0, 10) + "T12:00:00Z").getUTCDay() + 6) % 7];
          li.textContent = day + " " + e.at.slice(8, 10) + "." + e.at.slice(5, 7) + ", " + hhmm(e.at) + " — " + T("hh_a_" + e.action);
          list.appendChild(li);
        });
        box.hidden = false;
      });
    });
    function saved(r) {
      etag = r.headers.get("ETag") || etag;
      rules = r.json.rules;
      render();
      say(T("hh_saved"), "");
    }
    q("#save").addEventListener("click", function () {
      var bad = check(); if (bad) { say(bad, "err"); return; }
      api("PUT", "/v1/schedules/" + doc.id, { rules: rules }, { "If-Match": etag }).then(function (r) {
        if (!r.ok) { say(problems(r.json && r.json.error), "err"); return; }
        saved(r);
      });
    });
    q("#undo").addEventListener("click", function () {
      api("POST", "/v1/schedules/" + doc.id + "/undo").then(function (r) {
        if (!r.ok) { say(problems(r.json && r.json.error), "err"); return; }
        saved(r);
      });
    });
    render();
  }

  document.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-action]");
    if (!b || b.disabled) { return; }
    var a = b.dataset.action;
    if (a === "toggle") { onToggle(b); }
    else if (a.indexOf("vac-") === 0) { onVacuum(b); }
    else if (a.indexOf("run-") === 0) { onRun(b); }
    else if (["enrol-code", "setup-link", "toggle-web", "revoke"].indexOf(a) >= 0) { onPeople(b); }
  });

  // Task 30c: the update channel announces a published release on the event stream; the APP (not this page) re-checks and verifies everything itself. The page only tells it that something
  // happened, through a console line the app reads (WebChromeClient.onConsoleMessage): no JavaScript bridge exists and none is added.
  function announceRelease(d) { try { console.log("portal-release:" + parseInt(d.version_code, 10)); } catch (e) { /* nothing */ } }
  var page = document.body.dataset.page;
  if (page === "home") {
    listen({
      release: announceRelease,
      device: function (d) { refreshDevice(d.id); },
      vacuum: function () { clearTimeout(vacReload); vacReload = setTimeout(function () { location.reload(); }, 1500); },   // the buttons depend on the robot state (Pause only while it cleans)
      link: function (d) { var bn = q("#ha-banner"); if (bn) { bn.hidden = d.ha !== "down"; } if (d.ha === "down") { qa("[data-device]").forEach(function (c) { refreshDevice(c.dataset.device); }); } }
    });
  } else if (page === "services") {
    listen({ release: announceRelease, service: function (d) {
      var c = q('[data-service="' + d.id + '"]'); if (!c) { return; }
      var bd = q(".badge", c); bd.className = "badge b-" + d.badge; q(".btext", bd).textContent = T("badge_" + d.badge);
    } });
  } else if (page === "settings") {
    initEditor();
  }
  if (document.body.dataset.app === "yes" && page !== "home" && page !== "services") { listen({ release: announceRelease }); }          // every other page of the app: the same one-line announcement
}());

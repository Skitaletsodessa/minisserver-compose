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
        if (r.status === 401) { location.href = "/login"; return new Promise(function () {}); }
        if (r.status === 204) { return { ok: true, status: 204, json: null, headers: r.headers }; }
        return r.json().then(function (j) { return { ok: r.ok, status: r.status, json: j, headers: r.headers }; }, function () { return { ok: r.ok, status: r.status, json: null, headers: r.headers }; });
      });
  }
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
    card.dataset.state = d.state;
    card.dataset.stale = d.stale ? "yes" : "no";
    var b = q('[data-action="toggle"]', card);
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

  // ── home: vacuums ──────────────────────────────────────────────────────────
  var vacReload = null;
  function onVacuum(btn) {
    var card = btn.closest("[data-vacuum]");
    var id = card.dataset.vacuum;
    var act = btn.dataset.action;
    var path = act === "vac-start" ? "/v1/vacuums/" + id + "/scenarios/" + btn.dataset.scenario + "/start" : "/v1/vacuums/" + id + (act === "vac-pause" ? "/pause" : "/dock");
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

  // ── schedule editor ────────────────────────────────────────────────────────
  function initEditor() {
    var doc = JSON.parse(document.getElementById("doc").textContent);
    var isPlug = doc.kind === "plug";
    var rulesBox = q("#rules");
    var msg = q("#editor-msg");
    var etag = doc.etag;
    var readonly = !!q("#editor[data-readonly]");
    var rules = JSON.parse(JSON.stringify(doc.schedule.rules));
    var g = doc.schedule.guardrails || {};
    var parts = [];
    if (g.max_on_minutes) { parts.push(T("guard_max_on", { m: g.max_on_minutes })); }
    if (g.allowed_from) { parts.push(T("guard_range", { a: g.allowed_from, b: g.allowed_to })); }
    if (g.min_gap_minutes) { parts.push(T("guard_gap", { g: g.min_gap_minutes })); }
    q("#guard").textContent = parts.length ? T("guardrails") + ": " + parts.join("; ") : "";
    function say(text, kind) { msg.hidden = !text; msg.textContent = text || ""; msg.className = "msg" + (kind ? " " + kind : ""); }
    function render() {
      rulesBox.innerHTML = "";
      rules.forEach(function (r, i) {
        var d = document.createElement("div");
        d.className = "rule";
        var days = I.S.dow.map(function (n, k) {
          return '<label><input type="checkbox" data-day="' + (k + 1) + '"' + (r.days.indexOf(k + 1) >= 0 ? " checked" : "") + (readonly ? " disabled" : "") + "> " + n + "</label>";
        }).join("");
        d.innerHTML = '<strong>' + T("rule") + ' ' + (i + 1) + '</strong><div class="days">' + days + '</div><div class="times"><label>' + T("from") +
          '<input type="time" data-f="start" value="' + (r.start || "") + '"' + (readonly ? " disabled" : "") + '></label>' +
          (isPlug ? '<label>' + T("to") + '<input type="time" data-f="end" value="' + (r.end || "") + '"' + (readonly ? " disabled" : "") + "></label>" : "") +
          '</div>' + (readonly ? "" : '<button type="button" class="danger" data-del="' + i + '">×</button>');
        rulesBox.appendChild(d);
      });
    }
    function collect() {
      qa(".rule", rulesBox).forEach(function (el, i) {
        rules[i].days = qa("[data-day]", el).filter(function (c) { return c.checked; }).map(function (c) { return parseInt(c.dataset.day, 10); });
        rules[i].start = q('[data-f="start"]', el).value;
        if (isPlug) { rules[i].end = q('[data-f="end"]', el).value; }
      });
      return rules;
    }
    function problems(e) {
      if (!e) { return T("err_generic", { m: "?" }); }
      var list = (e.details && e.details.problems) || [];
      var head = (I.E && I.E[e.code]) || e.message;
      return head + (list.length ? " " + list.map(function (p) { return ((I.E && I.E[p.code]) || "") + " (" + p.message + ")"; }).join(" ") : "");
    }
    rulesBox.addEventListener("click", function (ev) {
      var del = ev.target.closest("[data-del]");
      if (del) { collect(); rules.splice(parseInt(del.dataset.del, 10), 1); render(); }
    });
    q("#add-rule").addEventListener("click", function () {
      collect();
      var n = 1;
      while (rules.some(function (r) { return r.id === "r" + n; })) { n++; }
      rules.push({ id: "r" + n, days: [1, 2, 3, 4, 5, 6, 7], start: "08:00", end: isPlug ? "09:00" : null, level: null, variables: null });
      render();
    });
    q("#preview").addEventListener("click", function () {
      api("POST", "/v1/schedules/" + doc.id + "/preview", { rules: collect() }).then(function (r) {
        var box = q("#preview-box"), list = q("#preview-list");
        if (!r.ok) { say(problems(r.json && r.json.error), "err"); box.hidden = true; return; }
        if (!r.json.valid) { say(r.json.problems.map(function (p) { return (I.E[p.code] || "") + " " + p.message; }).join(" "), "err"); box.hidden = true; return; }
        say("");
        list.innerHTML = "";
        r.json.next_7_days.forEach(function (e) {
          var li = document.createElement("li");
          li.textContent = e.at.slice(0, 10) + " " + hhmm(e.at) + " — " + e.action + " (" + e.rule + ")";
          list.appendChild(li);
        });
        box.hidden = false;
      });
    });
    q("#save").addEventListener("click", function () {
      api("PUT", "/v1/schedules/" + doc.id, { rules: collect() }, { "If-Match": etag }).then(function (r) {
        if (!r.ok) { say(problems(r.json && r.json.error), "err"); return; }
        etag = r.headers.get("ETag") || etag;
        rules = r.json.rules;
        render();
        say(T("saved"), "");
      });
    });
    q("#undo").addEventListener("click", function () {
      api("POST", "/v1/schedules/" + doc.id + "/undo").then(function (r) {
        if (!r.ok) { say(problems(r.json && r.json.error), "err"); return; }
        etag = r.headers.get("ETag") || etag;
        rules = r.json.rules;
        render();
        say(T("saved"), "");
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
    else if (["enrol-code", "setup-link", "toggle-web", "revoke"].indexOf(a) >= 0) { onPeople(b); }
  });

  var page = document.body.dataset.page;
  if (page === "home") {
    listen({
      device: function (d) { refreshDevice(d.id); },
      vacuum: function () { clearTimeout(vacReload); vacReload = setTimeout(function () { location.reload(); }, 1500); },   // the buttons depend on the robot state (Pause only while it cleans)
      link: function (d) { var bn = q("#ha-banner"); if (bn) { bn.hidden = d.ha !== "down"; } if (d.ha === "down") { qa("[data-device]").forEach(function (c) { refreshDevice(c.dataset.device); }); } }
    });
  } else if (page === "services") {
    listen({ service: function (d) {
      var c = q('[data-service="' + d.id + '"]'); if (!c) { return; }
      var bd = q(".badge", c); bd.className = "badge b-" + d.badge; q(".btext", bd).textContent = T("badge_" + d.badge);
    } });
  } else if (page === "settings") {
    initEditor();
  }
}());

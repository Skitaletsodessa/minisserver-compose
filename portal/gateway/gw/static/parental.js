(function () {
  "use strict";
  // Parental-control pages. Task 29 built them, Task 31 rebuilt the parents' view in plain language (copy deck: docs/portal/ui-copy-ru.md; every string is in gw/strings.py).
  // Everything the page shows from the server is put in with textContent / value, never as HTML.
  var I = { S: {}, E: {} };
  try { I = JSON.parse(document.getElementById("i18n").textContent); } catch (e) { /* keep defaults */ }
  var root = document.getElementById("par-root");
  if (!root) { return; }

  function T(key, vars) {
    var s = (I.S && I.S[key]) || key;
    if (vars) { Object.keys(vars).forEach(function (k) { s = s.split("{" + k + "}").join(vars[k]); }); }
    return s;
  }
  function A(key) { return (I.S && I.S[key]) || []; }
  function errText(e) {
    if (!e) { return T("err_generic", { m: "?" }); }
    var head = (I.E && I.E[e.code]) || e.message || e.code;
    var ps = e.details && e.details.problems;
    if (ps && ps.length) { head += " " + ps.map(function (p) { return ((I.E && I.E[p.code]) || "") + " " + p.message; }).join(" "); }
    return head;
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
      }, function () {
        return { ok: false, status: 0, json: { error: { code: "network", message: T("k_net_fail") } }, headers: new Headers() };
      });
  }
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") { n.textContent = attrs[k]; }
      else if (k === "class") { n.className = attrs[k]; }
      else if (k === "value") { n.value = attrs[k]; }
      else if (k === "checked" || k === "disabled" || k === "hidden" || k === "open") { n[k] = !!attrs[k]; }
      else { n.setAttribute(k, attrs[k]); }
    });
    (kids || []).forEach(function (c) { if (c) { n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); } });
    return n;
  }
  function clear(n) { while (n.firstChild) { n.removeChild(n.firstChild); } }
  function qa(sel, rootNode) { return Array.prototype.slice.call((rootNode || document).querySelectorAll(sel)); }
  function hhmm(iso) { return iso ? iso.slice(11, 16) : ""; }
  function dayhhmm(iso) { return iso ? iso.slice(8, 10) + "." + iso.slice(5, 7) + " " + iso.slice(11, 16) : ""; }
  function say(box, text, kind) { box.hidden = !text; box.textContent = text || ""; box.className = "msg" + (kind ? " " + kind : ""); }
  function section(title, id) { var s = el("section", { "class": "card par-sect", id: id }); s.appendChild(el("h2", { text: title })); return s; }

  var isAdmin = root.dataset.admin === "yes";                    // admin AND a web sign-in (an app session never shows the admin entries)
  var GATED = ["youtube", "roblox"];

  // ── time: the server's clock decides what "today" and "tomorrow" are ────────────────────────────────────────────
  var ST = { kids: [], always: [], nowIso: "", skew: 0, pend: {}, health: null, outOfSyncSince: 0 };
  function setServerNow(iso) { if (iso) { ST.nowIso = iso; ST.skew = Date.parse(iso) - Date.now(); } }
  function nowMs() { return Date.now() + ST.skew; }
  function offMin() { var m = /([+-])(\d\d):(\d\d)$/.exec(ST.nowIso || ""); return m ? (m[1] === "-" ? -1 : 1) * (parseInt(m[2], 10) * 60 + parseInt(m[3], 10)) : 0; }
  function localDate(ms) { return new Date(ms + offMin() * 60000).toISOString().slice(0, 10); }
  function localHour(ms) { return new Date(ms + offMin() * 60000).getUTCHours(); }
  function dowOf(dateStr) { var p = dateStr.split("-"); return (new Date(Date.UTC(+p[0], +p[1] - 1, +p[2])).getUTCDay() + 6) % 7; }      // Monday = 0
  function addDays(dateStr, n) { var p = dateStr.split("-"); return new Date(Date.UTC(+p[0], +p[1] - 1, +p[2] + n)).toISOString().slice(0, 10); }
  function plural(n, f) { var a = Math.abs(n) % 100, b = a % 10; if (a > 10 && a < 20) { return f[2]; } if (b > 1 && b < 5) { return f[1]; } if (b === 1) { return f[0]; } return f[2]; }
  function leftText(sec) {
    var m = Math.max(1, Math.ceil(sec / 60));
    if (m >= 60) { var h = Math.floor(m / 60), r = m % 60; return h + " " + plural(h, A("k_hour")) + (r ? " " + r + " " + plural(r, A("k_min")) : ""); }
    return m + " " + plural(m, A("k_min"));
  }
  function mins(hm) { return hm === "24:00" ? 1440 : parseInt(hm.slice(0, 2), 10) * 60 + parseInt(hm.slice(3), 10); }
  function fmtm(m) { return (m < 600 ? "0" : "") + Math.floor(m / 60) + ":" + (m % 60 < 10 ? "0" : "") + (m % 60); }
  function fmtHours(m) { return m % 60 === 0 ? (m / 60) + " ч" : Math.floor(m / 60) + " ч " + (m % 60) + " мин"; }

  // ── what the tile says ──────────────────────────────────────────────────────────────────────────────────────────
  function kidName(k) {
    if (k.display_name) { return k.display_name; }
    var n = (k.name || "").replace(/[-_]+/g, " ").trim();
    return n ? n.charAt(0).toUpperCase() + n.slice(1) : k.name;
  }
  function names(list) { return list.map(function (t) { return T("k_target_" + t); }).join(" и "); }
  function daysText(days) {
    var d = days.slice().sort(function (a, b) { return a - b; });
    if (d.length === 7) { return T("k_days_all"); }
    if (!d.length) { return T("k_days_none"); }
    var out = [], i = 0, dow = A("dow");
    while (i < d.length) {
      var j = i;
      while (j + 1 < d.length && d[j + 1] === d[j] + 1) { j++; }
      if (j - i >= 2) { out.push(dow[d[i] - 1] + "–" + dow[d[j] - 1]); } else { for (var k = i; k <= j; k++) { out.push(dow[d[k] - 1]); } }
      i = j + 1;
    }
    return out.join(", ");
  }
  function scheduleSummary(rules) {
    return rules.map(function (r) { return daysText(r.days) + ", " + r.start + "–" + r.end; }).join(T("k_sched_sep"));
  }
  function whenText(iso) {
    var d = iso.slice(0, 10), today = localDate(nowMs()), t = hhmm(iso);
    if (d === today) { return T("k_when_today", { t: t }); }
    if (d === addDays(today, 1)) { return T("k_when_tomorrow", { t: t }); }
    return T("k_when_day", { d: A("k_day_in")[dowOf(d)], t: t });
  }
  function nextTargets(kid) {
    var at = kid.status.opens_at;
    if (!at) { return GATED; }
    var dow = dowOf(at.slice(0, 10)) + 1, t = hhmm(at);
    var r = kid.windows.rules.filter(function (x) { return x.start === t && x.days.indexOf(dow) >= 0; })[0];
    var o = ((r && r.opens) || GATED).filter(function (g) { return GATED.indexOf(g) >= 0; });
    return o.length ? o : GATED;
  }
  function liveExceptions(kid) {
    return (kid.status.exceptions || []).filter(function (x) { return Date.parse(x.expires_at) > nowMs(); });
  }
  function statusInfo(kid) {
    var s = kid.status, ex = liveExceptions(kid), big, small;
    var all = ex.filter(function (x) { return x.target === "all"; })[0];
    var openG = (s.open_services || []).filter(function (t) { return GATED.indexOf(t) >= 0; });
    var left = s.open_until ? (Date.parse(s.open_until) - nowMs()) / 1000 : 0;
    var today = localDate(nowMs());
    var sched = kid.windows.rules.length ? T("k_l2_sched", { s: scheduleSummary(kid.windows.rules) }) : T("k_l2_nosched");
    var kind = "closed";
    if (all) {
      big = T("k_s_all", { t: hhmm(all.expires_at) }); kind = "all"; small = sched;
    } else if (s.state === "open" && s.open_until) {
      big = T(left <= 5400 ? "k_s_open_pl_left" : "k_s_open_pl_until", { t: hhmm(s.open_until), left: leftText(left) }); kind = "open"; small = sched;
    } else if (s.state === "restricted" && s.open_until) {
      big = T(left <= 5400 ? "k_s_open_one_left" : "k_s_open_one_until", { x: names(openG), t: hhmm(s.open_until), left: leftText(left) }); kind = "part"; small = sched;
    } else if (dowOf(today) === 6) {
      big = T("k_s_sunday");
      small = s.opens_at ? T("k_l2_opens", { when: whenText(s.opens_at) }) : sched;
    } else if (s.opens_at) {
      var tg = nextTargets(kid);
      big = T(tg.length === 1 ? "k_s_closed_one" : "k_s_closed_pl", { x: names(tg), when: whenText(s.opens_at) }); small = sched;
    } else {
      big = T("k_s_closed_none"); small = sched;
    }
    return { big: big, small: small, kind: kind };
  }
  function iconFor(k) { var i = k.icon || k.device_kind || "other"; return { phone: "dev_phone", tablet: "dev_tablet", laptop: "dev_laptop", console: "dev_console", tv: "dev_tv" }[i] || "dev_other"; }
  function svgIcon(id) {
    var ns = "http://www.w3.org/2000/svg", s = document.createElementNS(ns, "svg"), u = document.createElementNS(ns, "use");
    s.setAttribute("class", "ic kid-ic"); s.setAttribute("aria-hidden", "true");
    u.setAttributeNS("http://www.w3.org/1999/xlink", "href", "/static/icons.svg#" + id); u.setAttribute("href", "/static/icons.svg#" + id);
    s.appendChild(u);
    return s;
  }
  function kidById(id) { return ST.kids.filter(function (k) { return k.id === id; })[0]; }

  // ── the children list ───────────────────────────────────────────────────────────────────────────────────────────
  function loadKids() {
    return api("GET", "/v1/parental/children").then(function (r) {
      if (!r.ok) { return r; }
      ST.kids = r.json.children; ST.always = r.json.always_open || []; setServerNow(r.json.now);
      return r;
    });
  }
  function alwaysLine() {
    var items = ST.always.map(function (g) { return T("k_always_" + g); });
    return items.length ? T("k_always", { list: items.join(", ") }) : "";
  }
  function fillHead(node, kid, linked) {
    clear(node);
    var info = statusInfo(kid);
    node.className = (linked ? "kid-link" : "kid-head") + " s-" + info.kind;
    node.appendChild(svgIcon(iconFor(kid)));
    node.appendChild(el("span", { "class": "kid-text" }, [el(linked ? "span" : "h2", { "class": "kid-name", text: kidName(kid) }), el("span", { "class": "kid-big", text: info.big }), el("span", { "class": "kid-small", text: info.small })]));
  }
  function fillActions(node, kid) {
    clear(node);
    var groups = [];                                                       // exceptions that end in the same minute are ONE chip ("YouTube и Roblox · ещё 30 минут")
    liveExceptions(kid).forEach(function (x) {
      var g = groups.filter(function (q) { return q.key === x.expires_at.slice(0, 16); })[0];
      if (!g) { g = { key: x.expires_at.slice(0, 16), at: x.expires_at, items: [] }; groups.push(g); }
      g.items.push(x);
    });
    groups.forEach(function (g) {
      var what = g.items.slice().sort(function (a, b) { return ["youtube", "roblox", "all"].indexOf(a.target) - ["youtube", "roblox", "all"].indexOf(b.target); }).map(function (x) { return T("k_target_" + x.target); }).join(" и "), left = leftText((Date.parse(g.at) - nowMs()) / 1000);
      node.appendChild(el("span", { "class": "chip" }, [el("span", { text: T("k_chip", { what: what, left: left }) }),
        kid.can && kid.can.grant_exceptions ? el("button", { type: "button", "data-close-x": g.items.map(function (x) { return x.id; }).join(","), "data-kid": kid.id, "aria-label": T("k_chip_close_aria", { what: what }), text: T("k_chip_close") }) : null]));
    });
    var p = ST.pend[kid.id];
    if (p) {
      var line = el("p", { "class": "kid-pend " + p.kind, role: "status" }, [p.text, p.retry ? " " : null]);
      if (p.retry) { line.appendChild(el("button", { type: "button", "data-retry": kid.id, text: T("k_retry") })); }
      node.appendChild(line);
    }
    if (kid.can && kid.can.grant_exceptions) { node.appendChild(el("button", { type: "button", "class": "primary kid-open", "data-open-sheet": kid.id, text: T("k_open_access") })); }
  }
  function refreshStatus() {
    qa("[data-head-for]").forEach(function (n) { var k = kidById(n.dataset.headFor); if (k) { fillHead(n, k, n.dataset.linked === "1"); } });
    qa("[data-actions-for]").forEach(function (n) { var k = kidById(n.dataset.actionsFor); if (k) { fillActions(n, k); } });
    showAttention();
  }
  function tile(kid) {
    var head = el("a", { href: "#" + kid.id, "data-head-for": kid.id, "data-linked": "1" });
    var acts = el("div", { "class": "kid-actions", "data-actions-for": kid.id });
    var card = el("section", { "class": "card kid", "data-child": kid.id }, [head, acts]);
    fillHead(head, kid, true); fillActions(acts, kid);
    return card;
  }
  function setPend(id, p) { if (p) { ST.pend[id] = p; } else { delete ST.pend[id]; } refreshStatus(); }

  // the banner: at most one, only when something is wrong
  function loadHealth() {
    return api("GET", "/v1/parental/health").then(function (r) { ST.health = r.ok ? r.json : null; showAttention(); });
  }
  function showAttention() {
    var b = document.getElementById("par-attn");
    if (!b) { return; }
    var h = ST.health, text = "";
    if (h) {
      if (!h.policy.in_sync) { if (!ST.outOfSyncSince) { ST.outOfSyncSince = Date.now(); } } else { ST.outOfSyncSince = 0; }
      if (!h.agh.reachable) { text = T("k_attn_agh"); }
      else if (ST.outOfSyncSince && Date.now() - ST.outOfSyncSince > 120000) { text = T("k_attn_sync"); }
    }
    if (!text) {
      var late = ST.kids.filter(function (k) { return k.status.queries_today === 0 && localHour(nowMs()) >= 14; })[0];
      if (late) { text = T("k_attn_device", { name: kidName(late) }); }
    }
    if (!text && h && h.replica && h.replica.parity === "mismatch") { text = T("k_attn_replica"); }
    b.hidden = !text; b.textContent = text;
  }

  // ── the "open access" sheet ─────────────────────────────────────────────────────────────────────────────────────
  var sheet = null;
  function closeSheet() { if (sheet) { sheet.back.remove(); document.removeEventListener("keydown", sheet.onkey); var f = sheet.from; sheet = null; if (f && f.focus) { f.focus(); } } }
  function openSheet(kid, from) {
    closeSheet();
    var chosen = {};                                              // youtube / roblox / all
    var tiles = {}, durs = [];
    var hint = el("p", { "class": "meta sheet-hint", text: T("k_sheet_hint") });
    function choose(t) {
      if (t === "all") { chosen = chosen.all ? {} : { all: true }; }
      else { delete chosen.all; if (chosen[t]) { delete chosen[t]; } else { chosen[t] = true; } }
      Object.keys(tiles).forEach(function (k) { tiles[k].setAttribute("aria-pressed", chosen[k] ? "true" : "false"); });
      var any = Object.keys(chosen).length > 0;
      durs.forEach(function (d) { d.disabled = !any; });
      hint.hidden = any;
      var es = box.querySelector("[data-eod-sub]"); if (es) { es.textContent = T("k_d_eod_sub", { t: chosen.all ? eodAll : eodNorm }); }
    }
    function tileBtn(t, sub) {
      var b = el("button", { type: "button", "class": "choice" + (t === "all" ? " choice-all" : ""), "aria-pressed": "false", "data-what": t }, [el("span", { "class": "choice-name", text: T("k_target_" + t) }), sub ? el("span", { "class": "choice-sub", text: sub }) : null]);
      tiles[t] = b; b.addEventListener("click", function () { choose(t); });
      return b;
    }
    var eodAll = kid.eod ? kid.eod.all : "23:59", eodNorm = kid.eod ? kid.eod.normal : "23:59";
    var grid1 = el("div", { "class": "choices" }, [tileBtn("youtube"), tileBtn("roblox"), tileBtn("all", T("k_all_note"))]);
    var grid2 = el("div", { "class": "durs" });
    [["30", T("k_d_30"), 30], ["60", T("k_d_60"), 60], ["120", T("k_d_120"), 120], ["eod", T("k_d_eod"), 0]].forEach(function (d) {
      var sub = d[0] === "eod" ? T("k_d_eod_sub", { t: eodNorm }) : null;
      var b = el("button", { type: "button", "class": "dur", disabled: true, "data-dur": d[0] }, [el("span", { "class": "dur-name", text: d[1] }), sub ? el("span", { "class": "dur-sub", text: sub, "data-eod-sub": "1" }) : null]);
      b.addEventListener("click", function () { var targets = Object.keys(chosen); closeSheet(); grant(kid, targets, d[2] ? { minutes: d[2] } : { until_end_of_day: true }); });
      durs.push(b); grid2.appendChild(b);
    });
    var title = el("h2", { id: "sheet-title", text: T("k_sheet_title") });
    var close = el("button", { type: "button", "class": "sheet-x", "aria-label": T("k_sheet_close"), text: "×" });
    var box = el("div", { "class": "sheet", role: "dialog", "aria-modal": "true", "aria-labelledby": "sheet-title" }, [
      el("div", { "class": "sheet-top" }, [title, close]), el("p", { "class": "meta", text: T("k_sheet_for", { name: kidName(kid) }) }),
      el("h3", { text: T("k_sheet_what") }), grid1, el("h3", { text: T("k_sheet_how_long") }), grid2, hint]);
    var back = el("div", { "class": "sheet-back" }, [box]);
    function onkey(ev) {
      if (ev.key === "Escape") { closeSheet(); return; }
      if (ev.key === "Tab") {
        var f = qa("button:not([disabled])", box); if (!f.length) { return; }
        if (ev.shiftKey && document.activeElement === f[0]) { ev.preventDefault(); f[f.length - 1].focus(); }
        else if (!ev.shiftKey && document.activeElement === f[f.length - 1]) { ev.preventDefault(); f[0].focus(); }
      }
    }
    close.addEventListener("click", closeSheet);
    back.addEventListener("click", function (ev) { if (ev.target === back) { closeSheet(); } });
    document.addEventListener("keydown", onkey);
    document.body.appendChild(back);
    sheet = { back: back, onkey: onkey, from: from };
    grid1.querySelector("button").focus();
  }

  // ── the snackbar: 10 s to undo ──────────────────────────────────────────────────────────────────────────────────
  var snackTimer = null;
  function snack(text, undo) {
    var s = document.getElementById("snack");
    if (!s) { s = el("div", { id: "snack", role: "status", "aria-live": "polite", hidden: true }, [el("span", { "class": "snack-text" }), el("button", { type: "button", "class": "snack-undo", text: T("k_snack_undo") })]); document.body.appendChild(s); }
    s.querySelector(".snack-text").textContent = text;
    var ub = s.querySelector(".snack-undo");
    var nb = ub.cloneNode(true); ub.parentNode.replaceChild(nb, ub);
    nb.hidden = !undo;
    s.hidden = false;
    clearTimeout(snackTimer);
    function hide() { s.hidden = true; }
    if (undo) { nb.addEventListener("click", function () { clearTimeout(snackTimer); hide(); undo(); }); }
    snackTimer = setTimeout(hide, 10000);
  }

  // ── granting and closing access, with an honest read-back ───────────────────────────────────────────────────────
  var pollGen = 0;
  function confirmKid(id, test, doneText, retryFn) {
    var gen = ++pollGen, tries = 0;
    (function again() {
      if (gen !== pollGen) { return; }
      loadKids().then(function (r) {
        if (gen !== pollGen) { return; }
        var k = r.ok ? kidById(id) : null;
        if (k && k.status.policy_ok && test(k)) { setPend(id, { kind: "ok", text: doneText(k) }); setTimeout(function () { if (ST.pend[id] && ST.pend[id].kind === "ok") { setPend(id, null); } }, 20000); return; }
        if (k) { refreshStatus(); }
        if (++tries >= 18) { setPend(id, { kind: "err", text: T("k_unconfirmed"), retry: retryFn }); return; }
        setTimeout(again, 5000);
      });
    }());
  }
  function grant(kid, targets, spec) {
    var id = kid.id;
    setPend(id, { kind: "busy", text: T("k_applying") });
    var made = [], i = 0;
    function fail(r) { setPend(id, { kind: "err", text: T("k_fail", { why: errText(r.json && r.json.error) }), retry: function () { grant(kid, targets, spec); } }); }
    (function next() {
      if (i >= targets.length) { done(); return; }
      var body = { target: targets[i] }; Object.keys(spec).forEach(function (k) { body[k] = spec[k]; });
      api("POST", "/v1/parental/children/" + id + "/exceptions", body).then(function (r) {
        if (!r.ok) { fail(r); return; }
        made.push(r.json); i++; next();
      });
    }());
    function done() {
      var until = hhmm(made[0].expires_at), what = names(targets);
      snack(T("k_snack_done", { what: what, t: until }), function () { undo(); });
      confirmKid(id, function (k) { return made.every(function (m) { return liveExceptions(k).some(function (x) { return x.id === m.id; }); }); },
                 function () { return T(targets.indexOf("all") >= 0 ? "k_applied_all" : "k_applied_until", { t: until }); }, function () { setPend(id, { kind: "busy", text: T("k_applying") }); confirmKid(id, function () { return true; }, function () { return T("k_applied_until", { t: until }); }); });
      function undo() {
        setPend(id, { kind: "busy", text: T("k_applying") });
        var j = 0;
        (function del() {
          if (j >= made.length) {
            snack(T("k_snack_undone"));
            confirmKid(id, function (k) { return !made.some(function (m) { return liveExceptions(k).some(function (x) { return x.id === m.id; }); }); }, function () { return T("k_applied_closed"); }, function () { setPend(id, null); });
            return;
          }
          api("DELETE", "/v1/parental/children/" + id + "/exceptions/" + made[j].id).then(function (r) {
            if (!r.ok && r.status !== 404) { fail(r); return; }
            j++; del();
          });
        }());
      }
    }
  }
  function closeException(kidId, xids) {
    setPend(kidId, { kind: "busy", text: T("k_applying") });
    var ids = String(xids).split(","), j = 0;
    (function del() {
      if (j >= ids.length) {
        confirmKid(kidId, function (k) { return !liveExceptions(k).some(function (x) { return ids.indexOf(x.id) >= 0; }); }, function () { return T("k_applied_closed"); }, function () { setPend(kidId, null); });
        return;
      }
      api("DELETE", "/v1/parental/children/" + kidId + "/exceptions/" + ids[j]).then(function (r) {
        if (!r.ok && r.status !== 404) { setPend(kidId, { kind: "err", text: T("k_fail", { why: errText(r.json && r.json.error) }), retry: function () { closeException(kidId, xids); } }); return; }
        j++; del();
      });
    }());
  }

  // ── the list page ───────────────────────────────────────────────────────────────────────────────────────────────
  var tickTimer = null, pollTimer = null;
  function stopTimers() { clearInterval(tickTimer); clearInterval(pollTimer); tickTimer = pollTimer = null; }
  function showChildren() {
    stopTimers(); clear(root);
    root.appendChild(el("p", { "class": "meta", text: T("k_loading") }));
    Promise.all([loadKids(), loadHealth()]).then(function (rs) {
      clear(root);
      if (!rs[0].ok) { root.appendChild(el("p", { "class": "msg err", text: errText(rs[0].json && rs[0].json.error) })); return; }
      if (!ST.kids.length) { root.appendChild(el("p", { "class": "meta", text: T("k_none") })); }
      var grid = el("div", { "class": "grid kids" });
      ST.kids.forEach(function (k) { grid.appendChild(tile(k)); });
      root.appendChild(grid);
      var al = alwaysLine();
      if (al) { root.appendChild(el("p", { "class": "meta always", text: al })); }
      root.appendChild(el("p", { "class": "meta bedtime", text: T("k_bedtime") }));
      if (isAdmin) { root.appendChild(adminMenu(ST.kids)); }
      tickTimer = setInterval(refreshStatus, 30000);                       // the sentences count down by themselves
      pollTimer = setInterval(function () { if (!document.hidden) { loadKids().then(function () { refreshStatus(); }); loadHealth(); } }, 60000);
    });
  }

  // ── one child ───────────────────────────────────────────────────────────────────────────────────────────────────
  var cur = null;                                         // {id, d (detail), etag, rules}
  function base0() { return "/v1/parental/children/" + cur.id; }
  function detailsBlock(title, cls, onOpen) {
    var body = el("div", { "class": "kd-body" });
    var d = el("details", { "class": "card kd " + (cls || "") }, [el("summary", { text: title }), body]);
    var loaded = false;
    d.addEventListener("toggle", function () { if (d.open && !loaded) { loaded = true; onOpen(body); } });
    return { node: d, body: body };
  }

  function showChild(id, sub) {
    stopTimers(); clear(root);
    root.appendChild(el("p", { "class": "meta", text: T("k_loading") }));
    Promise.all([loadKids(), api("GET", "/v1/parental/children/" + id), api("GET", "/v1/parental/children/" + id + "/windows"), loadHealth()]).then(function (rs) {
      clear(root);
      if (!rs[1].ok) { root.appendChild(el("p", { "class": "msg err", text: errText(rs[1].json && rs[1].json.error) })); root.appendChild(el("p", {}, [el("a", { "class": "back", href: "#", text: T("k_back") })])); return; }
      var d = rs[1].json;
      setServerNow(d.now);
      cur = { id: id, d: d, etag: rs[2].headers.get("ETag"), rules: JSON.parse(JSON.stringify(d.windows.rules)) };
      if (!kidById(id)) { ST.kids.push(d); }
      if (sub === "schedule") { showSchedule(); return; }
      root.appendChild(el("p", {}, [el("a", { "class": "back", href: "#", text: T("k_back") })]));
      var k = kidById(id);
      var head = el("div", { "data-head-for": id, "data-linked": "0" });
      var acts = el("div", { "class": "kid-actions", "data-actions-for": id });
      root.appendChild(el("section", { "class": "card kid kid-page" }, [head, acts]));
      fillHead(head, k, false); fillActions(acts, k);
      root.appendChild(scheduleCard());
      root.appendChild(seenBlock().node);
      root.appendChild(sitesBlock().node);
      root.appendChild(techBlock().node);
      var al = alwaysLine();
      if (al) { root.appendChild(el("p", { "class": "meta always", text: al })); }
      if (isAdmin) { root.appendChild(moreBlock()); }
      tickTimer = setInterval(refreshStatus, 30000);
      pollTimer = setInterval(function () { if (!document.hidden) { loadKids().then(function () { refreshStatus(); }); loadHealth(); } }, 60000);
    });
  }

  function scheduleCard() {
    var d = cur.d, rules = d.windows.rules;
    var box = el("section", { "class": "card par-sect", id: "par-sched" });
    box.appendChild(el("h2", { text: T("k_c_sched") }));
    box.appendChild(el("p", { "class": "sched-line", text: rules.length ? scheduleSummary(rules) : T("k_l2_nosched") }));
    box.appendChild(el("a", { "class": "linkbtn", href: "#" + cur.id + "/schedule", text: d.windows.can_edit ? T("k_c_sched_edit") : T("k_c_sched_view") }));
    return box;
  }

  // "what they opened": on demand, collapsed, nothing stored
  function seenBlock() {
    var d = cur.d;
    return detailsBlock(T("k_c_seen"), "kd-seen", function (body) {
      body.appendChild(el("p", { "class": "meta", text: T("k_c_seen_note") }));
      var range = el("select", { "aria-label": T("k_c_seen") }, [el("option", { value: "24h", text: T("k_c_range_day") }), el("option", { value: "7d", text: T("k_c_range_week") })]);
      var onlyB = el("input", { type: "checkbox" });
      var search = el("input", { type: "search", placeholder: T("k_c_search"), maxlength: "80", "aria-label": T("k_c_search") });
      body.appendChild(el("div", { "class": "filters" }, [range, el("label", { "class": "inline" }, [onlyB, " " + T("k_c_only_blocked")]), search]));
      var list = el("ul", { "class": "seen" });
      var more = el("button", { type: "button", hidden: true, text: T("k_c_more") });
      var msg = el("p", { role: "status", hidden: true });
      body.appendChild(list); body.appendChild(more); body.appendChild(msg);
      var cursor = null;
      function row(r) {
        var flags = [];
        if (r.in_allow) { flags.push(T("k_c_in_allow")); }
        if (r.in_deny) { flags.push(T("k_c_in_deny")); }
        if (r.listed_by) { flags.push(T("k_c_in_list", { l: r.listed_by })); }
        if (r.threat) { flags.push(T("k_c_threat")); }
        var a = el("button", { type: "button", "data-allow": r.name, disabled: !d.can.edit_domains || r.in_allow, text: T("k_c_allow") });
        var n = el("button", { type: "button", "class": "danger", "data-deny": r.name, disabled: !d.can.edit_domains || r.in_deny, text: T("k_c_deny") });
        return el("li", { "class": "seen-row" }, [el("strong", { text: r.name }), el("span", { "class": "meta", text: T("k_c_row", { n: r.allowed, m: r.blocked }) + " · " + T("k_c_last", { t: dayhhmm(r.last) }) + (flags.length ? " · " + flags.join(", ") : "") }),
                                                  el("span", { "class": "row" }, [a, n])]);
      }
      function load(append) {
        var qs = "?range=" + range.value + (onlyB.checked ? "&blocked=1" : "") + (search.value.trim() ? "&q=" + encodeURIComponent(search.value.trim()) : "") + (append && cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
        api("GET", base0() + "/activity" + qs).then(function (r) {
          if (!r.ok) { say(msg, errText(r.json && r.json.error), "err"); return; }
          say(msg, r.json.truncated ? T("k_c_truncated") : "");
          if (!append) { clear(list); }
          r.json.rows.forEach(function (x) { list.appendChild(row(x)); });
          if (!list.children.length) { list.appendChild(el("li", { "class": "meta", text: T("k_c_none") })); }
          cursor = r.json.next_cursor; more.hidden = !cursor;
        });
      }
      range.addEventListener("change", function () { cursor = null; load(false); });
      onlyB.addEventListener("change", function () { cursor = null; load(false); });
      var t = null;
      search.addEventListener("input", function () { clearTimeout(t); t = setTimeout(function () { cursor = null; load(false); }, 400); });
      more.addEventListener("click", function () { load(true); });
      list.addEventListener("click", function (ev) {
        var a = ev.target.closest("button[data-allow]"), dn = ev.target.closest("button[data-deny]"), b = a || dn;
        if (!b || b.disabled) { return; }
        b.disabled = true;
        api("POST", base0() + "/domains", { kind: a ? "allow" : "deny", domain: a ? a.dataset.allow : dn.dataset.deny }).then(function (r) {
          if (!r.ok) { b.disabled = false; say(msg, errText(r.json && r.json.error), "err"); return; }
          cursor = null; load(false);
        });
      });
      load(false);
    });
  }

  // per-child lists
  function sitesBlock() {
    var d = cur.d;
    return detailsBlock(T("k_c_sites"), "kd-sites", function (body) {
      var msg = el("p", { role: "status", hidden: true });
      var cols = el("div", { "class": "grid" });
      function col(kind) {
        var c = el("div", {}, [el("h3", { text: T(kind === "allow" ? "k_c_sites_allow" : "k_c_sites_deny") })]);
        c.appendChild(el("ul", { "data-kind": kind, "class": "sites" }));
        var inp = el("input", { type: "text", placeholder: T("k_c_sites_ph"), "data-new": kind, disabled: !d.can.edit_domains, maxlength: "253", "aria-label": T(kind === "allow" ? "k_c_sites_allow" : "k_c_sites_deny") });
        c.appendChild(el("div", { "class": "row" }, [inp, el("button", { type: "button", "data-add": kind, disabled: !d.can.edit_domains, text: T("k_c_sites_add") })]));
        return c;
      }
      cols.appendChild(col("allow")); cols.appendChild(col("deny"));
      body.appendChild(cols);
      body.appendChild(el("p", {}, [el("button", { type: "button", "data-dom-undo": "1", disabled: !d.can.edit_domains, text: T("k_c_sites_undo") })]));
      body.appendChild(msg);
      if (!d.can.edit_domains) { body.appendChild(el("p", { "class": "meta", text: T("k_c_no_edit") })); }
      function draw(lists) {
        d.domains = lists;
        ["allow", "deny"].forEach(function (k) {
          var ul = body.querySelector('ul[data-kind="' + k + '"]');
          clear(ul);
          if (!lists[k].length) { ul.appendChild(el("li", { "class": "meta", text: T("k_c_sites_empty") })); }
          lists[k].forEach(function (dom) { ul.appendChild(el("li", {}, [el("span", { text: dom }), el("button", { type: "button", "data-rm": dom, "data-rm-kind": k, disabled: !d.can.edit_domains, text: T("k_c_sites_remove") })])); });
        });
      }
      draw(d.domains);
      function result(r) { if (!r.ok) { say(msg, errText(r.json && r.json.error), "err"); return; } say(msg, ""); draw(r.json); }
      body.addEventListener("click", function (ev) {
        var a = ev.target.closest("button[data-add]"), rm = ev.target.closest("button[data-rm]"), un = ev.target.closest("button[data-dom-undo]");
        var b = base0() + "/domains";
        if (a && !a.disabled) {
          var inp = body.querySelector('input[data-new="' + a.dataset.add + '"]');
          if (!inp.value.trim()) { return; }
          api("POST", b, { kind: a.dataset.add, domain: inp.value.trim() }).then(function (r) { if (r.ok) { inp.value = ""; } result(r); });
        } else if (rm && !rm.disabled) { api("DELETE", b + "?kind=" + encodeURIComponent(rm.dataset.rmKind) + "&domain=" + encodeURIComponent(rm.dataset.rm)).then(result); }
        else if (un && !un.disabled) { api("POST", b + "/undo").then(result); }
      });
    });
  }

  // the technical truth, collapsed
  function techBlock() {
    var d = cur.d;
    return detailsBlock(T("kd_title"), "kd-tech", function (body) {
      var s = d.status, i = d.identity;
      body.appendChild(el("p", { "class": s.policy_ok ? "meta" : "meta bad", text: T(s.policy_ok ? "kd_rules_ok" : "kd_rules_bad") }));
      body.appendChild(el("p", { "class": "meta", text: T(s.dns_seen ? "kd_dns_yes" : "kd_dns_no") }));
      if (s.queries_today !== null && s.queries_today !== undefined) { body.appendChild(el("p", { "class": "meta", text: T("kd_counts", { q: s.queries_today, b: s.blocked_today }) + (s.last_query_at ? " · " + T("kd_last", { t: hhmm(s.last_query_at) }) : "") })); }
      body.appendChild(el("p", { "class": "meta", text: T("kd_ident", { ip: i.ip, mac: (i.mac || "—") + (i.randomised_mac ? " (" + T("par_random_mac") + ")" : ""), v: i.vendor || "—", h: i.hostname || "—" }) }));
      if (d.can.grant_exceptions) { body.appendChild(checklistBox(cur.id, T("kd_checklist"))); }
    });
  }

  // admin, web sign-in only: rename and remove
  function moreBlock() {
    var d = cur.d;
    var det = detailsBlock(T("k_more"), "kd-more", function (body) {
      var out = el("p", { role: "status", hidden: true });
      var inp = el("input", { type: "text", maxlength: "40", placeholder: T("k_rename_ph"), value: d.display_name || "" });
      body.appendChild(el("h3", { text: T("k_rename_title") }));
      body.appendChild(el("label", {}, [T("k_rename_label"), inp]));
      body.appendChild(el("button", { type: "button", "data-rename-go": "1", text: T("k_rename_save") }));
      body.appendChild(out);
      body.addEventListener("click", function (ev) {
        var b = ev.target.closest("button[data-rename-go]");
        if (!b || b.disabled) { return; }
        b.disabled = true;
        api("PATCH", base0(), { display_name: inp.value.trim() }).then(function (r) {
          b.disabled = false;
          if (!r.ok) { say(out, errText(r.json && r.json.error), "err"); return; }
          d.display_name = r.json.display_name;
          var k = kidById(cur.id); if (k) { k.display_name = r.json.display_name; }
          say(out, T("k_rename_done")); refreshStatus();
        });
      });
      body.appendChild(removeBox());
    });
    return det.node;
  }

  // ── the schedule of one child, in plain language (Task 31 Part D) ────────────────────────────────────────────────
  // One card per window: day chips, "opens at" / "closes at" in 15-minute steps inside the allowed hours, a stepper for the length ("not more than N minutes a day"), and a
  // strip that shows the week. The controls cannot produce what the guardrails refuse; the service still checks everything and its refusal is explained in words.
  function showSchedule() {
    var d = cur.d, g = d.windows.guardrails || {}, can = d.windows.can_edit;
    var lo = mins(g.allowed_from || "08:00"), hi = mins(g.allowed_to || "21:00"), maxDay = g.max_open_minutes_per_day || (hi - lo);
    var STEP = 15;
    clear(root);
    root.appendChild(el("p", {}, [el("a", { "class": "back", href: "#" + cur.id, text: T("sch_back") })]));
    root.appendChild(el("h2", { "class": "sect", text: T("sch_title") }));
    root.appendChild(el("p", { "class": "meta", text: kidName(d) }));
    if (!can) { root.appendChild(el("p", { "class": "msg warn", text: T("sch_no_edit") })); }
    root.appendChild(el("p", { "class": "meta", text: T("sch_hours_limit", { a: fmtm(lo), b: fmtm(hi), m: maxDay + " " + plural(maxDay, A("k_min")) }) }));
    var rulesBox = el("div", { id: "sch-rules" });
    var weekBox = el("section", { "class": "card", id: "sch-week" });
    var msg = el("p", { id: "sch-msg", role: "status", hidden: true });
    var rules = cur.rules;
    var serial = 1;

    function optionsFor(from, to, current) {
      var out = [];
      for (var m = from; m <= to; m += STEP) { out.push(m); }
      if (current !== undefined && out.indexOf(current) < 0) { out.push(current); out.sort(function (a, b) { return a - b; }); }
      return out;
    }
    function timeSelect(label, values, selected, onChange) {
      var sel = el("select", { "aria-label": label }, values.map(function (m) { return el("option", { value: String(m), text: fmtm(m), }); }));
      sel.value = String(selected);
      sel.disabled = !can;
      sel.addEventListener("change", function () { onChange(parseInt(sel.value, 10)); });
      return el("label", {}, [label, sel]);
    }
    function drawRules() {
      clear(rulesBox);
      rules.forEach(function (r, i) {
        var s = mins(r.start), e = mins(r.end);
        var chips = el("div", { "class": "daychips", role: "group", "aria-label": T("sch_days") }, A("dow").map(function (n, k) {
          var on = r.days.indexOf(k + 1) >= 0;
          var b = el("button", { type: "button", "class": "daychip", "aria-pressed": on ? "true" : "false", disabled: !can, text: n });
          b.addEventListener("click", function () {
            var j = r.days.indexOf(k + 1);
            if (j >= 0) { r.days.splice(j, 1); } else { r.days.push(k + 1); r.days.sort(function (a, c) { return a - c; }); }
            drawRules();
          });
          return b;
        }));
        var starts = timeSelect(T("sch_from"), optionsFor(lo, hi - STEP, s), s, function (v) {
          var delta = v - s; var ne = Math.min(hi, e + delta); if (ne <= v) { ne = v + STEP; }
          r.start = fmtm(v); r.end = fmtm(ne); drawRules();
        });
        var ends = timeSelect(T("sch_to"), optionsFor(s + STEP, hi, e), e, function (v) { r.end = fmtm(v); drawRules(); });
        var len = e - s, maxLen = Math.min(maxDay, hi - s);
        var minus = el("button", { type: "button", "class": "step", "aria-label": T("sch_cap_less"), disabled: !can || len <= STEP, text: "−" });
        var plus = el("button", { type: "button", "class": "step", "aria-label": T("sch_cap_more"), disabled: !can || len >= maxLen, text: "+" });
        minus.addEventListener("click", function () { r.end = fmtm(Math.max(s + STEP, e - STEP)); drawRules(); });
        plus.addEventListener("click", function () { r.end = fmtm(Math.min(s + maxLen, e + STEP)); drawRules(); });
        var cap = el("div", { "class": "cap" }, [minus, el("span", { "class": "cap-text", text: T("sch_cap", { n: len + " " + plural(len, A("k_min")) }) }), plus]);
        var kids = [];
        if (rules.length > 1) { kids.push(el("strong", { text: T("sch_window_n", { n: i + 1 }) })); }
        kids.push(chips, el("div", { "class": "times" }, [starts, ends]), cap);
        var both = r.opens.length === 2;
        if (!both) {
          kids.push(el("div", { "class": "days" }, [el("span", { text: T("sch_opens") + ":" })].concat(GATED.map(function (t) {
            var b = el("button", { type: "button", "class": "daychip wide", "aria-pressed": r.opens.indexOf(t) >= 0 ? "true" : "false", disabled: !can, text: T("k_target_" + t) });
            b.addEventListener("click", function () { var j = r.opens.indexOf(t); if (j >= 0) { r.opens.splice(j, 1); } else { r.opens.push(t); } drawRules(); });
            return b;
          }))));
        }
        if (rules.length > 1 && can) {
          var rm = el("button", { type: "button", "class": "danger", text: T("sch_remove") });
          rm.addEventListener("click", function () { rules.splice(i, 1); drawRules(); });
          kids.push(rm);
        }
        rulesBox.appendChild(el("div", { "class": "rule sch-rule", "data-i": String(i) }, kids));
      });
      drawWeek();
    }
    function drawWeek() {
      clear(weekBox);
      weekBox.appendChild(el("h3", { text: T("sch_week") }));
      var strip = el("div", { "class": "weekstrip" });
      A("dow").forEach(function (n, k) {
        var segs = rules.filter(function (r) { return r.days.indexOf(k + 1) >= 0; }).map(function (r) { return [mins(r.start), mins(r.end)]; });
        var tot = segs.reduce(function (a, s) { return a + (s[1] - s[0]); }, 0);
        var track = el("div", { "class": "wk-track" });
        segs.forEach(function (s) {
          var bar = el("div", { "class": "wk-bar" });                       // CSSOM, not a style attribute: the page policy forbids inline styles
          bar.style.top = (100 * (s[0] - lo) / (hi - lo)).toFixed(1) + "%";
          bar.style.height = Math.max(4, 100 * (s[1] - s[0]) / (hi - lo)).toFixed(1) + "%";
          track.appendChild(bar);
        });
        var title = n + ": " + (segs.length ? segs.map(function (s) { return fmtm(s[0]) + "–" + fmtm(s[1]); }).join(", ") : T("sch_week_none"));
        strip.appendChild(el("div", { "class": "wk-day" + (tot ? "" : " off"), role: "img", "aria-label": title, title: title }, [track, el("span", { "class": "wk-n", text: n }), el("span", { "class": "wk-t", text: tot ? fmtHours(tot) : "—" })]));
      });
      weekBox.appendChild(strip);
      weekBox.appendChild(el("p", { "class": "meta", text: rules.length ? T("sch_total", { n: totalText() }) : "" }));
    }
    function totalText() {
      var worst = 0;
      for (var dday = 1; dday <= 7; dday++) {
        var t = 0; rules.forEach(function (r) { if (r.days.indexOf(dday) >= 0) { t += mins(r.end) - mins(r.start); } });
        worst = Math.max(worst, t);
      }
      return worst + " " + plural(worst, A("k_min"));
    }

    function problem() {
      var perDay = {};
      for (var i = 0; i < rules.length; i++) {
        var r = rules[i];
        if (!r.days.length) { return T("sch_err_days"); }
        if (!r.opens.length) { return T("sch_err_opens"); }
        if (mins(r.end) <= mins(r.start)) { return T("sch_err_range"); }
        if (mins(r.start) < lo) { return T("sch_err_from", { from: fmtm(lo) }); }
        if (mins(r.end) > hi) { return T("sch_err_to", { to: fmtm(hi) }); }
        for (var j = i + 1; j < rules.length; j++) {
          var q = rules[j];
          var share = q.opens.some(function (o) { return r.opens.indexOf(o) >= 0; }) && q.days.some(function (dd) { return r.days.indexOf(dd) >= 0; });
          if (share && mins(q.start) < mins(r.end) && mins(r.start) < mins(q.end)) { return T("sch_err_overlap"); }
        }
        r.days.forEach(function (dd) { perDay[dd] = (perDay[dd] || 0) + (mins(r.end) - mins(r.start)); });
      }
      for (var k in perDay) { if (perDay[k] > maxDay) { return T("sch_err_total", { n: maxDay + " " + plural(maxDay, A("k_min")) }); } }
      return null;
    }
    function serverProblem(e) {
      var ps = (e && e.details && e.details.problems) || [];
      if (e && e.code === "version_conflict") { return T("sch_err_conflict"); }
      for (var i = 0; i < ps.length; i++) {
        var p = ps[i];
        if (p.code === "allowed_range") { return /end/.test(p.field || "") ? T("sch_err_to", { to: fmtm(hi) }) : T("sch_err_from", { from: fmtm(lo) }); }
        if (p.code === "max_open") { return T("sch_err_total", { n: maxDay + " " + plural(maxDay, A("k_min")) }); }
        if (p.code === "overlap") { return T("sch_err_overlap"); }
      }
      return (e && (I.E && I.E[e.code])) || T("sch_err_other");
    }

    drawRules();
    root.appendChild(rulesBox);
    if (can) {
      var add = el("button", { type: "button", text: T("sch_add") });
      add.addEventListener("click", function () {
        rules.push({ id: "w" + (Date.now() % 1000000) + (serial++), days: [], start: fmtm(lo), end: fmtm(lo + 60), opens: GATED.slice() });
        drawRules();
      });
      root.appendChild(el("p", {}, [add]));
    }
    root.appendChild(weekBox);
    var al = alwaysLine();
    if (al) { root.appendChild(el("p", { "class": "meta always", text: T("sch_always", { list: al.replace(/^[^:]*:\s*/, "") }) + " " + T("sch_always_note") })); }
    var save = el("button", { type: "button", "class": "primary", disabled: !can, text: T("sch_save") });
    var undo = el("button", { type: "button", disabled: !can, text: T("sch_undo") });
    root.appendChild(el("div", { "class": "row" }, [save, undo]));
    root.appendChild(msg);
    function afterSave(r) {
      cur.etag = r.headers.get("ETag") || cur.etag;
      cur.rules = JSON.parse(JSON.stringify(r.json.rules));
      rules = cur.rules; drawRules();
      var k = kidById(cur.id); if (k) { k.windows.rules = JSON.parse(JSON.stringify(r.json.rules)); }
      say(msg, T("sch_saved", { s: rules.length ? scheduleSummary(rules) + "." : T("k_l2_nosched") }), "");
      var note = el("p", { "class": "kid-pend busy", role: "status", text: T("sch_applying") }); msg.parentNode.insertBefore(note, msg.nextSibling);
      var gen = ++pollGen, tries = 0;
      (function again() {
        if (gen !== pollGen) { return; }
        loadKids().then(function (rr) {
          if (gen !== pollGen) { return; }
          var kk = rr.ok ? kidById(cur.id) : null;
          if (kk && kk.status.policy_ok) { note.className = "kid-pend ok"; note.textContent = T("sch_applied"); return; }
          if (++tries >= 18) { note.className = "kid-pend err"; note.textContent = T("k_unconfirmed"); return; }
          setTimeout(again, 5000);
        });
      }());
    }
    save.addEventListener("click", function () {
      var p = problem();
      if (p) { say(msg, p, "err"); return; }
      save.disabled = true;
      api("PUT", base0() + "/windows", { rules: rules.map(function (r) { return { id: r.id, days: r.days, start: r.start, end: r.end, opens: r.opens }; }) }, { "If-Match": cur.etag }).then(function (r) {
        save.disabled = false;
        if (!r.ok) { say(msg, serverProblem(r.json && r.json.error), "err"); return; }
        afterSave(r);
      });
    });
    undo.addEventListener("click", function () {
      api("POST", base0() + "/windows/undo").then(function (r) { if (!r.ok) { say(msg, serverProblem(r.json && r.json.error), "err"); return; } afterSave(r); });
    });
  }
  // ── admin: filter lists ───────────────────────────────────────────────────────────────────────────────────────
  function stepsText(steps) { return steps.map(function (s) { return (s.ok ? "✓ " : "✗ ") + T("par_f_step_" + s.name) + (s.detail ? " (" + s.detail + ")" : ""); }).join("; "); }
  function showFilters() {
    clear(root);
    var out = el("div", { id: "fl-result", hidden: true });
    var listBox = el("div", { id: "fl-lists" });
    var hist = el("ul", { id: "fl-history" });
    var limits = el("p", { "class": "meta" });
    var addBox = el("section", { "class": "card" });
    root.appendChild(el("h2", { "class": "sect", text: T("par_f_lists") }));
    root.appendChild(listBox);
    root.appendChild(out);
    root.appendChild(limits);
    root.appendChild(addBox);
    root.appendChild(el("h2", { "class": "sect", text: T("par_f_history") }));
    root.appendChild(hist);
    var catalogue = [];
    function busy(on) { qa("button", root).forEach(function (b) { b.disabled = on; }); if (on) { out.hidden = false; out.className = "msg"; out.textContent = T("par_f_working"); } }
    function report(r, retry) {
      out.hidden = false;
      clear(out);
      if (!r.ok) { out.className = "msg err"; out.textContent = errText(r.json && r.json.error); return; }
      var js = r.json;
      if (js.result === "applied") { out.className = "msg"; out.textContent = T("par_f_applied"); }
      else if (js.result === "reverted") { out.className = "msg err"; out.textContent = T("par_f_reverted", { r: js.change.reason || "" }); }
      else {
        out.className = "msg warn"; out.textContent = T("par_f_held", { r: js.hold_reason || "" }) + " ";
        if (retry) { out.appendChild(el("button", { type: "button", "data-confirm-refresh": "1", text: T("par_f_confirm_apply") })); }
      }
      out.appendChild(el("div", { "class": "meta", text: stepsText(js.change.steps) }));
    }
    function load() {
      api("GET", "/v1/parental/filter-lists").then(function (r) {
        if (!r.ok) { listBox.textContent = errText(r.json && r.json.error); return; }
        var v = r.json;
        limits.textContent = T("par_f_limits", { m: v.limits.max_rules, h: v.limits.hard_cap_rules, p: v.limits.change_percent });
        clear(listBox);
        var grid = el("div", { "class": "grid" });
        v.lists.forEach(function (l) {
          grid.appendChild(el("section", { "class": "card", "data-list": String(l.id), "data-name": l.name }, [
            el("header", {}, [el("h2", { text: l.name }), el("span", { "class": "meta", text: l.enabled ? T("par_f_enabled") : T("par_f_disabled") })]),
            el("p", { "class": "meta", text: T("par_f_rules", { n: l.rules_count }) + (l.last_updated ? " · " + T("par_f_updated", { t: l.last_updated.slice(0, 16).replace("T", " ") }) : "") }),
            el("p", { "class": "meta", text: l.url }),
            el("div", { "class": "row" }, [
              el("button", { type: "button", "data-toggle": l.enabled ? "off" : "on", text: l.enabled ? T("par_f_toggle_off") : T("par_f_toggle_on") }),
              el("button", { type: "button", "data-rename": "1", text: T("par_f_rename") }),
              el("button", { type: "button", "data-refresh": "1", text: T("par_f_refresh") }),
              el("button", { type: "button", "data-revert": "1", text: T("par_f_revert") }),
              el("button", { type: "button", "class": "danger", "data-remove": "1", text: T("par_f_remove") })
            ])
          ]));
        });
        listBox.appendChild(grid);
        clear(hist);
        v.history.forEach(function (h) {
          hist.appendChild(el("li", {}, [el("strong", { text: dayhhmm(h.at) + " " + T("par_f_act_" + h.action) + " «" + (h.list_name || "") + "» — " + T("par_f_res_" + h.result) }), " ", el("span", { "class": "meta", text: (h.reason ? h.reason + " · " : "") + stepsText(h.steps) })]));
        });
      });
    }
    function buildAdd() {
      clear(addBox);
      var sel = el("select", { id: "fl-cat" }, [el("option", { value: "", text: T("par_f_choose") })].concat(catalogue.map(function (c) {
        return el("option", { value: c.catalogue_id, text: c.name + (c.installed ? " " + T("par_f_installed") : "") });
      })));
      var url = el("input", { type: "url", id: "fl-url", placeholder: "https://…", maxlength: "500" });
      var name = el("input", { type: "text", id: "fl-name", maxlength: "80" });
      var big = el("input", { type: "checkbox", id: "fl-big" });
      addBox.appendChild(el("h2", { text: T("par_f_add") }));
      addBox.appendChild(el("label", {}, [T("par_f_from_catalogue"), sel]));
      addBox.appendChild(el("label", {}, [T("par_f_url"), url]));
      addBox.appendChild(el("label", {}, [T("par_f_name"), name]));
      addBox.appendChild(el("label", { "class": "inline" }, [big, " " + T("par_f_confirm_large")]));
      addBox.appendChild(el("button", { type: "button", "class": "primary", "data-add-list": "1", text: T("par_f_add") }));
    }
    api("GET", "/v1/parental/filter-lists/catalogue").then(function (r) { catalogue = r.ok ? r.json.items : []; buildAdd(); });
    var lastRefresh = null;
    root.addEventListener("click", function (ev) {
      var card = ev.target.closest("[data-list]");
      var b = ev.target.closest("button");
      if (!b || b.disabled) { return; }
      function run(method, path, body) { busy(true); return api(method, path, body).then(function (r) { report(r, !!lastRefresh); busy(false); load(); buildAdd(); }); }
      if (b.dataset.addList) {
        var cat = document.getElementById("fl-cat").value, url = document.getElementById("fl-url").value.trim(), name = document.getElementById("fl-name").value.trim();
        var body = cat ? { catalogue_id: cat } : { url: url, name: name };
        if (document.getElementById("fl-big").checked) { body.confirm_large = true; }
        lastRefresh = null;
        run("POST", "/v1/parental/filter-lists", body);
      } else if (b.dataset.confirmRefresh && lastRefresh) {
        var id0 = lastRefresh; lastRefresh = null;
        run("POST", "/v1/parental/filter-lists/" + id0 + "/refresh", { confirm: true });
      } else if (card) {
        var id = card.dataset.list;
        lastRefresh = null;
        if (b.dataset.toggle) { run("PATCH", "/v1/parental/filter-lists/" + id, { enabled: b.dataset.toggle === "on" }); }
        else if (b.dataset.rename) { var nn = window.prompt(T("par_f_name"), card.dataset.name); if (nn && nn.trim()) { run("PATCH", "/v1/parental/filter-lists/" + id, { name: nn.trim() }); } }
        else if (b.dataset.refresh) { lastRefresh = id; run("POST", "/v1/parental/filter-lists/" + id + "/refresh", {}); }
        else if (b.dataset.revert) { run("POST", "/v1/parental/filter-lists/" + id + "/revert"); }
        else if (b.dataset.remove) { if (window.confirm(T("par_f_confirm_remove", { n: card.dataset.name }))) { run("DELETE", "/v1/parental/filter-lists/" + id); } }
      }
    });
    load();
  }

  // ── add / remove / restore a child device and the setup checklist (Task 29b) ──
  var ICONS = ["phone", "tablet", "laptop", "console", "tv", "other"];
  function stepsLine(steps) { return steps.map(function (s) { return (s.ok ? "✓ " : "✗ ") + T("par_d_step_" + s.name) + (s.detail ? " (" + s.detail + ")" : ""); }).join("; "); }
  function warnList(ws) { return el("ul", {}, (ws || []).map(function (w) { return el("li", { "class": "meta", text: w.message }); })); }
  function checklistBox(childId, title) {
    var box = section(title || T("par_cl_title"), "par-checklist");
    var body = el("div", {});
    box.appendChild(body);
    api("GET", "/v1/parental/children/" + childId + "/setup-checklist").then(function (r) {
      if (!r.ok) { body.appendChild(el("p", { "class": "msg err", text: errText(r.json && r.json.error) })); return; }
      body.appendChild(el("p", { "class": "meta", text: r.json.note }));
      r.json.items.forEach(function (i) {
        var mark = { ok: "✓", warn: "⚠", todo: "○", unknown: "?", info: "ℹ" }[i.status] || "?";
        body.appendChild(el("div", { "class": "rule cl-" + i.status }, [
          el("strong", { text: mark + " " + i.title }), el("p", { "class": "meta", text: i.text }), i.detail ? el("p", { text: i.detail }) : null
        ]));
      });
    });
    return box;
  }
  function addPanel() {
    var panel = el("section", { "class": "card", id: "par-add", hidden: true });
    var name = el("input", { type: "text", id: "add-name", maxlength: "40" });
    var label = el("input", { type: "text", id: "add-label", maxlength: "40" });
    var ip = el("input", { type: "text", id: "add-ip", placeholder: "192.168.31.70", maxlength: "15" });
    var mac = el("input", { type: "text", id: "add-mac", placeholder: "aa:bb:cc:dd:ee:ff", maxlength: "17" });
    var icon = el("select", { id: "add-icon" }, ICONS.map(function (k) { return el("option", { value: k, text: T("par_icon_" + k) }); }));
    var tpl = el("select", { id: "add-template" }, [el("option", { value: "closed", text: T("par_tpl_closed") })]);
    var cands = el("div", { id: "add-cands" });
    var out = el("div", { id: "add-result", hidden: true });
    panel.appendChild(el("h2", { text: T("par_add_title") }));
    panel.appendChild(el("p", { "class": "meta", text: T("par_add_hint") }));
    panel.appendChild(el("h3", { text: T("par_add_cands") }));
    panel.appendChild(cands);
    [["par_add_child", name], ["par_add_label", label], ["par_add_icon", icon], ["par_add_ip", ip], ["par_add_mac", mac], ["par_add_template", tpl]].forEach(function (p) { panel.appendChild(el("label", {}, [T(p[0]), p[1]])); });
    panel.appendChild(el("button", { type: "button", "class": "primary", "data-add-go": "1", text: T("par_add_go") }));
    panel.appendChild(out);
    function loadCands() {
      clear(cands);
      cands.appendChild(el("p", { "class": "meta", text: T("par_loading") }));
      api("GET", "/v1/parental/candidates").then(function (r) {
        clear(cands);
        if (!r.ok) { cands.appendChild(el("p", { "class": "msg err", text: errText(r.json && r.json.error) })); return; }
        if (!r.json.candidates.length) { cands.appendChild(el("p", { "class": "meta", text: T("par_add_nocands") })); }
        r.json.candidates.forEach(function (c) {
          var txt = c.ip + " · " + c.mac + (c.vendor ? " · " + c.vendor : "") + (c.hostname ? " · " + c.hostname : "") + (c.randomised_mac ? " · " + T("par_random_mac") : "") +
            (c.queries_today !== null ? " · " + T("par_add_queries", { q: c.queries_today }) : "") + (c.last_query_at ? " · " + T("par_last_query", { t: c.last_query_at.slice(11, 16) }) : "");
          cands.appendChild(el("div", { "class": "row exc" }, [el("span", { text: txt }), el("button", { type: "button", "data-pick": c.ip, "data-mac": c.mac, "data-kind": c.device_kind, text: T("par_add_pick") })]));
        });
        cands.appendChild(el("p", { "class": "meta", text: r.json.note }));
      });
    }
    panel.loadCands = loadCands;
    panel.fillTemplates = function (kids) {
      clear(tpl);
      tpl.appendChild(el("option", { value: "closed", text: T("par_tpl_closed") }));
      kids.forEach(function (k) { tpl.appendChild(el("option", { value: "copy:" + k.id, text: T("par_tpl_copy", { n: k.name }) })); });
    };
    panel.addEventListener("click", function (ev) {
      var pick = ev.target.closest("button[data-pick]"), go = ev.target.closest("button[data-add-go]");
      if (pick) { ip.value = pick.dataset.pick; mac.value = pick.dataset.mac; if (ICONS.indexOf(pick.dataset.kind) >= 0) { icon.value = pick.dataset.kind; } return; }
      if (!go || go.disabled) { return; }
      var body = { name: name.value.trim(), ip: ip.value.trim(), icon: icon.value, template: tpl.value };
      if (label.value.trim()) { body.device_label = label.value.trim(); }
      if (mac.value.trim()) { body.mac = mac.value.trim(); }
      go.disabled = true;
      out.hidden = false; out.className = "msg"; out.textContent = T("par_d_working");
      api("POST", "/v1/parental/children", body).then(function (r) {
        go.disabled = false;
        clear(out);
        if (!r.ok) { out.className = "msg err"; out.textContent = errText(r.json && r.json.error); return; }
        var js = r.json;
        out.className = js.result === "created" ? "msg" : "msg err";
        out.appendChild(el("p", { text: js.result === "created" ? T("par_d_created") : T("par_d_reverted", { r: js.reason || "" }) }));
        out.appendChild(el("p", { "class": "meta", text: stepsLine(js.steps) }));
        if (js.warnings && js.warnings.length) { out.appendChild(el("h3", { text: T("par_d_warnings") })); out.appendChild(warnList(js.warnings)); }
        if (js.result === "created") { out.appendChild(checklistBox(js.child_id)); out.appendChild(el("p", {}, [el("a", { "class": "linkbtn", href: "#" + js.child_id, text: T("k_open_access") })])); }
      });
    });
    return panel;
  }
  function removeBox() {
    var box = section(T("par_rm_title"), "par-remove");
    var d = cur.d;
    var inp = el("input", { type: "text", id: "rm-confirm", placeholder: d.name, maxlength: "100" });
    var out = el("div", { id: "rm-result", hidden: true });
    box.appendChild(el("p", { "class": "meta", text: T("par_rm_hint", { n: d.name }) }));
    box.appendChild(el("label", {}, [T("par_rm_type"), inp]));
    box.appendChild(el("button", { type: "button", "class": "danger", "data-rm-go": "1", text: T("par_rm_go") }));
    box.appendChild(out);
    box.addEventListener("click", function (ev) {
      var b = ev.target.closest("button[data-rm-go]");
      if (!b || b.disabled) { return; }
      if (inp.value.trim() !== d.name) { out.hidden = false; out.className = "msg err"; out.textContent = T("par_rm_mismatch"); return; }
      b.disabled = true;
      out.hidden = false; out.className = "msg"; out.textContent = T("par_d_working");
      api("DELETE", "/v1/parental/children/" + cur.id, { confirm: inp.value.trim() }).then(function (r) {
        b.disabled = false;
        clear(out);
        if (!r.ok) { out.className = "msg err"; out.textContent = errText(r.json && r.json.error); return; }
        out.className = r.json.result === "removed" ? "msg" : "msg err";
        out.appendChild(el("p", { text: r.json.result === "removed" ? T("par_d_removed") : T("par_d_removal_incomplete") }));
        out.appendChild(el("p", { "class": "meta", text: stepsLine(r.json.steps) }));
        out.appendChild(el("p", {}, [el("a", { "class": "back", href: "#", text: T("par_back") })]));
      });
    });
    return box;
  }

  // the admin entries of the list page, behind one "Ещё" (web sign-in only; an app session never gets them)
  function adminMenu(kids) {
    var body = el("div", { "class": "kd-body" });
    var det = el("details", { "class": "card kd kd-more" }, [el("summary", { text: T("k_more") }), body]);
    var panel = addPanel();
    panel.fillTemplates(kids);
    body.appendChild(el("p", {}, [el("a", { "class": "linkbtn", href: "/parental/filters", text: T("k_more_lists") })]));
    body.appendChild(el("p", {}, [el("button", { type: "button", "data-add-open": "1", text: T("k_more_add") })]));
    body.appendChild(panel);
    var removed = el("div", { id: "par-removed" });
    body.appendChild(removed);
    body.addEventListener("click", function (ev) {
      var o = ev.target.closest("button[data-add-open]"), rs = ev.target.closest("button[data-restore]");
      if (o) { panel.hidden = !panel.hidden; if (!panel.hidden) { panel.loadCands(); } return; }
      if (rs && !rs.disabled) {
        rs.disabled = true;
        removed.appendChild(el("p", { "class": "msg", text: T("par_d_working") }));
        api("POST", "/v1/parental/removed-children/" + rs.dataset.restore + "/restore").then(function (r) {
          if (!r.ok) { rs.disabled = false; removed.appendChild(el("p", { "class": "msg err", text: errText(r.json && r.json.error) })); return; }
          removed.appendChild(el("p", { "class": r.json.result === "created" ? "msg" : "msg err", text: r.json.result === "created" ? T("par_d_restored") : T("par_d_reverted", { r: r.json.reason || "" }) }));
          if (r.json.result === "created") { setTimeout(showChildren, 1500); }
        });
      }
    });
    api("GET", "/v1/parental/removed-children").then(function (r) {
      if (!r.ok || !r.json.removed.length) { return; }
      removed.appendChild(el("h3", { text: T("par_removed_title") }));
      r.json.removed.forEach(function (x) {
        removed.appendChild(el("div", { "class": "row exc" }, [el("span", { text: x.name + " · " + x.ip + " · " + T("par_removed_until", { t: dayhhmm(x.restorable_until) }) }),
                                                              el("button", { type: "button", "data-restore": x.removal_id, text: T("par_removed_undo") })]));
      });
    });
    return det;
  }

  // ── wiring ─────────────────────────────────────────────────────────────────────────────────────────────────────
  if (root.dataset.mode === "filters") { showFilters(); return; }
  root.addEventListener("click", function (ev) {
    var o = ev.target.closest("button[data-open-sheet]"), x = ev.target.closest("button[data-close-x]"), rt = ev.target.closest("button[data-retry]");
    if (o) { var k = kidById(o.dataset.openSheet); if (k) { openSheet(k, o); } }
    else if (x) { closeException(x.dataset.kid, x.dataset.closeX); }
    else if (rt) { var p = ST.pend[rt.dataset.retry]; if (p && p.retry) { p.retry(); } }
  });
  function route() {
    closeSheet();
    var h = location.hash.replace(/^#/, "");
    if (!h) { showChildren(); } else { var parts = h.split("/"); showChild(parts[0], parts[1]); }
  }
  window.addEventListener("hashchange", function () { window.scrollTo(0, 0); route(); });
  route();
}());

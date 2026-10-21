(function () {
  "use strict";
  // Parental-control pages (Task 29). Everything the page shows from the server is put in with textContent / value, never as HTML.
  var I = { S: {}, E: {} };
  try { I = JSON.parse(document.getElementById("i18n").textContent); } catch (e) { /* keep defaults */ }
  var root = document.getElementById("par-root");
  if (!root) { return; }

  function T(key, vars) {
    var s = (I.S && I.S[key]) || key;
    if (vars) { Object.keys(vars).forEach(function (k) { s = s.split("{" + k + "}").join(vars[k]); }); }
    return s;
  }
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
      });
  }
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") { n.textContent = attrs[k]; }
      else if (k === "class") { n.className = attrs[k]; }
      else if (k === "value") { n.value = attrs[k]; }
      else if (k === "checked" || k === "disabled" || k === "hidden") { n[k] = !!attrs[k]; }
      else { n.setAttribute(k, attrs[k]); }
    });
    (kids || []).forEach(function (c) { if (c) { n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); } });
    return n;
  }
  function clear(n) { while (n.firstChild) { n.removeChild(n.firstChild); } }
  function hhmm(iso) { return iso ? iso.slice(11, 16) : ""; }
  function dayhhmm(iso) { return iso ? iso.slice(8, 10) + "." + iso.slice(5, 7) + " " + iso.slice(11, 16) : ""; }
  function say(box, text, kind) { box.hidden = !text; box.textContent = text || ""; box.className = "msg" + (kind ? " " + kind : ""); }
  function leftText(s) { return s >= 3600 ? Math.floor(s / 3600) + " ч " + Math.floor((s % 3600) / 60) + " мин" : Math.max(1, Math.ceil(s / 60)) + " мин"; }

  // ── health badge ─────────────────────────────────────────────────────────────────────────────────────────────
  function loadHealth() {
    var b = document.getElementById("par-health");
    if (!b) { return; }
    api("GET", "/v1/parental/health").then(function (r) {
      var kind = "unknown", text = T("par_loading");
      if (r.ok) {
        var h = r.json;
        if (!h.agh.reachable) { kind = "down"; text = T("par_health_agh"); }
        else if (!h.policy.in_sync) { kind = "degraded"; text = T("par_health_bad"); }
        else if (h.replica.parity === "mismatch") { kind = "degraded"; text = T("par_health_parity"); }
        else { kind = "ok"; text = T("par_health_ok"); }
      }
      b.className = "badge b-" + kind;
      b.querySelector(".btext").textContent = text;
    });
  }

  // ── children grid ──────────────────────────────────────────────────────────────────────────────────────────────
  function statusLine(s) {
    var parts = [T("par_state_" + s.state)];
    if (s.open_until) { parts.push(T("par_until", { t: hhmm(s.open_until) })); }
    else if (s.opens_at) { parts.push(T("par_opens_at", { t: dayhhmm(s.opens_at) })); }
    return parts.join(" · ");
  }
  function showChildren() {
    clear(root);
    root.appendChild(el("p", { "class": "meta", text: T("par_loading") }));
    api("GET", "/v1/parental/children").then(function (r) {
      clear(root);
      if (!r.ok) { root.appendChild(el("p", { "class": "msg err", text: errText(r.json && r.json.error) })); return; }
      var grid = el("div", { "class": "grid" });
      r.json.children.forEach(function (c) {
        var s = c.status;
        var card = el("section", { "class": "card", "data-child": c.id }, [
          el("header", {}, [el("h2", { text: c.name }), el("span", { "class": "meta", text: c.device_kind })]),
          el("p", { "class": "par-state s-" + s.state, text: statusLine(s) }),
          el("p", { "class": "meta", text: s.queries_today === null ? "" : T("par_queries", { q: s.queries_today, b: s.blocked_today }) + (s.last_query_at ? " · " + T("par_last_query", { t: hhmm(s.last_query_at) }) : "") }),
          el("p", { "class": "meta " + (s.policy_ok ? "" : "bad"), text: (s.policy_ok ? "✓ " + T("par_policy_ok") : "✗ " + T("par_policy_bad")) + " · " + (s.dns_seen ? T("par_dns_seen") : T("par_dns_unseen")) }),
          el("button", { type: "button", "class": "primary", "data-open": c.id, text: T("par_open") })
        ]);
        s.exceptions.forEach(function (x) { card.insertBefore(el("p", { "class": "meta", text: T("par_target_" + x.target) + ": " + T("par_exc_left", { r: leftText(x.remaining_s) }) }), card.lastChild); });
        grid.appendChild(card);
      });
      root.appendChild(grid);
      if (isAdmin) { root.appendChild(adminToolbar(r.json.children)); }
    });
  }

  // ── one child ─────────────────────────────────────────────────────────────────────────────────────────────────
  var cur = null;                                         // {id, detail, etag}
  function section(title, id) { var s = el("section", { "class": "card par-sect", id: id }); s.appendChild(el("h2", { text: title })); return s; }

  function showChild(id) {
    clear(root);
    root.appendChild(el("p", { "class": "meta", text: T("par_loading") }));
    Promise.all([api("GET", "/v1/parental/children/" + id), api("GET", "/v1/parental/children/" + id + "/windows")]).then(function (rs) {
      clear(root);
      if (!rs[0].ok) { root.appendChild(el("p", { "class": "msg err", text: errText(rs[0].json && rs[0].json.error) })); return; }
      cur = { id: id, d: rs[0].json, etag: rs[1].headers.get("ETag"), rules: JSON.parse(JSON.stringify(rs[0].json.windows.rules)) };
      var d = cur.d;
      root.appendChild(el("p", {}, [el("button", { type: "button", "data-back": "1", text: T("par_back") })]));
      root.appendChild(el("h2", { "class": "sect", text: d.name }));
      root.appendChild(el("p", { "class": "par-state s-" + d.status.state, text: statusLine(d.status) }));
      root.appendChild(identityBox(d.identity));
      root.appendChild(windowsBox());
      root.appendChild(exceptionsBox());
      root.appendChild(domainsBox());
      root.appendChild(activityBox());
      if (d.can.grant_exceptions) { root.appendChild(checklistBox(id)); }
      if (isAdmin) { root.appendChild(removeBox()); }
    });
  }

  function identityBox(i) {
    var rows = [[T("par_ip"), i.ip], [T("par_mac"), (i.mac || "—") + (i.randomised_mac ? " (" + T("par_random_mac") + ")" : "")], [T("par_vendor"), i.vendor || "—"], [T("par_hostname"), i.hostname || "—"]];
    return el("p", { "class": "meta", text: T("par_identity") + ": " + rows.map(function (r) { return r[0] + " " + r[1]; }).join(" · ") });
  }

  // windows
  function windowsBox() {
    var d = cur.d, w = d.windows, g = w.guardrails;
    var box = section(T("par_windows"), "par-windows");
    box.appendChild(el("p", { "class": "meta", text: T("par_windows_hint") }));
    var capped = g.max_exception_minutes || g.exception_latest_end || g.all_latest_end;            // the exception limits are optional: by default there are none
    var excText = capped ? T("par_guard_exc_capped", { x: g.max_exception_minutes || 1440, e: g.exception_latest_end || "23:59", all: g.all_latest_end || "23:59" }) : T("par_guard_exc_free");
    box.appendChild(el("p", { "class": "meta", text: T("par_guard", { a: g.allowed_from, b: g.allowed_to, m: g.max_open_minutes_per_day }) + " " + excText + (g.proposed ? " " + T("par_guard_proposed") : "") }));
    var rulesBox = el("div", { id: "par-rules" });
    var msg = el("p", { id: "par-win-msg", role: "status", hidden: true });
    var out = el("div", { id: "par-preview", hidden: true });
    box.appendChild(rulesBox);
    var can = w.can_edit;
    function draw() {
      clear(rulesBox);
      cur.rules.forEach(function (r, i) {
        var days = el("div", { "class": "days" }, I.S.dow.map(function (n, k) {
          return el("label", {}, [el("input", { type: "checkbox", "data-day": String(k + 1), checked: r.days.indexOf(k + 1) >= 0, disabled: !can }), " " + n]);
        }));
        var opens = el("div", { "class": "days" }, ["youtube", "roblox", "maps"].map(function (t) {
          return el("label", {}, [el("input", { type: "checkbox", "data-opens": t, checked: r.opens.indexOf(t) >= 0, disabled: !can }), " " + T("par_target_" + t)]);
        }));
        rulesBox.appendChild(el("div", { "class": "rule", "data-i": String(i) }, [
          el("strong", { text: r.id }), days,
          el("div", { "class": "times" }, [el("label", {}, [T("from"), el("input", { type: "time", "data-f": "start", value: r.start, disabled: !can })]),
                                          el("label", {}, [T("to"), el("input", { type: "time", "data-f": "end", value: r.end === "24:00" ? "23:59" : r.end, disabled: !can })])]),
          el("div", {}, [T("par_rule_opens") + ":", opens])
        ]));
      });
    }
    function collect() {
      qa(".rule", rulesBox).forEach(function (n, i) {
        var r = cur.rules[i];
        r.days = qa("[data-day]", n).filter(function (c) { return c.checked; }).map(function (c) { return parseInt(c.dataset.day, 10); });
        r.opens = qa("[data-opens]", n).filter(function (c) { return c.checked; }).map(function (c) { return c.dataset.opens; });
        r.start = n.querySelector('[data-f="start"]').value;
        var e = n.querySelector('[data-f="end"]').value;
        r.end = (e === "23:59") ? "24:00" : e;
      });
      return cur.rules;
    }
    draw();
    if (!can) { box.appendChild(el("p", { "class": "msg warn", text: T("par_no_edit") })); }
    var row = el("div", { "class": "row" }, [
      el("button", { type: "button", "data-act": "preview", disabled: !can, text: T("par_check_preview") }),
      el("button", { type: "button", "class": "primary", "data-act": "save", disabled: !can, text: T("par_save") }),
      el("button", { type: "button", "data-act": "undo", disabled: !can, text: T("par_undo") })
    ]);
    box.appendChild(row);
    box.appendChild(msg);
    box.appendChild(out);
    box.addEventListener("click", function (ev) {
      var b = ev.target.closest("button[data-act]");
      if (!b || b.disabled) { return; }
      var act = b.dataset.act, base = "/v1/parental/children/" + cur.id + "/windows";
      if (act === "preview") {
        api("POST", base + "/preview", { rules: collect() }).then(function (r) {
          clear(out);
          if (!r.ok) { out.hidden = true; say(msg, errText(r.json && r.json.error), "err"); return; }
          say(msg, "");
          out.hidden = false;
          if (!r.json.valid) {
            out.appendChild(el("p", { "class": "msg err", text: T("par_preview_invalid") }));
            out.appendChild(el("ul", {}, r.json.problems.map(function (p) { return el("li", { text: ((I.E && I.E[p.code]) || "") + " " + p.message }); })));
          } else {
            out.appendChild(el("p", { text: T("par_preview_valid") }));
            out.appendChild(el("ul", {}, r.json.open_next_7_days.map(function (x) { return el("li", { text: dayhhmm(x.from) + " – " + (x.to.slice(0, 10) !== x.from.slice(0, 10) && x.to.slice(11, 16) === "00:00" ? "24:00" : hhmm(x.to)) + ": " + x.opens.map(function (o) { return T("par_target_" + o); }).join(", ") }); })));
          }
        });
      } else if (act === "save") {
        api("PUT", base, { rules: collect() }, { "If-Match": cur.etag }).then(function (r) {
          if (!r.ok) { say(msg, errText(r.json && r.json.error), "err"); return; }
          cur.etag = r.headers.get("ETag") || cur.etag;
          cur.rules = JSON.parse(JSON.stringify(r.json.rules));
          draw();
          out.hidden = true;
          say(msg, T("par_saved"), "");
          loadHealth();
        });
      } else if (act === "undo") {
        api("POST", base + "/undo").then(function (r) {
          if (!r.ok) { say(msg, errText(r.json && r.json.error), "err"); return; }
          cur.etag = r.headers.get("ETag") || cur.etag;
          cur.rules = JSON.parse(JSON.stringify(r.json.rules));
          draw();
          out.hidden = true;
          say(msg, T("par_saved"), "");
        });
      }
    });
    return box;
  }
  function qa(sel, rootNode) { return Array.prototype.slice.call((rootNode || document).querySelectorAll(sel)); }

  // exceptions
  function exceptionsBox() {
    var d = cur.d;
    var box = section(T("par_exceptions"), "par-exceptions");
    var list = el("div", { id: "par-exc-list" });
    var msg = el("p", { role: "status", hidden: true });
    function draw(items) {
      clear(list);
      if (!items.length) { list.appendChild(el("p", { "class": "meta", text: T("par_exc_none") })); return; }
      items.forEach(function (x) {
        list.appendChild(el("div", { "class": "row exc", "data-x": x.id }, [
          el("span", { text: T("par_target_" + x.target) + " · " + T("par_until", { t: hhmm(x.expires_at) }) + " · " + T("par_exc_left", { r: leftText(x.remaining_s) }) + " · " + T("par_exc_by", { p: x.created_by }) }),
          el("button", { type: "button", "class": "danger", "data-cancel": x.id, disabled: !d.can.grant_exceptions, text: T("par_exc_cancel") })
        ]));
      });
    }
    function refresh() { api("GET", "/v1/parental/children/" + cur.id + "/exceptions").then(function (r) { if (r.ok) { draw(r.json.exceptions); } }); loadHealth(); }
    draw(d.exceptions);
    box.appendChild(el("h3", { text: T("par_exc_active") }));
    box.appendChild(list);
    box.appendChild(el("h3", { text: T("par_exc_grant") }));
    ["youtube", "roblox", "maps", "all"].forEach(function (t) {
      var row = el("div", { "class": "row" }, [el("strong", { text: T("par_target_" + t) })]);
      [[30, "par_exc_30"], [60, "par_exc_60"], [120, "par_exc_120"], [0, "par_exc_eod"]].forEach(function (p) {
        row.appendChild(el("button", { type: "button", "data-grant": t, "data-min": String(p[0]), disabled: !d.can.grant_exceptions, text: T(p[1]) }));
      });
      box.appendChild(row);
    });
    box.appendChild(msg);
    box.addEventListener("click", function (ev) {
      var g = ev.target.closest("button[data-grant]"), c = ev.target.closest("button[data-cancel]");
      if (g && !g.disabled) {
        var m = parseInt(g.dataset.min, 10);
        api("POST", "/v1/parental/children/" + cur.id + "/exceptions", m ? { target: g.dataset.grant, minutes: m } : { target: g.dataset.grant, until_end_of_day: true }).then(function (r) {
          if (!r.ok) { say(msg, errText(r.json && r.json.error), "err"); return; }
          say(msg, "");
          refresh();
        });
      } else if (c && !c.disabled) {
        api("DELETE", "/v1/parental/children/" + cur.id + "/exceptions/" + c.dataset.cancel).then(function (r) {
          if (!r.ok) { say(msg, errText(r.json && r.json.error), "err"); return; }
          refresh();
        });
      }
    });
    return box;
  }

  // per-child domain lists
  var domState = { lists: null, msg: null, redraw: null };
  function domainsBox() {
    var d = cur.d;
    var box = section(T("par_domains"), "par-domains");
    var cols = el("div", { "class": "grid" });
    var msg = el("p", { role: "status", hidden: true });
    function listCol(kind) {
      var col = el("div", {}, [el("h3", { text: T(kind === "allow" ? "par_allow" : "par_deny") })]);
      var ul = el("ul", { "data-kind": kind });
      col.appendChild(ul);
      var inp = el("input", { type: "text", placeholder: T("par_dom_placeholder"), "data-new": kind, disabled: !d.can.edit_domains, maxlength: "253" });
      col.appendChild(el("div", { "class": "row" }, [inp, el("button", { type: "button", "data-add": kind, disabled: !d.can.edit_domains, text: T("par_dom_add") })]));
      return col;
    }
    cols.appendChild(listCol("allow"));
    cols.appendChild(listCol("deny"));
    box.appendChild(cols);
    box.appendChild(el("p", {}, [el("button", { type: "button", "data-dom-undo": "1", disabled: !d.can.edit_domains, text: T("par_dom_undo") })]));
    box.appendChild(msg);
    function draw(lists) {
      cur.d.domains = lists;
      ["allow", "deny"].forEach(function (k) {
        var ul = box.querySelector('ul[data-kind="' + k + '"]');
        clear(ul);
        if (!lists[k].length) { ul.appendChild(el("li", { "class": "meta", text: T("par_dom_empty") })); }
        lists[k].forEach(function (dom) {
          ul.appendChild(el("li", {}, [dom + " ", el("button", { type: "button", "data-rm": dom, "data-rm-kind": k, disabled: !d.can.edit_domains, text: T("par_dom_remove") })]));
        });
      });
    }
    draw(d.domains);
    function result(r) {
      if (!r.ok) { say(msg, errText(r.json && r.json.error), "err"); return; }
      say(msg, "");
      draw(r.json);
      var act = document.getElementById("par-activity-rows");
      if (act) { act.dispatchEvent(new Event("domains-changed")); }
      loadHealth();
    }
    box.addEventListener("click", function (ev) {
      var a = ev.target.closest("button[data-add]"), rm = ev.target.closest("button[data-rm]"), un = ev.target.closest("button[data-dom-undo]");
      var base = "/v1/parental/children/" + cur.id + "/domains";
      if (a && !a.disabled) {
        var inp = box.querySelector('input[data-new="' + a.dataset.add + '"]');
        if (!inp.value.trim()) { return; }
        api("POST", base, { kind: a.dataset.add, domain: inp.value.trim() }).then(function (r) { if (r.ok) { inp.value = ""; } result(r); });
      } else if (rm && !rm.disabled) {
        api("DELETE", base + "?kind=" + encodeURIComponent(rm.dataset.rmKind) + "&domain=" + encodeURIComponent(rm.dataset.rm)).then(result);
      } else if (un && !un.disabled) {
        api("POST", base + "/undo").then(result);
      }
    });
    domState.msg = msg;
    domState.apply = function (kind, domain, btn) {
      api("POST", base0() + "/domains", { kind: kind, domain: domain }).then(function (r) { result(r); if (btn) { btn.disabled = true; } });
    };
    return box;
  }
  function base0() { return "/v1/parental/children/" + cur.id; }

  // activity
  function activityBox() {
    var d = cur.d;
    var box = section(T("par_activity"), "par-activity");
    box.appendChild(el("p", { "class": "meta", id: "par-act-note" }));
    var range = el("select", { id: "par-range" }, [el("option", { value: "24h", text: T("par_range_24h") }), el("option", { value: "7d", text: T("par_range_7d") })]);
    var onlyB = el("input", { type: "checkbox", id: "par-only-blocked" });
    var search = el("input", { type: "search", id: "par-search", placeholder: T("par_search"), maxlength: "80" });
    box.appendChild(el("div", { "class": "filters" }, [range, el("label", {}, [onlyB, " " + T("par_only_blocked")]), search]));
    var wrap = el("div", { "class": "tablewrap" });
    var tbody = el("tbody", { id: "par-activity-rows" });
    wrap.appendChild(el("table", {}, [el("thead", {}, [el("tr", {}, [T("par_col_name"), T("par_col_ok"), T("par_col_blocked"), T("par_col_last"), T("par_col_flags"), ""].map(function (h) { return el("th", { text: h }); }))]), tbody]));
    box.appendChild(wrap);
    var more = el("button", { type: "button", hidden: true, text: T("par_more") });
    var msg = el("p", { role: "status", hidden: true });
    box.appendChild(more);
    box.appendChild(msg);
    var cursor = null;
    function row(r) {
      var flags = [];
      if (r.in_allow) { flags.push(T("par_flag_in_allow")); }
      if (r.in_deny) { flags.push(T("par_flag_in_deny")); }
      if (r.listed_by) { flags.push(T("par_flag_list", { l: r.listed_by })); }
      if (r.threat) { flags.push(T("par_flag_threat")); }
      var allowB = el("button", { type: "button", "data-allow": r.name, disabled: !d.can.edit_domains || r.in_allow, text: T("par_allow_always") });
      var denyB = el("button", { type: "button", "class": "danger", "data-deny": r.name, disabled: !d.can.edit_domains || r.in_deny, text: T("par_block_forever") });
      return el("tr", {}, [el("td", { text: r.name }), el("td", { text: String(r.allowed) }), el("td", { text: String(r.blocked) }), el("td", { text: dayhhmm(r.last) }),
                           el("td", { text: flags.join("; ") }), el("td", {}, [allowB, " ", denyB])]);
    }
    function load(append) {
      var qs = "?range=" + range.value + (onlyB.checked ? "&blocked=1" : "") + (search.value.trim() ? "&q=" + encodeURIComponent(search.value.trim()) : "") + (append && cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
      api("GET", "/v1/parental/children/" + cur.id + "/activity" + qs).then(function (r) {
        if (!r.ok) { say(msg, errText(r.json && r.json.error), "err"); return; }
        say(msg, r.json.truncated ? T("par_act_truncated") : "");
        if (!append) { clear(tbody); }
        r.json.rows.forEach(function (x) { tbody.appendChild(row(x)); });
        if (!tbody.children.length) { tbody.appendChild(el("tr", {}, [el("td", { colspan: "6", text: T("par_act_none") })])); }
        document.getElementById("par-act-note").textContent = r.json.note + " " + T("par_retention_line", { s: r.json.retention.server, r: r.json.retention.replica });
        cursor = r.json.next_cursor;
        more.hidden = !cursor;
      });
    }
    range.addEventListener("change", function () { cursor = null; load(false); });
    onlyB.addEventListener("change", function () { cursor = null; load(false); });
    var t = null;
    search.addEventListener("input", function () { clearTimeout(t); t = setTimeout(function () { cursor = null; load(false); }, 400); });
    more.addEventListener("click", function () { load(true); });
    tbody.addEventListener("click", function (ev) {
      var a = ev.target.closest("button[data-allow]"), dn = ev.target.closest("button[data-deny]");
      if (a && !a.disabled) { domState.apply("allow", a.dataset.allow, a); }
      else if (dn && !dn.disabled) { domState.apply("deny", dn.dataset.deny, dn); }
    });
    tbody.addEventListener("domains-changed", function () { cursor = null; load(false); });
    load(false);
    return box;
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

  // ── add / remove / restore a child device and the setup checklist (Task 29b) ───────────────────────────────────────
  var isAdmin = root.dataset.admin === "yes";
  var ICONS = ["phone", "tablet", "laptop", "console", "tv", "other"];
  function stepsLine(steps) { return steps.map(function (s) { return (s.ok ? "✓ " : "✗ ") + T("par_d_step_" + s.name) + (s.detail ? " (" + s.detail + ")" : ""); }).join("; "); }
  function warnList(ws) { return el("ul", {}, (ws || []).map(function (w) { return el("li", { "class": "meta", text: w.message }); })); }

  function checklistBox(childId) {
    var box = section(T("par_cl_title"), "par-checklist");
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
        if (js.result === "created") { out.appendChild(checklistBox(js.child_id)); out.appendChild(el("p", {}, [el("button", { type: "button", "data-open": js.child_id, text: T("par_open") })])); }
      });
    });
    return panel;
  }

  function adminToolbar(kids) {
    var wrap = el("div", { id: "par-admin" });
    var panel = addPanel();
    panel.fillTemplates(kids);
    wrap.appendChild(el("p", {}, [el("button", { type: "button", "class": "primary", "data-add-open": "1", text: T("par_add_open") })]));
    wrap.appendChild(panel);
    var removed = el("div", { id: "par-removed" });
    wrap.appendChild(removed);
    wrap.addEventListener("click", function (ev) {
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
    return wrap;
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
        out.appendChild(el("p", {}, [el("button", { type: "button", "data-back": "1", text: T("par_back") })]));
      });
    });
    return box;
  }

  // ── wiring ─────────────────────────────────────────────────────────────────────────────────────────────────────
  if (root.dataset.mode === "filters") { showFilters(); return; }
  loadHealth();
  setInterval(loadHealth, 60000);
  root.addEventListener("click", function (ev) {
    var o = ev.target.closest("button[data-open]"), back = ev.target.closest("button[data-back]");
    if (o) { location.hash = o.dataset.open; }
    else if (back) { location.hash = ""; }
  });
  function route() { var id = location.hash.replace(/^#/, ""); if (id) { showChild(id); } else { showChildren(); } }
  window.addEventListener("hashchange", route);
  route();
}());

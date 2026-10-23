(function () {
  "use strict";
  // The release page (Task 30c): list, «Опубликовать», «Остановить выдачу». Everything from the server is put in with textContent.
  var I = { S: {}, E: {} };
  try { I = JSON.parse(document.getElementById("i18n").textContent); } catch (e) { /* keep defaults */ }
  var root = document.getElementById("rel-root"), msg = document.getElementById("rel-msg");
  if (!root) { return; }
  function T(key, vars) {
    var s = (I.S && I.S[key]) || key;
    if (vars) { Object.keys(vars).forEach(function (k) { s = s.split("{" + k + "}").join(vars[k]); }); }
    return s;
  }
  function csrf() { var m = document.querySelector('meta[name="csrf"]'); return m ? m.content : ""; }
  function api(method, path, body) {
    var h = { "Accept": "application/json", "X-CSRF-Token": csrf() };
    if (body !== undefined) { h["Content-Type"] = "application/json"; }
    if (method !== "GET") { h["Idempotency-Key"] = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2); }
    return fetch(path, { method: method, headers: h, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin" })
      .then(function (r) { if (r.status === 401) { location.href = "/login"; return new Promise(function () {}); } return r.json().then(function (j) { return { ok: r.ok, json: j }; }, function () { return { ok: r.ok, json: null }; }); },
            function () { return { ok: false, json: { error: { code: "network", message: T("net_down_short") } } }; });
  }
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { if (k === "text") { n.textContent = attrs[k]; } else if (k === "class") { n.className = attrs[k]; } else { n.setAttribute(k, attrs[k]); } });
    (kids || []).forEach(function (c) { if (c) { n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); } });
    return n;
  }
  function say(text, kind) { msg.hidden = !text; msg.textContent = text || ""; msg.className = "msg" + (kind ? " " + kind : ""); }
  function errText(e) { return (e && ((I.E && I.E[e.code]) || e.message)) || T("err_generic", { m: "?" }); }
  function when(iso) { return iso ? iso.slice(8, 10) + "." + iso.slice(5, 7) + "." + iso.slice(0, 4) + " " + iso.slice(11, 16) : "—"; }

  function draw(d) {
    root.textContent = "";
    var h = d.health || {};
    var status;
    if (!h.key_ok) { status = T("rel_no_key"); }
    else if (h.published === null) { status = T("rel_none_published"); }
    else if (d.stopped) { status = T("rel_stopped", { v: h.published }); }
    else { status = T("rel_serving", { v: h.published }); }
    root.appendChild(el("section", { "class": "card" }, [el("h2", { text: T("rel_channel") }), el("p", { "class": "rel-status", text: status }),
      h.published !== null && h.manifest_ok === false ? el("p", { "class": "msg err", text: T("rel_manifest_bad") }) : null,
      h.published !== null && h.apk_ok === false ? el("p", { "class": "msg err", text: T("rel_apk_bad") }) : null,
      el("p", { "class": "meta", text: T("rel_cert") }), el("p", { "class": "sha", text: d.pinned_cert_sha256 || "—" }),
      h.published !== null ? el("button", { type: "button", "class": d.stopped ? "primary" : "danger", "data-act": d.stopped ? "resume" : "stop", text: T(d.stopped ? "rel_resume" : "rel_stop") }) : null]));
    if (!d.releases.length) { root.appendChild(el("p", { "class": "meta", text: T("rel_empty") })); }
    d.releases.forEach(function (r) {
      var card = el("section", { "class": "card", "data-code": String(r.version_code) }, [
        el("h2", { text: T("rel_version", { n: r.version_name, c: r.version_code }) }),
        el("p", { "class": "rel-state s-" + r.state, text: T("rel_state_" + r.state) + (r.state === "published" && r.published_at ? " · " + when(r.published_at) : "") }),
        el("p", { text: r.notes }),
        el("p", { "class": "meta", text: T("rel_size", { s: (r.size / 1048576).toFixed(1) }) + " · " + T("rel_added", { who: r.added_by || "?", t: when(r.added_at) }) }),
        el("p", { "class": "sha", text: r.sha256 })]);
      if (r.state === "pending") {
        var min = el("input", { type: "number", min: "0", max: String(r.version_code), inputmode: "numeric", placeholder: T("rel_min_ph"), "aria-label": T("rel_min") });
        card.appendChild(el("label", {}, [T("rel_min"), min]));
        card.appendChild(el("button", { type: "button", "class": "primary big-publish", "data-act": "publish", "data-code": String(r.version_code), text: T("rel_publish") }));
      }
      root.appendChild(card);
    });
  }
  function load() { api("GET", "/v1/releases").then(function (r) { if (!r.ok) { say(errText(r.json && r.json.error), "err"); return; } draw(r.json); }); }
  root.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-act]");
    if (!b || b.disabled) { return; }
    var act = b.dataset.act;
    if (act === "publish") {
      var card = b.closest("[data-code]"), code = b.dataset.code, minEl = card.querySelector("input"), body = {};
      if (minEl && minEl.value.trim() !== "") { body.min_supported_version_code = parseInt(minEl.value, 10); }
      if (!window.confirm(T("rel_confirm_publish", { c: code }))) { return; }
      b.disabled = true;
      api("POST", "/v1/releases/" + code + "/publish", body).then(function (r) { b.disabled = false; say(r.ok ? T("rel_published_ok") : errText(r.json && r.json.error), r.ok ? "" : "err"); load(); });
    } else if (act === "stop" || act === "resume") {
      if (act === "stop" && !window.confirm(T("rel_confirm_stop"))) { return; }
      b.disabled = true;
      api("POST", "/v1/releases/" + act).then(function (r) { b.disabled = false; say(r.ok ? T(act === "stop" ? "rel_stopped_ok" : "rel_resumed_ok") : errText(r.json && r.json.error), r.ok ? "" : "err"); load(); });
    }
  });
  load();
}());

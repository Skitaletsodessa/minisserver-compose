"""Adding and removing a child DEVICE from the portal (Task 29b; admin only).

The portal never touches the router or the device: it creates the policy (the schedule service's `child_window` entity -> the host job's snapshot -> AdGuard's rules and
client entry) and VERIFIES it before it reports success; anything it cannot verify is rolled back. A person still has to do the router/device steps: the setup checklist
says which and checks what can be checked.

  add      validate -> create in the service -> wait for the host job (the catch-all is active for the new IP) -> verify (an ordinary name blocked, an always-on name
           allowed, the AdGuard client entry has the child flags, the applied rules equal the expected ones, the OTHER children's rules did not move) -> the replica and the
           parity checker agree -> ANY failure: the device is removed again (and its client entry), nothing is left behind, the reason is shown
  remove   confirm by typing the device name -> remove in the service (the definition is kept 24 h for the undo) -> wait for the job (the IP is an ordinary client again) ->
           delete the AdGuard client entry (the sync carries it to the replica) -> verify the other children did not move -> parity
  restore  the undo of a removal within 24 h, verified like an add (and removed again if the verification fails)

One change at a time. Nothing about the children's browsing is stored.
"""
import asyncio
import json
import os
import re
import time

from . import agh as aghmod
from .auth import GwError
from .parental import _vendor_of, device_kind

INFRA = {"192.168.31.1": "роутер", "192.168.31.2": "сервер", "192.168.31.5": "Raspberry Pi (реплика DNS)"}
ALWAYS_ON = "api.telegram.org"
NEVER_ALLOWED = "never-allowed.example.net"
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
KNOWN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_devices.json")


class Refused(Exception):
    def __init__(self, status, code, message, **details):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


class ChildDevices:
    def __init__(self, parental, agh, db, clock=time.time, *, arp_file="/proc/net/arp", known_file=KNOWN_FILE, oui_file="/ro/ieee-data/oui.txt", apply_timeout_s=150.0,
                 poll_s=3.0, sleep=None):
        self.p, self.agh, self.db, self.clock = parental, agh, db, clock
        self.arp_file, self.oui_file = arp_file, oui_file
        self.apply_timeout_s, self.poll_s = apply_timeout_s, poll_s
        self.sleep = sleep or asyncio.sleep
        try:
            with open(known_file, encoding="utf-8") as f:
                self.known = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
        except (OSError, ValueError):
            self.known = {}
        self._busy = False

    # ── what the machine can see ──────────────────────────────────────────────────────────────────────────────────
    def _arp(self):
        out = {}
        try:
            with open(self.arp_file, encoding="utf-8") as f:
                next(f, None)
                for line in f:
                    c = line.split()
                    if len(c) >= 6 and c[2] == "0x2" and c[0].startswith("192.168.31.") and c[3] != "00:00:00:00:00:00":
                        out[c[0]] = c[3].lower()
        except OSError:
            pass
        return out

    async def _children(self, actor):
        return await self.p._children_raw(actor)

    async def candidates(self, actor):
        """LAN devices that are not children, not infrastructure and not gear the portal already knows. Suggestions only: nothing is applied by listing."""
        kids = await self._children(actor)
        taken = {c["ip"] for c in kids}
        arp = self._arp()
        host = await self.p._agh_clients()
        out = []
        for ip, mac in sorted(arp.items(), key=lambda kv: tuple(int(x) for x in kv[0].split("."))):
            if ip in taken or ip in INFRA or ip in self.known:
                continue
            vendor, rnd = _vendor_of(mac, self.oui_file)
            last, q, _b = await self.p._today(ip)
            out.append({"ip": ip, "mac": mac, "vendor": vendor, "randomised_mac": rnd, "hostname": host.get(ip), "last_query_at": last, "queries_today": q,
                        "device_kind": device_kind(host.get(ip) or vendor or ""), "warnings": ["randomised_mac"] if rnd else []})
        out.sort(key=lambda c: (-(c["queries_today"] or 0), c["ip"]))
        return {"candidates": out, "excluded": {"children": len(taken), "infrastructure": len(INFRA), "known_devices": len(self.known)},
                "note": "Это только подсказки: ничего не применяется, пока вы не добавите устройство."}

    # ── AdGuard facts ─────────────────────────────────────────────────────────────────────────────────────────────
    async def _lines_of(self, ips):
        st = await self.agh.call("GET", "/filtering/status")
        return [x for x in st.get("user_rules") or [] if any(x.endswith("$client=" + ip) for ip in ips)]

    async def _client_entry(self, ip, name):
        d = await self.agh.call("GET", "/clients")
        for c in d.get("clients") or []:
            if ip in (c.get("ids") or []) or c.get("name") == name:
                return c
        return None

    async def _verdict(self, name, ip):
        c = await self.agh.check_host(name, ip)
        return "blocked" if aghmod.filtered(c) else "allowed"

    async def _wait(self, cond, timeout):
        end = self.clock() + timeout
        while True:
            try:
                if await cond():
                    return True
            except aghmod.AghError:
                pass
            if self.clock() >= end:
                return False
            await self.sleep(self.poll_s)

    # ── verification shared by add and restore ───────────────────────────────────────────────────────────────────
    async def _verify_added(self, actor, ip, name, others, base_lines, started, steps):
        ok = await self._wait(lambda: self._is_blocked(ip), self.apply_timeout_s)
        steps.append({"name": "job", "ok": ok, "detail": "the host job applied the catch-all for %s" % ip if ok else "the host job did not apply the policy within %d s" % self.apply_timeout_s})
        if not ok:
            return False
        problems = []
        if await self._verdict(ALWAYS_ON, ip) != "allowed":
            problems.append("the always-on host %s is not allowed" % ALWAYS_ON)
        cl = await self._client_entry(ip, name)
        ss = (cl or {}).get("safe_search") or {}
        if not cl:
            problems.append("no AdGuard client entry for the device")
        elif cl.get("use_global_settings") is not False or not ss.get("enabled") or not ss.get("youtube"):
            problems.append("the client entry does not carry the child settings (own settings, Safe Search, restricted YouTube)")
        steps.append({"name": "verify", "ok": not problems, "detail": "; ".join(problems) if problems else "an ordinary name is blocked, the always-on host is allowed, the client entry has the child flags"})
        if problems:
            return False
        self.p._cache.pop("policy", None)
        kids = await self._children(actor)
        pol = await self.p._policy(kids)
        now_lines = await self._lines_of(others)
        same = now_lines == base_lines
        policy_ok = pol["in_sync"] is True and all(pol["per_ip"].get(i) for i in others + [ip])
        good = policy_ok
        detail = ("the applied rules equal the expected ones; the other children's rules are byte-identical" if same and policy_ok else
                  "the applied rules equal the expected ones; the other children's rules changed with the clock, not by this change" if policy_ok else
                  "the applied rules do not equal the expected ones (hash %s vs %s)" % (pol["applied_hash"], pol["expected_hash"]))
        steps.append({"name": "rules", "ok": good, "detail": detail})
        if not good:
            return False
        fl = self.p.filters
        return await fl._wait_parity(started, steps) if fl is not None else True

    async def _is_blocked(self, ip):
        return await self._verdict(NEVER_ALLOWED, ip) == "blocked"

    async def _is_ordinary(self, ip):
        return await self._verdict(NEVER_ALLOWED, ip) == "allowed"

    async def _delete_client(self, ip, name, steps):
        cl = await self._client_entry(ip, name)
        if not cl:
            steps.append({"name": "client", "ok": True, "detail": "no AdGuard client entry to delete"})
            return True
        try:
            await self.agh.call("POST", "/clients/delete", {"name": cl["name"]})
        except aghmod.AghError as e:
            steps.append({"name": "client", "ok": False, "detail": "AdGuard refused to delete the client entry: %s" % e.message[:120]})
            return False
        gone = await self._wait(lambda: self._no_client(ip, name), 20)
        steps.append({"name": "client", "ok": gone, "detail": "the AdGuard client entry was deleted on the origin (the sync carries it to the replica)" if gone else "the client entry is still there"})
        return gone

    async def _no_client(self, ip, name):
        return await self._client_entry(ip, name) is None

    # ── add ───────────────────────────────────────────────────────────────────────────────────────────────────────
    def _checks(self, body, kids):
        name, ip = (body.get("name") or "").strip(), (body.get("ip") or "").strip()
        label = (body.get("label") or "").strip()
        if not name:
            raise Refused(400, "bad_body", "the child's name is required")
        if not IP_RE.match(ip) or any(int(x) > 255 for x in ip.split(".")):
            raise Refused(422, "bad_ip", "%r is not an IPv4 address" % ip)
        if ip in INFRA:
            raise Refused(422, "infrastructure_ip", "%s is %s: a catch-all there would cut DNS for the whole house" % (ip, INFRA[ip]), ip=ip)
        if not ip.startswith("192.168.31."):
            raise Refused(422, "not_lan", "%s is not an address of the house network (192.168.31.x)" % ip)
        if any(k["ip"] == ip for k in kids):
            raise Refused(422, "duplicate", "%s already belongs to %s" % (ip, next(k["name"] for k in kids if k["ip"] == ip)))
        template = body.get("template") or "closed"
        if template != "closed" and not (template.startswith("copy:") and any(k["id"] == template[5:] for k in kids)):
            raise Refused(422, "bad_template", "template must be `closed` or `copy:<an existing child device id>`")
        return name, label, ip, template

    def _warnings(self, ip, mac, vendor_rnd, arp):
        w = []
        if vendor_rnd:
            w.append({"code": "randomised_mac", "message": "У устройства случайный (приватный) MAC: он может меняться, и тогда резервация IP в роутере не сработает. Отключите случайный MAC для этой сети на устройстве."})
        seen = arp.get(ip)
        if mac and seen and mac.lower() != seen:
            w.append({"code": "mac_differs", "message": "По адресу %s сейчас отвечает другое устройство (MAC %s), не тот, что указан." % (ip, seen)})
        if not seen:
            w.append({"code": "not_seen", "message": "Адрес %s сейчас не виден в сети (устройство выключено или другой IP)." % ip})
        if ip in self.known:
            w.append({"code": "known_device", "message": "Этот адрес занят другим устройством дома: %s." % self.known[ip]})
        w.append({"code": "static_unverifiable", "message": "Постоянство IP проверить отсюда нельзя: убедитесь, что в роутере есть резервация (DHCP) для этого MAC."})
        return w

    async def add(self, actor, body):
        if self._busy:
            raise GwError(409, "device_busy", "another device change is running")
        self._busy = True
        steps = []
        try:
            kids = await self._children(actor)
            try:
                name, label, ip, template = self._checks(body, kids)
            except Refused as e:
                raise GwError(e.status, e.code, e.message, e.details)
            mac = (body.get("mac") or "").strip().lower()
            arp = self._arp()
            mac_eff = mac or arp.get(ip, "")
            _v, rnd = _vendor_of(mac_eff, self.oui_file)
            warnings = self._warnings(ip, mac, rnd, arp)
            others = [k["ip"] for k in kids]
            base = await self._lines_of(others)
            started = self.clock()
            payload = {"name": name, "ip": ip, "label": label, "icon": body.get("icon") or "other"}
            if mac_eff:
                payload["mac"] = mac_eff
            if template == "closed":
                payload["closed"] = True
            else:
                payload["like"] = template[5:]
            js, _hd = await self.p._svc(actor, "POST", "/v1/child", payload, expect=(201,), role="admin")
            cid, full = js["id"], js["name"]
            steps.append({"name": "create", "ok": True, "detail": "the device %s is in the schedule service (%s)" % (full, "no windows" if template == "closed" else "windows and lists copied from " + template[5:])})
            ok = False
            try:
                ok = await self._verify_added(actor, ip, full, others, base, started, steps)
            except Exception as e:                                  # noqa: BLE001 - any surprise rolls the device back
                steps.append({"name": "internal", "ok": False, "detail": "unexpected %s" % e.__class__.__name__})
            if ok:
                return self._finish(actor, "child_device_add", cid, {"result": "created", "child_id": cid, "steps": steps, "warnings": warnings})
            failed = next(s for s in steps if not s["ok"])
            await self._rollback(actor, cid, ip, full, steps)
            return self._finish(actor, "child_device_add", cid, {"result": "reverted", "child_id": None, "steps": steps, "warnings": warnings,
                                                                 "reason": "%s failed: %s" % (failed["name"], failed["detail"])}, ok=False)
        finally:
            self._busy = False

    async def _rollback(self, actor, cid, ip, name, steps):
        try:
            await self.p._svc(actor, "DELETE", "/v1/child/%s?forget=1" % cid, role="admin", expect=(200,))
            ok = await self._wait(lambda: self._is_ordinary(ip), self.apply_timeout_s)
            steps.append({"name": "revert", "ok": ok, "detail": "the device was removed again and the IP is an ordinary client" if ok else "the device was removed in the service, the job has not yet released the IP"})
            await self._delete_client(ip, name, steps)
        except GwError as e:
            steps.append({"name": "revert", "ok": False, "detail": "could not remove the device again: %s" % e.message})

    def _finish(self, actor, action, target, result, ok=True):
        self.db.audit(action, person=actor.get("person_id"), phone=actor.get("phone_id"), target=target, after={"result": result["result"], "failed": [s["name"] for s in result["steps"] if not s["ok"]]},
                      ok=ok, ip=actor.get("ip"))
        self.p._cache.pop("policy", None)
        return result

    # ── remove / restore ──────────────────────────────────────────────────────────────────────────────────────────
    async def remove(self, actor, cid, body):
        if self._busy:
            raise GwError(409, "device_busy", "another device change is running")
        self._busy = True
        steps = []
        try:
            kids = await self._children(actor)
            dev = next((k for k in kids if k["id"] == cid), None)
            if not dev:
                raise GwError(404, "not_found", "no such child device", {"id": cid})
            if (body.get("confirm") or "").strip() != dev["name"]:
                raise GwError(422, "confirm_mismatch", "type the exact name of the device to confirm: %s" % dev["name"], {"expected": dev["name"]})
            ip, name = dev["ip"], dev["name"]
            others = [k["ip"] for k in kids if k["id"] != cid]
            base = await self._lines_of(others)
            started = self.clock()
            rec, _hd = await self.p._svc(actor, "DELETE", "/v1/child/" + cid, role="admin", expect=(200,))
            steps.append({"name": "remove", "ok": True, "detail": "the device is removed from the policy; the definition is kept for 24 h (undo)"})
            ok = await self._wait(lambda: self._is_ordinary(ip), self.apply_timeout_s)
            steps.append({"name": "job", "ok": ok, "detail": "the IP is an ordinary client again (no catch-all)" if ok else "the host job has not released the IP within %d s" % self.apply_timeout_s})
            ok2 = await self._delete_client(ip, name, steps)
            self.p._cache.pop("policy", None)
            kids2 = await self._children(actor)
            pol = await self.p._policy(kids2)
            now_lines = await self._lines_of(others)
            same = now_lines == base
            good = pol["in_sync"] is True and all(pol["per_ip"].get(i) for i in others)
            steps.append({"name": "others", "ok": good, "detail": "the other children's rules are byte-identical" if same and good else
                          ("the other children's rules changed with the clock, not by this change" if good else "the other children's rules do not equal the expected ones")})
            ok3 = True
            if self.p.filters is not None and ok and ok2 and good:
                ok3 = await self.p.filters._wait_parity(started, steps)
            complete = ok and ok2 and good and ok3
            res = {"result": "removed" if complete else "removal_incomplete", "removal": rec, "steps": steps}
            return self._finish(actor, "child_device_remove", cid, res, ok=complete)
        finally:
            self._busy = False

    async def restore(self, actor, rid):
        if self._busy:
            raise GwError(409, "device_busy", "another device change is running")
        self._busy = True
        steps = []
        try:
            kids = await self._children(actor)
            others = [k["ip"] for k in kids]
            base = await self._lines_of(others)
            started = self.clock()
            js, _hd = await self.p._svc(actor, "POST", "/v1/child/removed/%s/restore" % rid, expect=(201,), role="admin")
            cid, ip, full = js["id"], js["ip"], js["name"]
            steps.append({"name": "create", "ok": True, "detail": "the device %s is back in the schedule service with its windows, lists and exceptions" % full})
            ok = False
            try:
                ok = await self._verify_added(actor, ip, full, others, base, started, steps)
            except Exception as e:                                  # noqa: BLE001
                steps.append({"name": "internal", "ok": False, "detail": "unexpected %s" % e.__class__.__name__})
            if ok:
                return self._finish(actor, "child_device_restore", cid, {"result": "created", "child_id": cid, "steps": steps, "warnings": []})
            failed = next(s for s in steps if not s["ok"])
            await self._rollback(actor, cid, ip, full, steps)
            return self._finish(actor, "child_device_restore", cid, {"result": "reverted", "child_id": None, "steps": steps, "warnings": [],
                                                                     "reason": "%s failed: %s" % (failed["name"], failed["detail"])}, ok=False)
        finally:
            self._busy = False

    async def removed(self, actor):
        js, _hd = await self.p._svc(actor, "GET", "/v1/child/removed", role="admin")
        return js

    # ── the setup checklist ───────────────────────────────────────────────────────────────────────────────────────
    async def checklist(self, actor, cid):
        c = await self.p._child_raw(actor, cid)
        ip, mac = c["ip"], (c.get("mac") or "")
        arp = self._arp()
        _v, rnd = _vendor_of(mac, self.oui_file)
        last, q, _b = await self.p._today(ip)
        seen_mac = arp.get(ip)
        items = [
            {"id": "dhcp_reservation", "kind": "instruction", "status": "unknown" if (mac and seen_mac == mac.lower()) else ("warn" if seen_mac and mac and seen_mac != mac.lower() else "todo"),
             "title": "Резервация IP в роутере",
             "text": "В роутере (Xiaomi AX3600, веб-интерфейс) закрепите за MAC %s адрес %s, чтобы он не менялся. Портал это не делает и проверить резервацию не может." % (mac or "(MAC не указан)", ip),
             "detail": ("Сейчас по адресу %s отвечает именно это устройство." % ip) if mac and seen_mac == mac.lower() else
                       ("По адресу %s сейчас отвечает другое устройство (MAC %s)." % (ip, seen_mac) if seen_mac and mac else "Устройство сейчас не видно по этому адресу.")},
            {"id": "mac_randomisation", "kind": "live", "status": "warn" if rnd else ("ok" if mac else "unknown"), "title": "Случайный MAC выключен",
             "text": "На устройстве в настройках Wi-Fi этой сети выберите «Использовать MAC устройства» (не «случайный»), иначе MAC и резервация расходятся.",
             "detail": "Указанный MAC похож на случайный (приватный)." if rnd else ("MAC не случайный." if mac else "MAC не указан.")},
            {"id": "static_dns", "kind": "live", "status": "ok" if q else "todo", "title": "Статический IP и DNS 192.168.31.2 + 192.168.31.5",
             "text": "На устройстве включите статический IP (в Android поля DNS видны только так) и впишите DNS1 192.168.31.2, DNS2 192.168.31.5. При автоматических настройках (DHCP) детская политика молча не работает.",
             "detail": ("Запросы устройства приходят на наш DNS: сегодня %d, последний в %s." % (q, (last or "")[11:16])) if q else "Запросов от этого адреса сегодня не было: DNS на устройстве ещё не настроен или оно выключено.",
             "both_resolvers": "unknown"},
            {"id": "private_dns_vpn", "kind": "instruction", "status": "unknown", "title": "Частный DNS и VPN выключены",
             "text": "На устройстве выключите «Частный DNS» и не ставьте VPN-приложения: они обходят фильтр. Проверить это отсюда нельзя (пределы — в docs/dns-filtering.md §5)."},
            {"id": "bedtime", "kind": "note", "status": "info", "title": "Время сна — в Mi WiFi", "text": "Время сна и отключение экрана настраиваются в приложении Mi WiFi на роутере, не в портале."},
        ]
        return {"child": cid, "ip": ip, "items": items, "note": "Чек-лист только информирует: он ничего не блокирует и ничего не меняет на устройстве."}

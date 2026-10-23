"""gwctl - administration of the gateway, run INSIDE the container against its database:
    docker exec portal-gateway python -m gw.ctl COMMAND ...        (the host wrapper is /srv/compose/portal/gateway/gwctl)

NO command prints a secret. Anything secret (a setup link, an enrolment code) is written to a mode-600 file under /data/out/ and only the PATH
is printed; the person it is for opens or reads that file themselves, and removes it afterwards (`gwctl clean-out`).

  bootstrap                       first run: Ivan (admin, parent) and Dasha (household, parent, web login off); writes Ivan's one-time setup link
  person list | add NAME ROLE [--parent] | disable ID | enable ID | role ID ROLE | web-login ID on|off
  setup-link ID                   writes a fresh one-time setup link (password + TOTP) for a person with web login
  reset-credentials ID            break-glass for a locked-out admin: forget password/TOTP/sessions, then write a new setup link
  enrol-code ID                   writes a one-time enrolment code (10 min) to a file
  phones | phone-revoke ID
  audit [N] | verify              last N audit rows | verify the hash chain
  release pin SHA256              record the APK signing certificate the channel trusts (once; never changed from here)
  release add FILE --notes TEXT   verify an APK (package, signer = the pinned certificate, versionCode strictly higher, sound zip) and store it as PENDING
  release list | verify           the releases and their states | check the published manifest signature and the APK hash (exit 1 on a problem)
  clean-out                       delete the files under /data/out/
"""
import os
import sys
import glob
import time

from . import config, db as dbmod, people as peoplemod, releases as relmod
from .auth import GwError, fmt_code

OUT = os.path.join(config.DATA, "out")
ACTOR = {"person_id": "gwctl", "phone_id": None, "ip": "local"}


def write_secret_file(name, text):
    os.makedirs(OUT, mode=0o700, exist_ok=True)
    os.chmod(OUT, 0o700)
    path = os.path.join(OUT, name)
    if os.path.exists(path):
        os.remove(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text + "\n")
    return path


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 2
    db = dbmod.DB(config.DATA)
    P = peoplemod.People(db)
    cmd, a = argv[1], argv[2:]
    try:
        if cmd == "bootstrap":
            if P.list():
                print("already bootstrapped: %s" % ", ".join(p["id"] for p in P.list()))
                return 0
            P.create(ACTOR, "Ivan", "admin", parent=True)
            P.create(ACTOR, "Dasha", "household", parent=True, web_login=False)
            raw, _exp = P.create_setup_link(ACTOR, "ivan")
            path = write_secret_file("setup-ivan.url", "%s/setup/%s" % (config.PUBLIC_URL, raw))
            print("people: ivan (admin, parent), dasha (household, parent, web login off)")
            print("Ivan's one-time setup link (valid %d min) was written to %s (mode 600); open it in your browser, set your own password and scan the TOTP QR. Then run: gwctl clean-out" % (config.SETUP_TTL_S // 60, path))
        elif cmd == "person" and a:
            sub = a[0]
            if sub == "list":
                for p in P.list():
                    print("%-12s %-10s parent=%-5s active=%-5s web=%-5s creds=%-5s phones=%d" % (p["id"], p["role"], p["parent"], p["active"], p["web_login_enabled"], p["has_web_credentials"], p["phones"]))
            elif sub == "add" and len(a) >= 3:
                p = P.create(ACTOR, a[1], a[2], parent="--parent" in a)
                print("created", p["id"])
            elif sub in ("disable", "enable") and len(a) == 2:
                P.patch(ACTOR, a[1], {"active": sub == "enable"})
                print(sub + "d", a[1])
            elif sub == "role" and len(a) == 3:
                P.patch(ACTOR, a[1], {"role": a[2]})
                print("role of", a[1], "is now", a[2])
            elif sub == "web-login" and len(a) == 3 and a[2] in ("on", "off"):
                P.patch(ACTOR, a[1], {"web_login_enabled": a[2] == "on"})
                print("web login of", a[1], a[2])
            else:
                print(__doc__)
                return 2
        elif cmd in ("setup-link", "reset-credentials") and len(a) == 1:
            if cmd == "reset-credentials":
                P.reset_credentials(ACTOR, a[0])
            raw, _exp = P.create_setup_link(ACTOR, a[0])
            path = write_secret_file("setup-%s.url" % a[0], "%s/setup/%s" % (config.PUBLIC_URL, raw))
            print("one-time setup link (valid %d min) written to %s (mode 600)" % (config.SETUP_TTL_S // 60, path))
        elif cmd == "enrol-code" and len(a) == 1:
            code, _exp = P.create_enrolment_code(ACTOR, a[0])
            path = write_secret_file("enrol-%s.txt" % a[0], fmt_code(code))
            print("enrolment code (valid %d min, single use) written to %s (mode 600)" % (config.ENROL_TTL_S // 60, path))
        elif cmd == "phones":
            for ph in P.phones():
                print("%-10s %-10s %-24s revoked=%-5s last_seen=%s" % (ph["id"], ph["person_id"], ph["label"], ph["revoked"], ph["last_seen"]))
        elif cmd == "phone-revoke" and len(a) == 1:
            P.revoke_phone({"role": "admin", "person_id": "gwctl", "phone_id": None, "ip": "local"}, a[0])
            print("revoked", a[0])
        elif cmd == "audit":
            for r in reversed(db.audit_rows(int(a[0]) if a else 20)):
                print("#%d %s %s %s %s %s" % (r["seq"], r["ts"][:19], r["person"] or "-", r["action"], r["target"] or "", "ok" if r["ok"] else "FAILED"))
        elif cmd == "verify":
            ok, rows, bad = db.verify_audit()
            print("audit chain: %s over %d rows%s" % ("INTACT" if ok else "BROKEN", rows, "" if ok else " (first bad seq %s)" % bad))
            return 0 if ok else 1
        elif cmd == "release" and a:
            R = relmod.Releases(db, config.DATA, secrets_dir=config.SECRETS_DIR)
            sub = a[0]
            try:
                if sub == "pin" and len(a) == 2:
                    print("pinned", R.pin(a[1], ACTOR))
                elif sub == "add" and len(a) >= 2:
                    notes = a[a.index("--notes") + 1] if "--notes" in a and a.index("--notes") + 1 < len(a) else ""
                    info = R.add(a[1], notes, ACTOR)
                    print("stored as PENDING: %s (code %d), %.1f MB, sha256 %s. Approve it on /app/releases (web sign-in)." % (info["version_name"], info["version_code"], info["size"] / 1048576, info["sha256"]))
                elif sub == "list":
                    for r in R.list():
                        print("%-6d %-10s %-11s %6.1f MB  %s%s" % (r["version_code"], r["version_name"], r["state"], r["size"] / 1048576, r["sha256"][:16], "  <- served" if r["current"] and not R.stopped() else ("  (STOPPED)" if r["current"] else "")))
                    print("pinned certificate:", R.pinned_cert() or "-", "| channel", "STOPPED" if R.stopped() else "on")
                elif sub == "verify":
                    h = R.health()
                    print("published:", h["published"], "| stopped:", h["stopped"], "| pending:", h["pending"], "| signing key present:", h["key_ok"], "| manifest signature ok:", h["manifest_ok"], "| apk hash ok:", h["apk_ok"])
                    return 0 if h["key_ok"] and h["manifest_ok"] is not False and h["apk_ok"] is not False else 1
                else:
                    print(__doc__)
                    return 2
            except relmod.ReleaseError as e:
                print("refused: %s (%s)" % (e.message, e.code))
                return 1
        elif cmd == "clean-out":
            n = 0
            for f in glob.glob(os.path.join(OUT, "*")):
                os.remove(f)
                n += 1
            print("removed %d file(s) from %s" % (n, OUT))
        else:
            print(__doc__)
            return 2
    except GwError as e:
        print("refused: %s (%s)" % (e.message, e.code))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""Bootstrap and break-glass administration, run INSIDE the container against the database:
   docker exec portal-schedules python -m sched.admin token-create NAME ROLE OUTFILE [--gateway]
The secret is written to OUTFILE (mode 600), never printed."""
import os
import sys
from . import store

DATA = os.environ.get("PORTAL_DATA", "/data")


def main(argv):
    gateway = "--gateway" in argv
    argv = [a for a in argv if a != "--gateway"]
    if len(argv) == 5 and argv[1] == "token-create":
        _, _, name, role, outfile = argv
        st = store.Store(DATA)
        tid, secret = st.create_token({"token": "-", "name": "admin-cli", "role": "admin"}, name, role, gateway=gateway)
        fd = os.open(outfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret + "\n")
        print("token %s (%s, %s%s) written to %s" % (tid, name, role, ", gateway" if gateway else "", outfile))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

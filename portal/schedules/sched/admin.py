"""Bootstrap and break-glass administration, run INSIDE the container against the database:
   docker exec portal-schedules python -m sched.admin token-create NAME ROLE OUTFILE
The secret is written to OUTFILE (mode 600), never printed."""
import os
import sys
from . import store

DATA = os.environ.get("PORTAL_DATA", "/data")


def main(argv):
    if len(argv) == 5 and argv[1] == "token-create":
        _, _, name, role, outfile = argv
        st = store.Store(DATA)
        tid, secret = st.create_token({"token": "-", "name": "admin-cli", "role": "admin"}, name, role)
        fd = os.open(outfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret + "\n")
        print("token %s (%s, %s) written to %s" % (tid, name, role, outfile))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

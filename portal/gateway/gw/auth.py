"""Authentication primitives: scrypt passwords, RFC 6238 TOTP, random tokens, constant-time comparison. Standard library only."""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD = 12


class GwError(Exception):
    """An API error: HTTP status, stable code, message, details (never a secret)."""
    def __init__(self, status, code, message, details=None, headers=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details, self.headers = status, code, message, details or {}, headers or {}


def sha(s):
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


def token(nbytes=32):
    return base64.urlsafe_b64encode(secrets.token_bytes(nbytes)).decode().rstrip("=")


def eq(a, b):
    return hmac.compare_digest(a.encode() if isinstance(a, str) else a, b.encode() if isinstance(b, str) else b)


# ── passwords ─────────────────────────────────────────────────────────────────
def hash_password(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(h).decode())


def verify_password(pw, stored):
    try:
        _s, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def burn_password_time(pw):
    """Unknown account: spend the same time as a real verification so the response time does not reveal which accounts exist."""
    hashlib.scrypt(pw.encode(), salt=b"\x00" * 16, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)


def password_problem(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        return "the password must be at least %d characters" % MIN_PASSWORD
    if len(pw) > 200:
        return "the password is too long"
    return None


# ── TOTP (RFC 6238: HMAC-SHA1, 6 digits, 30 s) ────────────────────────────────
def new_totp_seed():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(seed_b32, counter):
    key = base64.b32decode(seed_b32 + "=" * (-len(seed_b32) % 8))
    d = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = d[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", d[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)


def totp_now(seed_b32, now=None, step=30):
    return _hotp(seed_b32, int((now if now is not None else time.time()) // step))


def verify_totp(seed_b32, code, last_step, now=None, step=30, window=1):
    """Returns the accepted time step, or None. A step already used (<= last_step) is refused: a code works once."""
    if not isinstance(code, str) or not code.isdigit() or len(code) != 6:
        return None
    cur = int((now if now is not None else time.time()) // step)
    for s in range(cur - window, cur + window + 1):
        if s > last_step and hmac.compare_digest(_hotp(seed_b32, s), code):
            return s
    return None


def otpauth_uri(seed_b32, account, issuer="Home Portal"):
    from urllib.parse import quote
    return "otpauth://totp/%s:%s?secret=%s&issuer=%s&algorithm=SHA1&digits=6&period=30" % (quote(issuer), quote(account), seed_b32, quote(issuer))


# ── codes the person types (enrolment): 12 base32 characters, grouped ─────────
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"        # no 0/O/1/I


def new_code():
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return raw


def fmt_code(raw):
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def norm_code(s):
    return "".join(ch for ch in (s or "").upper() if ch in _ALPHABET)

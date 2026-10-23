"""Ed25519 (RFC 8032) in pure Python: key generation from a 32-byte seed, signing, verification. No dependency: the gateway image has no crypto library and one signature per
published release does not need speed (a signature takes a few milliseconds here). This is the reference algorithm of RFC 8032 section 6, checked in tests/test_releases.py
against the RFC's own test vectors, against OpenSSL, and (the app side) against java.security's Ed25519 in the Android unit tests.
Signing is deterministic. The seed is the private key: it never leaves the file that holds it."""
import hashlib

P = 2 ** 255 - 19
D = -121665 * pow(121666, P - 2, P) % P
Q = 2 ** 252 + 27742317777372353535851937790883648493
_SQRT_M1 = pow(2, (P - 1) // 4, P)


def _sha512(s):
    return hashlib.sha512(s).digest()


def _inv(x):
    return pow(x, P - 2, P)


def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % P, (a[1] + a[0]) * (b[1] + b[0]) % P
    C, D_ = 2 * a[3] * b[3] * D % P, 2 * a[2] * b[2] % P
    E, F, G_, H = B - A, D_ - C, D_ + C, B + A
    return (E * F % P, G_ * H % P, F * G_ % P, E * H % P)


def _mul(s, pt):
    r = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        s >>= 1
    return r


def _equal(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % P == 0 and (a[1] * b[2] - b[1] * a[2]) % P == 0


def _recover_x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(D * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * _SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


_GY = 4 * _inv(5) % P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % P)


def _compress(pt):
    zinv = _inv(pt[2])
    x, y = pt[0] * zinv % P, pt[1] * zinv % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _expand(seed):
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _h_modq(s):
    return int.from_bytes(_sha512(s), "little") % Q


def public_key(seed):
    a, _ = _expand(seed)
    return _compress(_mul(a, _G))


def sign(seed, msg):
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, _G))
    r = _h_modq(prefix + msg)
    rs = _compress(_mul(r, _G))
    h = _h_modq(rs + pub + msg)
    s = (r + h * a) % Q
    return rs + int.to_bytes(s, 32, "little")


def verify(pub, msg, sig):
    """True only for a valid signature; never raises on malformed input."""
    try:
        if len(pub) != 32 or len(sig) != 64:
            return False
        A = _decompress(pub)
        R = _decompress(sig[:32])
        if A is None or R is None:
            return False
        s = int.from_bytes(sig[32:], "little")
        if s >= Q:
            return False
        h = _h_modq(sig[:32] + pub + msg)
        return _equal(_mul(s, _G), _add(R, _mul(h, A)))
    except (ValueError, TypeError):
        return False

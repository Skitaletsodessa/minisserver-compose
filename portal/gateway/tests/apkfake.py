"""Builds tiny but structurally real APKs for the update-channel tests: a zip with a compiled (binary) AndroidManifest.xml and an APK Signing Block (v2 layout) that
carries a certificate. Nothing here is signed cryptographically - the gateway's check is structural (see gw/apkinfo.py) and so are these files."""
import hashlib
import io
import os
import struct
import zipfile


def _u8(s):
    b = s.encode()
    assert len(s) < 128 and len(b) < 128
    return bytes([len(s), len(b)]) + b + b"\0"


def axml(package, version_code, version_name):
    strings = ["versionCode", "versionName", "package", "manifest", package, version_name]
    enc = [_u8(s) for s in strings]
    offsets, o = [], 0
    for e in enc:
        offsets.append(o)
        o += len(e)
    blob = b"".join(enc)
    blob += b"\0" * ((-len(blob)) % 4)
    n = len(strings)
    start = 28 + 4 * n
    pool = struct.pack("<HHIIIIII", 0x0001, 28, start + len(blob), n, 0, 0x100, start, 0) + b"".join(struct.pack("<I", x) for x in offsets) + blob
    attrs = [(-1, 0, -1, 0x10, version_code), (-1, 1, 5, 0x03, 5), (-1, 2, 4, 0x03, 4)]
    elem = struct.pack("<HHIII", 0x0102, 16, 16 + 20 + 20 * len(attrs), 1, 0xFFFFFFFF) + struct.pack("<iiHHHHHH", -1, 3, 20, 20, len(attrs), 0, 0, 0)
    for ns, name, raw, dtype, data in attrs:
        elem += struct.pack("<iiiHBBI", ns, name, raw, 8, 0, dtype, data)
    body = pool + elem
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


def _lp(b):
    return struct.pack("<I", len(b)) + b


def signing_block(certs_der, scheme="v2"):
    signed = _lp(b"") + _lp(b"".join(_lp(c) for c in certs_der)) + _lp(b"")
    signer = _lp(signed) + _lp(b"") + _lp(b"")
    value = _lp(_lp(signer))
    pid = 0x7109871A if scheme == "v2" else 0xF05368C0
    pair = struct.pack("<QI", 4 + len(value), pid) + value
    size = len(pair) + 8 + 16
    return struct.pack("<Q", size) + pair + struct.pack("<Q", size) + b"APK Sig Block 42"


def make_apk(path, package="home.minis.portal", version_code=4, version_name="1.1.0", certs=(b"cert-A",), extra=b"", scheme="v2", with_block=True, manifest=True):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_STORED) as z:
        if manifest:
            z.writestr("AndroidManifest.xml", axml(package, version_code, version_name))
        z.writestr("classes.dex", os.urandom(2048) + extra)
    data = bio.getvalue()
    if with_block:
        eocd = data.rfind(b"PK\x05\x06")
        cd = struct.unpack_from("<I", data, eocd + 16)[0]
        blk = signing_block(list(certs), scheme)
        data = data[:cd] + blk + data[cd:eocd] + data[eocd:eocd + 16] + struct.pack("<I", cd + len(blk)) + data[eocd + 20:]
    with open(path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()


def fp(cert):
    return hashlib.sha256(cert).hexdigest()

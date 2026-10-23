"""What the gateway can read out of an APK without any Android tooling (pure Python, stdlib): the package name, versionCode and versionName from the binary AndroidManifest.xml,
and the SHA-256 fingerprints of the signing certificates from the APK Signing Block (schemes v2 and v3, which the app's builds use).

WHAT THIS IS AND IS NOT. It reads the *claimed* signer certificate(s) and the version; it does NOT verify the signature over the contents (that needs RSA/ECDSA verification
and the digest of the whole file). The real enforcement is on the phone: Android refuses to install an update whose signature does not verify or whose certificate differs from the
installed app's, and the app itself compares the downloaded file's certificate with its own before it hands the file over (see docs/portal/adr/0010-update-channel.md). The server
check catches the operator's mistakes (a debug build, a build signed with another key, a wrong file) at `gwctl release add`, before anything can be published."""
import hashlib
import struct
import zipfile

SIG_MAGIC = b"APK Sig Block 42"
ID_V2, ID_V3 = 0x7109871A, 0xF05368C0
MAX_MANIFEST = 4 * 1024 * 1024


class ApkError(Exception):
    pass


# ── binary AndroidManifest.xml (AXML) ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _string_pool(data, off):
    typ, hsize, size = struct.unpack_from("<HHI", data, off)
    count, _styles, flags, strings_start = struct.unpack_from("<IIII", data, off + 8)
    utf8 = bool(flags & 0x100)
    offs = struct.unpack_from("<%dI" % count, data, off + hsize)
    base = off + strings_start
    out = []
    for o in offs:
        p = base + o
        if utf8:
            n = data[p]; p += 1
            if n & 0x80:
                n = ((n & 0x7F) << 8) | data[p]; p += 1
            n = data[p]; p += 1
            if n & 0x80:
                n = ((n & 0x7F) << 8) | data[p]; p += 1
            out.append(data[p:p + n].decode("utf-8", "replace"))
        else:
            n = struct.unpack_from("<H", data, p)[0]; p += 2
            if n & 0x8000:
                n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0]; p += 2
            out.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
    return out, off + size


def parse_manifest(data):
    """Returns {"package", "versionCode", "versionName"} from a compiled AndroidManifest.xml."""
    if len(data) < 8 or struct.unpack_from("<H", data, 0)[0] != 0x0003:
        raise ApkError("AndroidManifest.xml is not a compiled XML file")
    total = struct.unpack_from("<I", data, 4)[0]
    off = struct.unpack_from("<H", data, 2)[0]
    strings = []
    result = {}
    while off + 8 <= min(len(data), total):
        typ, hsize, size = struct.unpack_from("<HHI", data, off)
        if size < 8:
            break
        if typ == 0x0001:
            strings, _ = _string_pool(data, off)
        elif typ == 0x0102 and strings:
            ns, name, attr_start, attr_size, attr_count = struct.unpack_from("<iiHHH", data, off + 16)
            if strings[name] == "manifest":
                p = off + 16 + attr_start
                for _i in range(attr_count):
                    _ans, aname, raw, _vsize, _res0, dtype, vdata = struct.unpack_from("<iiiHBBI", data, p)
                    key = strings[aname]
                    if key == "versionCode" and dtype == 0x10:
                        result["versionCode"] = vdata
                    elif key in ("package", "versionName"):
                        result[key] = strings[raw] if raw >= 0 else None
                    p += attr_size
                break
        off += size
    if "versionCode" not in result or not result.get("package"):
        raise ApkError("versionCode or package not found in AndroidManifest.xml")
    result.setdefault("versionName", "")
    return result


# ── APK Signing Block ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def _central_directory_offset(f, size):
    tail = min(size, 65557)
    f.seek(size - tail)
    buf = f.read(tail)
    i = buf.rfind(b"PK\x05\x06")
    if i < 0:
        raise ApkError("not a zip file (no end-of-central-directory record)")
    return struct.unpack_from("<I", buf, i + 16)[0]


def _lp(data, off):
    n = struct.unpack_from("<I", data, off)[0]
    return data[off + 4:off + 4 + n], off + 4 + n


def signer_certs(path):
    """SHA-256 fingerprints (lower-case hex) of every certificate found in the v2 and v3 signers. Raises ApkError when the file has no signing block."""
    import os
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        cd = _central_directory_offset(f, size)
        if cd < 32:
            raise ApkError("no APK signing block")
        f.seek(cd - 24)
        tail = f.read(24)
        if tail[8:] != SIG_MAGIC:
            raise ApkError("no APK signing block (an unsigned or v1-only APK)")
        block_size = struct.unpack_from("<Q", tail, 0)[0]
        if block_size < 32 or block_size > cd:
            raise ApkError("a corrupt APK signing block")
        f.seek(cd - block_size - 8)
        block = f.read(block_size + 8)
    pairs, off, end = {}, 8, len(block) - 24
    while off + 12 <= end:
        ln = struct.unpack_from("<Q", block, off)[0]
        pid = struct.unpack_from("<I", block, off + 8)[0]
        if ln < 4 or off + 8 + ln > end + 0:
            break
        pairs.setdefault(pid, block[off + 12:off + 8 + ln])
        off += 8 + ln
    certs = []
    for pid in (ID_V2, ID_V3):
        if pid not in pairs:
            continue
        try:
            signers, _ = _lp(pairs[pid], 0)
            p = 0
            while p < len(signers):
                signer, p = _lp(signers, p)
                signed_data, _q = _lp(signer, 0)
                _digests, q = _lp(signed_data, 0)
                cert_seq, _q = _lp(signed_data, q)
                c = 0
                while c < len(cert_seq):
                    der, c = _lp(cert_seq, c)
                    certs.append(hashlib.sha256(der).hexdigest())
        except (struct.error, IndexError):
            raise ApkError("a corrupt APK signing block")
    if not certs:
        raise ApkError("no v2/v3 signature in the APK")
    return certs


def inspect(path):
    """The whole check used by `gwctl release add`: a sound zip, the manifest's identity, the signer fingerprints, the file's SHA-256 and size."""
    import os
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ApkError("not a zip/APK file: %s" % e)
    with z:
        bad = z.testzip()
        if bad:
            raise ApkError("the file is damaged (CRC error in %s)" % bad)
        try:
            info = z.getinfo("AndroidManifest.xml")
        except KeyError:
            raise ApkError("no AndroidManifest.xml: not an APK")
        if info.file_size > MAX_MANIFEST:
            raise ApkError("AndroidManifest.xml is implausibly large")
        manifest = parse_manifest(z.read(info))
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"package": manifest["package"], "version_code": manifest["versionCode"], "version_name": manifest["versionName"],
            "certs": signer_certs(path), "sha256": h.hexdigest(), "size": os.path.getsize(path)}

package home.minis.portal

import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.security.Signature
import java.util.Base64

/**
 * The pure part of the app's own update channel (ADR 0010): reading and verifying the signed manifest, deciding whether it means an update, verifying the downloaded file.
 * Nothing here touches Android or the network, so every rejection path is a JVM unit test. The gateway signs the EXACT payload string (Ed25519); the app verifies the signature
 * over those bytes with one of the embedded public keys FIRST and only then reads a field.
 */
data class Manifest(
    val versionName: String, val versionCode: Int, val minSupported: Int, val apkUrl: String, val apkSha256: String, val apkSize: Long,
    val certSha256: String, val changelog: String, val publishedAt: String,
)

sealed class Parsed {
    class Ok(val manifest: Manifest) : Parsed()
    object NoUpdate : Parsed()                       // the gateway says there is nothing (none published, or the channel is stopped)
    class Bad(val reason: String) : Parsed()          // anything that is not a valid, signed, well-formed manifest: nothing is done with it
}

object ManifestVerifier {
    const val APP_ID = "home.minis.portal"
    const val MAX_APK_BYTES = 80L * 1024 * 1024
    private val URL_RE = Regex("^/app/releases/([0-9]{1,9})/app\\.apk$")
    private val SHA_RE = Regex("^[0-9a-f]{64}$")

    fun hexToBytes(h: String): ByteArray {
        require(h.length % 2 == 0)
        return ByteArray(h.length / 2) { h.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    }

    fun hex(b: ByteArray): String = b.joinToString("") { "%02x".format(it) }

    fun sha256Hex(b: ByteArray): String = hex(MessageDigest.getInstance("SHA-256").digest(b))

    private fun verifySignature(pub: ByteArray, msg: ByteArray, sig: ByteArray): Boolean = Ed25519.verify(pub, msg, sig)

    /** [keys] = the raw 32-byte public keys embedded in the app (the current one and a spare): the signature is accepted when ANY of them verifies it. */
    fun parse(body: String, keys: List<ByteArray>): Parsed {
        val top = try { JSONObject(body) } catch (e: JSONException) { return Parsed.Bad("not json") }
        if (top.optInt("schema", -1) != 1) return Parsed.Bad("unknown schema")
        if (top.has("update") && !top.optBoolean("update", true)) return Parsed.NoUpdate
        val payload = top.optString("payload", "")
        val sigB64 = top.optString("signature", "")
        if (payload.isEmpty() || sigB64.isEmpty()) return Parsed.Bad("missing payload or signature")
        val sig = try { Base64.getDecoder().decode(sigB64) } catch (e: IllegalArgumentException) { return Parsed.Bad("signature is not base64") }
        if (sig.size != 64) return Parsed.Bad("signature has the wrong length")
        val bytes = payload.toByteArray(Charsets.UTF_8)
        if (keys.none { it.size == 32 && verifySignature(it, bytes, sig) }) return Parsed.Bad("signature does not verify")
        return try {
            val p = JSONObject(payload)
            if (p.optInt("schema", -1) != 1 || p.optString("app_id") != APP_ID) return Parsed.Bad("not for this app")
            val code = p.getInt("version_code")
            val url = p.getString("apk_url")
            val m = URL_RE.matchEntire(url)
            if (m == null || m.groupValues[1].toInt() != code) return Parsed.Bad("apk_url does not belong to this version")
            val sha = p.getString("apk_sha256")
            val cert = p.getString("apk_signing_cert_sha256")
            val size = p.getLong("apk_size")
            val min = p.getInt("min_supported_version_code")
            if (!SHA_RE.matches(sha) || !SHA_RE.matches(cert)) return Parsed.Bad("malformed fingerprint")
            if (size <= 0 || size > MAX_APK_BYTES) return Parsed.Bad("implausible size")
            if (code <= 0 || min < 0 || min > code) return Parsed.Bad("implausible version numbers")
            Parsed.Ok(Manifest(p.getString("version_name"), code, min, url, sha, size, cert, p.optString("changelog", ""), p.optString("published_at", "")))
        } catch (e: JSONException) {
            Parsed.Bad("payload is malformed")
        }
    }
}

enum class Verdict { NONE, AVAILABLE, STALE }

object UpdateDecision {
    /** [lastSeen] = the highest version_code of any manifest this phone has already verified: a manifest below it is a rollback attempt (a stolen manifest key can only do that). */
    fun decide(installed: Int, lastSeen: Int, m: Manifest): Verdict = when {
        m.versionCode < lastSeen -> Verdict.STALE
        m.versionCode <= installed -> Verdict.NONE
        else -> Verdict.AVAILABLE
    }

    /** Below the minimum the app shows a blocking screen and no family data. */
    fun mustUpdate(installed: Int, minSupported: Int): Boolean = installed < minSupported
}

/** What the platform tells about signing certificates; faked in tests. */
interface ApkInspector {
    fun signerSha256(path: String): Set<String>?          // null = the file cannot be read as an APK
    fun installedSignerSha256(): Set<String>
}

sealed class FileVerdict {
    object Ok : FileVerdict()
    class Fail(val code: String) : FileVerdict()
}

object ApkVerifier {
    /** Verifies the downloaded file in this order: size, SHA-256, that it is an APK with signers, that its signing certificate EQUALS the installed app's (and the one the manifest
     *  names), and the compiled-in pinned fingerprint. Only then is it handed to the installer. */
    fun verify(file: File, m: Manifest, insp: ApkInspector, pinned: String?): FileVerdict {
        if (!file.isFile) return FileVerdict.Fail("missing")
        if (file.length() != m.apkSize) return FileVerdict.Fail("size")
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { s ->
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = s.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        if (ManifestVerifier.hex(md.digest()) != m.apkSha256) return FileVerdict.Fail("hash")
        val apk = insp.signerSha256(file.path)
        if (apk == null || apk.isEmpty()) return FileVerdict.Fail("not_an_apk")
        val installed = insp.installedSignerSha256()
        if (installed.isEmpty() || apk != installed) return FileVerdict.Fail("other_certificate")
        if (apk != setOf(m.certSha256)) return FileVerdict.Fail("manifest_certificate")
        if (pinned != null && apk != setOf(pinned)) return FileVerdict.Fail("pinned_certificate")
        return FileVerdict.Ok
    }
}

/** What the About screen and the forced screen say about the update state, in words. The state itself is kept in [UpdateState]. */
object UpdateTexts {
    fun errorText(code: String?): String? = when (code) {
        null, "" -> null
        "network" -> Texts.UPD_ERR_NETWORK
        "bad_manifest" -> Texts.UPD_ERR_MANIFEST
        "stale" -> Texts.UPD_ERR_STALE
        "size", "hash", "missing", "not_an_apk" -> Texts.UPD_ERR_FILE
        "other_certificate", "manifest_certificate", "pinned_certificate" -> Texts.UPD_ERR_CERT
        "blocked" -> Texts.UPD_ERR_BLOCKED
        "install" -> Texts.UPD_ERR_INSTALL
        "no_key" -> Texts.UPD_ERR_NOKEY
        else -> Texts.UPD_ERR_OTHER
    }
}

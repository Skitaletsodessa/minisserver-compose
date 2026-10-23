package home.minis.portal

import java.io.File
import java.io.IOException

/** What the update logic remembers between runs (SharedPreferences on the phone, a map in tests). Nothing here is secret. */
class UpdateState(private val kv: KeyValueStore) {
    var lastSeen: Int get() = kv.get("last_seen")?.toIntOrNull() ?: 0; set(v) = kv.put("last_seen", v.toString())
    var minSupported: Int get() = kv.get("min_supported")?.toIntOrNull() ?: 0; set(v) = kv.put("min_supported", v.toString())
    var lastCheckAt: Long get() = kv.get("last_check")?.toLongOrNull() ?: 0L; set(v) = kv.put("last_check", v.toString())
    var latestName: String? get() = kv.get("latest_name"); set(v) { if (v == null) kv.remove("latest_name") else kv.put("latest_name", v) }
    var latestCode: Int get() = kv.get("latest_code")?.toIntOrNull() ?: 0; set(v) = kv.put("latest_code", v.toString())
    var lastError: String? get() = kv.get("last_error"); set(v) { if (v == null) kv.remove("last_error") else kv.put("last_error", v) }
    var lastDetail: String? get() = kv.get("last_detail"); set(v) { if (v == null) kv.remove("last_detail") else kv.put("last_detail", v.take(120)) }      // the short technical reason behind lastError (for a diagnosis from a phone)
    var notificationAsked: Boolean get() = kv.get("notif_asked") == "1"; set(v) = kv.put("notif_asked", if (v) "1" else "0")
    var unknownSourcesExplained: Boolean get() = kv.get("unknown_explained") == "1"; set(v) = kv.put("unknown_explained", if (v) "1" else "0")
    var installingCode: Int get() = kv.get("installing")?.toIntOrNull() ?: 0; set(v) = kv.put("installing", v.toString())
}

sealed class CheckOutcome {
    object None : CheckOutcome()
    class Available(val manifest: Manifest) : CheckOutcome()
    class Failed(val code: String) : CheckOutcome()
}

/** The one place that downloads the APK (streamed to a file, bounded in size and time). Faked in tests. */
interface Downloader {
    @Throws(IOException::class)
    fun fetch(url: String, headers: Map<String, String>, dest: File, expectedSize: Long)
}

class UpdateCore(
    private val state: UpdateState,
    private val http: Http,
    private val keys: List<ByteArray>,
    private val installedCode: () -> Int,
    private val now: () -> Long,
    private val pinnedCert: String?,
) {
    private fun auth(key: String) = mapOf("Authorization" to "Bearer $key", "Accept" to "application/json")

    /** One check: the signed manifest from the gateway, verified, compared. Updates the remembered state; never throws. */
    fun check(base: String, updateKey: String?): CheckOutcome {
        if (updateKey.isNullOrEmpty()) return fail("no_key")
        val r = try {
            http.request("GET", "$base/app/update.json", auth(updateKey), null)
        } catch (e: Exception) {
            return fail("network")
        }
        if (r.status == 401) return fail("no_key")
        if (r.status != 200) return fail("network")
        state.lastCheckAt = now()
        return when (val p = ManifestVerifier.parse(r.body, keys)) {
            is Parsed.NoUpdate -> { state.lastError = null; CheckOutcome.None }
            is Parsed.Bad -> { state.lastDetail = p.reason; fail("bad_manifest") }
            is Parsed.Ok -> {
                val m = p.manifest
                when (UpdateDecision.decide(installedCode(), state.lastSeen, m)) {
                    Verdict.STALE -> fail("stale")
                    Verdict.NONE -> { remember(m); state.lastError = null; state.latestName = m.versionName; state.latestCode = m.versionCode; CheckOutcome.None }
                    Verdict.AVAILABLE -> { remember(m); state.lastError = null; state.latestName = m.versionName; state.latestCode = m.versionCode; CheckOutcome.Available(m) }
                }
            }
        }
    }

    private fun remember(m: Manifest) {
        if (m.versionCode > state.lastSeen) state.lastSeen = m.versionCode
        if (m.minSupported > state.minSupported) state.minSupported = m.minSupported
    }

    private fun fail(code: String): CheckOutcome.Failed { state.lastError = code; if (code != "bad_manifest") state.lastDetail = null; return CheckOutcome.Failed(code) }

    /** Downloads the APK of [m] into [dir] and verifies it (size, SHA-256, signer = the installed app's, pinned). Returns the file, or the failure code (the file is deleted). */
    fun fetchAndVerify(base: String, updateKey: String, m: Manifest, dir: File, dl: Downloader, insp: ApkInspector): Pair<File?, String?> {
        dir.mkdirs()
        dir.listFiles()?.forEach { it.delete() }                                                 // nothing stays from before: old versions, partial files, an earlier copy of this one
        val dest = File(dir, "portal-${m.versionCode}.apk")
        val part = File(dir, "portal-${m.versionCode}.apk.part")
        part.delete()
        try {
            dl.fetch(base + m.apkUrl, auth(updateKey), part, m.apkSize)
        } catch (e: Exception) {
            part.delete()
            state.lastError = "network"
            return Pair(null, "network")
        }
        val v = ApkVerifier.verify(part, m, insp, pinnedCert)
        if (v is FileVerdict.Fail) { part.delete(); state.lastError = v.code; return Pair(null, v.code) }
        if (!part.renameTo(dest)) { part.delete(); state.lastError = "install"; return Pair(null, "install") }
        return Pair(dest, null)
    }
}

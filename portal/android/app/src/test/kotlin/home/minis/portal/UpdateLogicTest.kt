package home.minis.portal

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.io.IOException
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.Signature
import java.util.Base64

/** The update channel's logic on the JVM: every rejection path ends with nothing installed. (The install itself is exercised on the emulator.) */
class UpdateLogicTest {
    // a manifest produced by the SERVER's pure-Python Ed25519 (seed = bytes 0..31): the two implementations must agree
    private val serverPub = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
    private val serverPayload = """{"apk_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","apk_signing_cert_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","apk_size":4500000,"apk_url":"/app/releases/5/app.apk","app_id":"home.minis.portal","changelog":"Что нового: тест","key_id":"k1","min_supported_version_code":4,"published_at":"2026-10-22T21:00:00+0200","schema":1,"version_code":5,"version_name":"1.1.1"}"""
    private val serverSig = "x8OGdyO4EA/UxjasyghGcR91/ppWTCiipyyqwgl0fn7XMZYEr1an8N8xYtv7EaUo4KVaoJ1CCpmRpX2YVqPjAA=="

    private fun manifestJson(payload: String, sig: String, schema: Int = 1) = JSONObject().put("schema", schema).put("key_id", "k1").put("payload", payload).put("signature", sig).toString()

    // a local key pair for the tamper tests (java.security's own Ed25519)
    private val kp = KeyPairGenerator.getInstance("Ed25519").generateKeyPair()
    private val pub: ByteArray = kp.public.encoded.takeLast(32).toByteArray()
    private val otherPub: ByteArray = KeyPairGenerator.getInstance("Ed25519").generateKeyPair().public.encoded.takeLast(32).toByteArray()

    private fun sign(payload: String): String {
        val s = Signature.getInstance("Ed25519").apply { initSign(kp.private); update(payload.toByteArray(Charsets.UTF_8)) }
        return Base64.getEncoder().encodeToString(s.sign())
    }

    private fun payload(code: Int = 5, name: String = "1.1.1", min: Int = 4, size: Long = 1000, sha: String = "a".repeat(64), cert: String = "b".repeat(64), url: String? = null, app: String = "home.minis.portal") =
        JSONObject().put("schema", 1).put("app_id", app).put("key_id", "k1").put("version_name", name).put("version_code", code).put("min_supported_version_code", min)
            .put("apk_url", url ?: "/app/releases/$code/app.apk").put("apk_sha256", sha).put("apk_size", size).put("apk_signing_cert_sha256", cert).put("changelog", "x").put("published_at", "t").toString()

    private fun parse(p: String, keys: List<ByteArray> = listOf(pub)) = ManifestVerifier.parse(manifestJson(p, sign(p)), keys)
    private fun bad(p: Parsed) = (p as? Parsed.Bad)?.reason

    @Test fun theServerSignedManifestVerifiesHere() {
        val r = ManifestVerifier.parse(manifestJson(serverPayload, serverSig), listOf(ManifestVerifier.hexToBytes(serverPub)))
        val m = (r as Parsed.Ok).manifest
        assertEquals(listOf("1.1.1", 5, 4, "/app/releases/5/app.apk", 4500000L), listOf(m.versionName, m.versionCode, m.minSupported, m.apkUrl, m.apkSize))
        assertEquals("Что нового: тест", m.changelog)
    }

    @Test fun aPayloadChangedByOneCharacterIsRejected() {
        for ((a, b) in listOf("1.1.1" to "1.1.2", "\"version_code\":5" to "\"version_code\":6", "4500000" to "4500001", "/app/releases/5/" to "/app/releases/6/", "тест" to "тесТ", "aaaa" to "aaab")) {
            val tampered = serverPayload.replaceFirst(a, b)
            assertTrue(a, tampered != serverPayload)
            assertEquals(a, "signature does not verify", bad(ManifestVerifier.parse(manifestJson(tampered, serverSig), listOf(ManifestVerifier.hexToBytes(serverPub)))))
        }
        assertEquals("signature does not verify", bad(ManifestVerifier.parse(manifestJson(serverPayload + " ", serverSig), listOf(ManifestVerifier.hexToBytes(serverPub)))))
    }

    @Test fun aWrongOrMalformedSignatureIsRejected() {
        val keys = listOf(ManifestVerifier.hexToBytes(serverPub))
        val flipped = Base64.getDecoder().decode(serverSig).also { it[10] = (it[10].toInt() xor 1).toByte() }
        assertEquals("signature does not verify", bad(ManifestVerifier.parse(manifestJson(serverPayload, Base64.getEncoder().encodeToString(flipped)), keys)))
        assertEquals("signature has the wrong length", bad(ManifestVerifier.parse(manifestJson(serverPayload, Base64.getEncoder().encodeToString(ByteArray(63))), keys)))
        assertEquals("signature is not base64", bad(ManifestVerifier.parse(manifestJson(serverPayload, "***"), keys)))
        assertEquals("missing payload or signature", bad(ManifestVerifier.parse("""{"schema":1}""", keys)))
        assertEquals("unknown schema", bad(ManifestVerifier.parse(manifestJson(serverPayload, serverSig, schema = 2), keys)))
        assertEquals("not json", bad(ManifestVerifier.parse("<html>", keys)))
        assertEquals("signature does not verify", bad(ManifestVerifier.parse(manifestJson(serverPayload, serverSig), listOf(otherPub))))      // another key
        assertEquals("signature does not verify", bad(ManifestVerifier.parse(manifestJson(serverPayload, serverSig), emptyList())))
    }

    @Test fun anyOfTheTwoEmbeddedKeysMayVerify() {
        val p = payload()
        assertTrue(parse(p, listOf(otherPub, pub)) is Parsed.Ok)
        assertTrue(parse(p, listOf(pub, otherPub)) is Parsed.Ok)
        assertEquals("signature does not verify", bad(parse(p, listOf(otherPub, ByteArray(31)))))
    }

    @Test fun aSignedButNonsensicalPayloadIsRejected() {
        assertEquals("not for this app", bad(parse(payload(app = "evil.app"))))
        assertEquals("apk_url does not belong to this version", bad(parse(payload(code = 5, url = "/app/releases/6/app.apk"))))
        assertEquals("apk_url does not belong to this version", bad(parse(payload(url = "https://evil.example/app.apk"))))
        assertEquals("apk_url does not belong to this version", bad(parse(payload(url = "/app/releases/5/../../x/app.apk"))))
        assertEquals("malformed fingerprint", bad(parse(payload(sha = "zz"))))
        assertEquals("malformed fingerprint", bad(parse(payload(cert = "A".repeat(64)))))
        assertEquals("implausible size", bad(parse(payload(size = 0))))
        assertEquals("implausible size", bad(parse(payload(size = ManifestVerifier.MAX_APK_BYTES + 1))))
        assertEquals("implausible version numbers", bad(parse(payload(code = 5, min = 6))))
        assertEquals("implausible version numbers", bad(parse(payload(code = 0, min = 0, url = "/app/releases/0/app.apk"))))
        assertEquals("payload is malformed", bad(parse("""{"schema":1,"app_id":"home.minis.portal","version_code":"x"}""")))
        assertEquals("not for this app", bad(parse("""{"schema":2,"app_id":"home.minis.portal"}""")))
    }

    @Test fun noUpdateAnswersAreNotAnError() {
        assertTrue(ManifestVerifier.parse("""{"schema":1,"update":false,"reason":"stopped"}""", listOf(pub)) is Parsed.NoUpdate)
        assertTrue(ManifestVerifier.parse("""{"schema":1,"update":false,"reason":"none"}""", listOf(pub)) is Parsed.NoUpdate)
    }

    private fun man(code: Int, min: Int = 0) = Manifest("v$code", code, min, "/app/releases/$code/app.apk", "a".repeat(64), 10, "b".repeat(64), "", "")

    @Test fun theDecision() {
        assertEquals(Verdict.AVAILABLE, UpdateDecision.decide(4, 4, man(5)))
        assertEquals(Verdict.NONE, UpdateDecision.decide(5, 5, man(5)))
        assertEquals(Verdict.NONE, UpdateDecision.decide(6, 6, man(6)))
        assertEquals(Verdict.STALE, UpdateDecision.decide(4, 6, man(5)))                  // below what was already verified: a rollback attempt
        assertEquals(Verdict.STALE, UpdateDecision.decide(6, 6, man(5)))
        assertEquals(Verdict.AVAILABLE, UpdateDecision.decide(4, 0, man(5)))
        assertTrue(UpdateDecision.mustUpdate(4, 5))
        assertFalse(UpdateDecision.mustUpdate(5, 5))
        assertFalse(UpdateDecision.mustUpdate(4, 0))
    }

    // ── the downloaded file ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    private class Insp(val apk: Set<String>?, val installed: Set<String>) : ApkInspector {
        override fun signerSha256(path: String) = apk
        override fun installedSignerSha256() = installed
    }

    private val tmp = File.createTempFile("upd", "dir").let { it.delete(); it.mkdirs(); it }

    private fun fileWith(bytes: ByteArray, name: String = "f.apk") = File(tmp, name).also { it.writeBytes(bytes) }
    private fun sha(b: ByteArray) = ManifestVerifier.hex(MessageDigest.getInstance("SHA-256").digest(b))
    private val body = ByteArray(5000) { (it * 7).toByte() }
    private val cert = "c".repeat(64)
    private fun mf(b: ByteArray = body, c: String = cert) = Manifest("1.1.1", 5, 0, "/app/releases/5/app.apk", sha(b), b.size.toLong(), c, "", "")

    @Test fun aGoodFilePasses() {
        assertTrue(ApkVerifier.verify(fileWith(body), mf(), Insp(setOf(cert), setOf(cert)), cert) is FileVerdict.Ok)
    }

    @Test fun everyFileRejection() {
        fun code(f: File, m: Manifest, i: ApkInspector, pinned: String? = cert) = (ApkVerifier.verify(f, m, i, pinned) as FileVerdict.Fail).code
        val ok = Insp(setOf(cert), setOf(cert))
        assertEquals("missing", code(File(tmp, "nope.apk"), mf(), ok))
        assertEquals("size", code(fileWith(body + 1), mf(), ok))
        assertEquals("hash", code(fileWith(ByteArray(5000)), mf(), ok))                                                  // right size, other bytes
        assertEquals("not_an_apk", code(fileWith(body), mf(), Insp(null, setOf(cert))))
        assertEquals("not_an_apk", code(fileWith(body), mf(), Insp(emptySet(), setOf(cert))))
        assertEquals("other_certificate", code(fileWith(body), mf(), Insp(setOf("d".repeat(64)), setOf(cert))))          // signed by another key
        assertEquals("other_certificate", code(fileWith(body), mf(), Insp(setOf(cert), emptySet())))
        assertEquals("other_certificate", code(fileWith(body), mf(), Insp(setOf(cert, "d".repeat(64)), setOf(cert))))
        assertEquals("manifest_certificate", code(fileWith(body), mf(c = "e".repeat(64)), ok))                            // the manifest names another certificate
        assertEquals("pinned_certificate", code(fileWith(body), mf(), ok, pinned = "f".repeat(64)))
    }

    // ── the whole check/download flow with a fake network ──────────────────────────────────────────────────────────────────────────────────────
    private class MapKv : KeyValueStore {
        val m = HashMap<String, String>()
        override fun get(key: String) = m[key]
        override fun put(key: String, value: String) { m[key] = value }
        override fun remove(key: String) { m.remove(key) }
        override fun clear() = m.clear()
    }

    private class FakeHttp(var status: Int = 200, var body: String = "", var throws: Boolean = false) : Http {
        var lastHeaders: Map<String, String> = emptyMap()
        override fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpResponse {
            lastHeaders = headers
            if (throws) throw IOException("down")
            return HttpResponse(status, this.body, emptyMap())
        }
    }

    private lateinit var kv: MapKv
    private lateinit var state: UpdateState
    private var installed = 4

    @Before fun setUp() { kv = MapKv(); state = UpdateState(kv); installed = 4 }

    private fun core(h: Http) = UpdateCore(state, h, listOf(pub), { installed }, { 1_700_000_000_000L }, cert)
    private fun signedFor(code: Int, min: Int = 0, b: ByteArray = body) = payload(code = code, name = "v$code", min = min, size = b.size.toLong(), sha = sha(b), cert = cert).let { manifestJson(it, sign(it)) }

    @Test fun aCheckThatFindsAnUpdateRemembersWhatItLearned() {
        val h = FakeHttp(body = signedFor(5, min = 4))
        val out = core(h).check("https://x", "ukey_abc")
        assertTrue(out is CheckOutcome.Available)
        assertEquals("Bearer ukey_abc", h.lastHeaders["Authorization"])
        assertEquals(listOf<Any?>(5, 4, 5, 1_700_000_000_000L, null), listOf<Any?>(state.lastSeen, state.minSupported, state.latestCode, state.lastCheckAt, state.lastError))
        assertEquals("v5", state.latestName)
    }

    @Test fun everyFailureOfACheckEndsWithNothingToInstall() {
        fun code(o: CheckOutcome) = (o as CheckOutcome.Failed).code
        assertEquals("no_key", code(core(FakeHttp()).check("https://x", null)))
        assertEquals("no_key", code(core(FakeHttp()).check("https://x", "")))
        assertEquals("network", code(core(FakeHttp(throws = true)).check("https://x", "k")))
        assertEquals("no_key", code(core(FakeHttp(status = 401)).check("https://x", "k")))                                  // a revoked key
        assertEquals("network", code(core(FakeHttp(status = 503)).check("https://x", "k")))
        assertEquals("bad_manifest", code(core(FakeHttp(body = "garbage")).check("https://x", "k")))
        val p = payload(code = 5)
        assertEquals("bad_manifest", code(core(FakeHttp(body = manifestJson(p, sign(p).reversed()))).check("https://x", "k")))
        assertEquals("bad_manifest", code(UpdateCore(state, FakeHttp(body = signedFor(5)), listOf(otherPub), { installed }, { 0L }, cert).check("https://x", "k")))
        state.lastSeen = 7
        assertEquals("stale", code(core(FakeHttp(body = signedFor(6))).check("https://x", "k")))                            // a rollback to an older published build
        assertEquals(7, state.lastSeen)                                                                                     // and it does not move what the phone has seen
        assertEquals("stale", state.lastError)
    }

    @Test fun noUpdateAndStoppedAreQuiet() {
        val h = FakeHttp(body = """{"schema":1,"update":false,"reason":"stopped"}""")
        assertTrue(core(h).check("https://x", "k") is CheckOutcome.None)
        assertNull(state.lastError)
        assertEquals(1_700_000_000_000L, state.lastCheckAt)
        assertEquals(0, state.latestCode)
    }

    @Test fun theMinimumOnlyGoesUp() {
        core(FakeHttp(body = signedFor(5, min = 5))).check("https://x", "k")
        assertEquals(5, state.minSupported)
        installed = 5
        core(FakeHttp(body = signedFor(6, min = 3))).check("https://x", "k")                                                // a lower minimum in a newer manifest does not lower it
        assertEquals(5, state.minSupported)
        assertTrue(UpdateDecision.mustUpdate(4, state.minSupported))
    }

    private class FakeDownloader(val bytes: ByteArray?, val fail: Boolean = false) : Downloader {
        var url = ""
        override fun fetch(url: String, headers: Map<String, String>, dest: File, expectedSize: Long) {
            this.url = url
            if (fail) { dest.writeBytes(ByteArray(10)); throw IOException("killed") }
            dest.writeBytes(bytes!!)
        }
    }

    private fun m5(b: ByteArray = body) = Manifest("v5", 5, 0, "/app/releases/5/app.apk", sha(b), b.size.toLong(), cert, "", "")

    @Test fun aDownloadIsVerifiedBeforeItIsKeptAndLeavesNothingBehindWhenItFails() {
        val dir = File(tmp, "upd1")
        dir.mkdirs()
        File(dir, "portal-3.apk").writeBytes(ByteArray(3))                                                                // an old leftover
        val ok = Insp(setOf(cert), setOf(cert))
        val c = core(FakeHttp())
        val dl = FakeDownloader(body)
        val (f, e) = c.fetchAndVerify("https://x", "k", m5(), dir, dl, ok)
        assertNull(e)
        assertEquals("https://x/app/releases/5/app.apk", dl.url)
        assertEquals(listOf("portal-5.apk"), dir.list()!!.toList())                                                        // the leftover and any partial file are gone
        assertNotNull(f)
        // killed mid-download: nothing kept
        val (f2, e2) = c.fetchAndVerify("https://x", "k", m5(), dir, FakeDownloader(null, fail = true), ok)
        assertEquals(Pair(null, "network"), Pair(f2, e2))
        assertEquals(emptyList<String>(), dir.list()!!.toList())
        // wrong bytes (a "man in the middle" with the right length)
        val (f3, e3) = c.fetchAndVerify("https://x", "k", m5(), dir, FakeDownloader(ByteArray(body.size)), ok)
        assertEquals(Pair(null, "hash"), Pair(f3, e3))
        assertEquals(emptyList<String>(), dir.list()!!.toList())
        // signed by another key
        val (f4, e4) = c.fetchAndVerify("https://x", "k", m5(), dir, FakeDownloader(body), Insp(setOf("d".repeat(64)), setOf(cert)))
        assertEquals(Pair(null, "other_certificate"), Pair(f4, e4))
        assertEquals(emptyList<String>(), dir.list()!!.toList())
        assertEquals("other_certificate", state.lastError)
    }

    @Test fun everyErrorCodeHasAPlainRussianText() {
        for (c in listOf("network", "bad_manifest", "stale", "size", "hash", "missing", "not_an_apk", "other_certificate", "manifest_certificate", "pinned_certificate", "blocked", "install", "no_key", "whatever")) {
            val t = UpdateTexts.errorText(c)
            assertNotNull(c, t)
            assertFalse(c, Regex("[A-Za-z]{4,}").containsMatchIn(t!!.replace("Android", "")))
        }
        assertNull(UpdateTexts.errorText(null))
    }

    @Test fun theBackgroundPeriodIsAboutSixHours() {
        assertEquals(6 * 3600 * 1000L, UpdateJob.PERIOD_MS)
    }
}

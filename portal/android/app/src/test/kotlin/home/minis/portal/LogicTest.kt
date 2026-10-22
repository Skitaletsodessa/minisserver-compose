package home.minis.portal

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class SignInFormTest {
    private fun v(name: String = "Иван", pw: String = "correct horse battery", totp: String = "", url: String = "") = SignInForm.validate(name, pw.toCharArray(), totp, url)

    @Test fun `a normal admin sign-in is valid and defaults the server`() {
        val r = v(totp = "123456")
        assertTrue(r.ok); assertEquals("123456", r.totp); assertEquals(SignInForm.DEFAULT_URL, r.baseUrl)
    }

    @Test fun `household may leave the code empty`() {
        val r = v(name = "Дарья")
        assertTrue(r.ok); assertNull(r.totp)
    }

    @Test fun `an empty name or password is refused`() {
        assertEquals(Texts.ERR_NAME_EMPTY, v(name = "  ").nameError)
        assertEquals(Texts.ERR_PASSWORD_EMPTY, v(pw = "").passwordError)
        assertFalse(v(pw = "").ok)
    }

    @Test fun `the code is exactly six digits`() {
        for (bad in listOf("12345", "1234567", "12345a", "١٢٣٤٥٦")) assertEquals(bad, Texts.ERR_TOTP_FORMAT, v(totp = bad).totpError)
        assertNull(v(totp = " 654321 ").totpError)
    }

    @Test fun `only an https origin without a path is a valid server address`() {
        assertEquals("https://minisserver.tail6bf4d5.ts.net:17443", SignInForm.normaliseUrl("https://minisserver.tail6bf4d5.ts.net:17443/"))
        assertEquals("https://example.test", SignInForm.normaliseUrl(" HTTPS://Example.Test "))
        for (bad in listOf("http://x.test", "https://x.test/path", "https://u:p@x.test", "https://x.test?a=1", "ftp://x.test", "x.test", "https://", "javascript:alert(1)")) assertNull(bad, SignInForm.normaliseUrl(bad))
        assertFalse(v(url = "http://192.168.31.2:17444").ok)
    }

    @Test fun `no message ever contains the password`() {
        val r = SignInForm.validate("", "s3cret-password".toCharArray(), "x", "http://bad")
        val all = listOfNotNull(r.nameError, r.passwordError, r.totpError, r.urlError).joinToString()
        assertFalse(all.contains("s3cret"))
    }
}

class UrlPolicyTest {
    private val base = "https://minisserver.tail6bf4d5.ts.net:17443"

    @Test fun `the gateway origin is allowed`() {
        assertEquals(UrlPolicy.Action.ALLOW, UrlPolicy.classify(base, "$base/home"))
        assertEquals(UrlPolicy.Action.ALLOW, UrlPolicy.classify(base, "https://MINISSERVER.tail6bf4d5.ts.net:17443/parental?x=1#y"))
    }

    @Test fun `another port, host or scheme is not the same origin`() {
        for (u in listOf("https://minisserver.tail6bf4d5.ts.net:9443/", "https://minisserver.tail6bf4d5.ts.net/", "https://evil.example/", "http://minisserver.tail6bf4d5.ts.net:17443/"))
            assertFalse(u, UrlPolicy.sameOrigin(base, u))
        assertFalse(UrlPolicy.sameOrigin(base, "https://minisserver.tail6bf4d5.ts.net:17443@evil.example/"))
        assertFalse(UrlPolicy.sameOrigin("http://x.test", "http://x.test/"))                      // a cleartext base is never an origin
    }

    @Test fun `other https sites go to the system browser, everything else is blocked`() {
        assertEquals(UrlPolicy.Action.EXTERNAL, UrlPolicy.classify(base, "https://minisserver.tail6bf4d5.ts.net:9443/web/"))
        for (u in listOf("http://evil.example/", "intent://x#Intent;end", "file:///data/data/x", "javascript:alert(1)", "data:text/html,hi", "content://x/y", "not a url", ""))
            assertEquals(u, UrlPolicy.Action.BLOCK, UrlPolicy.classify(base, u))
    }

    @Test fun `the about link is handled natively and never reaches the server`() {
        assertEquals(UrlPolicy.Action.ABOUT, UrlPolicy.classify(base, "$base/app/about"))
        assertEquals(UrlPolicy.Action.ALLOW, UrlPolicy.classify(base, "$base/app/manifest.webmanifest"))
    }
}

class LockPolicyTest {
    @Test fun `fresh launch is not locked by this policy (a new process starts locked by construction)`() = assertFalse(LockPolicy.shouldLock(null, 1_000))
    @Test fun `less than two minutes in the background keeps the session`() = assertFalse(LockPolicy.shouldLock(1_000, 1_000 + 119_999))
    @Test fun `two minutes or more locks`() { assertTrue(LockPolicy.shouldLock(1_000, 1_000 + 120_000)); assertTrue(LockPolicy.shouldLock(1_000, 1_000 + 600_000)) }
    @Test fun `a clock that went backwards locks`() = assertTrue(LockPolicy.shouldLock(5_000, 4_000))
}

class ErrorMapperTest {
    @Test fun `no connection offers Tailscale and retry`() {
        val s = ErrorMapper.forStartup(GatewayError.Network("x"))
        assertEquals(Texts.NET_TITLE, s.title); assertTrue(s.showTailscale && s.showRetry && !s.showSignIn)
    }

    @Test fun `a revoked token means signing in again`() {
        val s = ErrorMapper.forStartup(GatewayError.Revoked())
        assertTrue(s.showSignIn && !s.showRetry); assertEquals(Texts.RELOGIN_REVOKED, s.text)
    }

    @Test fun `a certificate problem has no Tailscale button and a plain explanation`() {
        val s = ErrorMapper.forStartup(GatewayError.Tls("x"))
        assertTrue(!s.showTailscale && s.showRetry); assertEquals(Texts.TLS_TEXT, s.text)
    }

    @Test fun `sign-in failures never say which part was wrong`() {
        assertEquals(Texts.LOGIN_BAD, ErrorMapper.forSignIn(GatewayError.BadLogin()))
        assertEquals(Texts.LOGIN_THROTTLED, ErrorMapper.forSignIn(GatewayError.Throttled(30)))
        assertEquals(Texts.LOGIN_DEVICE_LIMIT, ErrorMapper.forSignIn(GatewayError.DeviceLimit()))
        assertEquals(Texts.LOGIN_GENERIC, ErrorMapper.forSignIn(GatewayError.Server(500, null)))
    }
}

class GatewayTest {
    private class Fake(val replies: ArrayDeque<HttpResponse>) : Http {
        val seen = ArrayList<Triple<String, String, String?>>()
        val headersSeen = ArrayList<Map<String, String>>()
        override fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpResponse {
            seen.add(Triple(method, url, body)); headersSeen.add(headers)
            return replies.removeFirst()
        }
    }

    private fun resp(status: Int, body: String = "", vararg h: Pair<String, String>) = HttpResponse(status, body, h.groupBy({ it.first }, { it.second }))
    private val person = """{"id":"ivan","name":"Иван","role":"admin","parent":true}"""

    @Test fun `login posts the credentials once and returns the token and person`() {
        val f = Fake(ArrayDeque(listOf(resp(201, """{"phone_id":"p-1","token":"TOK","person":$person,"expires_at":"x"}"""))))
        val r = Gateway("https://gw.test", f).login("ivan", "pw pw pw pw pw".toCharArray(), "123456", "Pixel", "Pixel 8", "1.0.0")
        assertEquals("TOK", r.token); assertEquals("admin", r.person.role); assertTrue(r.person.parent)
        val (m, url, body) = f.seen.single()
        assertEquals("POST", m); assertEquals("https://gw.test/v1/app/login", url)
        assertTrue(body!!.contains("\"totp\":\"123456\"") && body.contains("\"username\":\"ivan\""))
    }

    @Test fun `a household login without a code sends no totp field`() {
        val f = Fake(ArrayDeque(listOf(resp(201, """{"phone_id":"p-1","token":"T","person":$person,"expires_at":"x"}"""))))
        Gateway("https://gw.test", f).login("d", "pw".toCharArray(), null, "d", "m", "1")
        assertFalse(f.seen.single().third!!.contains("totp"))
    }

    private fun failure(r: HttpResponse, call: (Gateway) -> Unit): GatewayError {
        try { call(Gateway("https://gw.test", Fake(ArrayDeque(listOf(r))))) } catch (e: GatewayError) { return e }
        fail("expected a GatewayError"); throw IllegalStateException()
    }

    @Test fun `login statuses map to the right errors`() {
        val login = { g: Gateway -> g.login("a", "b".toCharArray(), null, "d", "m", "1"); Unit }
        assertTrue(failure(resp(401, """{"error":{"code":"invalid_credentials"}}"""), login) is GatewayError.BadLogin)
        assertTrue(failure(resp(429, "", "Retry-After" to "30"), login).let { it is GatewayError.Throttled && it.retryAfterS == 30 })
        assertTrue(failure(resp(409, """{"error":{"code":"device_limit"}}"""), login) is GatewayError.DeviceLimit)
        assertTrue(failure(resp(500), login) is GatewayError.Server)
        assertTrue(failure(resp(201, "not json"), login) is GatewayError.Protocol)
    }

    @Test fun `the session call sends the token as a bearer and returns the cookie`() {
        val f = Fake(ArrayDeque(listOf(resp(200, """{"person":$person,"csrf":"c","expires_at":"x"}""", "Set-Cookie" to "gw_session=abc; HttpOnly; Secure"))))
        val s = Gateway("https://gw.test", f).session("TOK")
        assertEquals("gw_session=abc; HttpOnly; Secure", s.cookieHeader)
        assertEquals("Bearer TOK", f.headersSeen.single()["Authorization"])
    }

    @Test fun `a 401 on the session means revoked and a missing cookie is a protocol error`() {
        assertTrue(failure(resp(401)) { it.session("T") } is GatewayError.Revoked)
        assertTrue(failure(resp(200, """{"person":$person}""")) { it.session("T") } is GatewayError.Protocol)
    }

    @Test fun `logout is a bearer post and 204 is success`() {
        val f = Fake(ArrayDeque(listOf(resp(204))))
        Gateway("https://gw.test", f).logout("TOK")
        assertEquals("https://gw.test/v1/app/logout", f.seen.single().second)
        assertEquals("Bearer TOK", f.headersSeen.single()["Authorization"])
    }

    @Test fun `transport failures are network or tls errors and never leak the host's data`() {
        val boom = object : Http { override fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpResponse = throw java.net.UnknownHostException("secret.example") }
        try { Gateway("https://gw.test", boom).session("T"); fail() } catch (e: GatewayError.Network) { assertFalse(e.message!!.contains("secret")) }
        val tls = object : Http { override fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpResponse = throw javax.net.ssl.SSLHandshakeException("bad cert") }
        try { Gateway("https://gw.test", tls).session("T"); fail() } catch (e: GatewayError.Tls) { /* ok */ }
    }

    @Test fun `a call that never answers is abandoned after the total timeout and reported as no connection`() {
        val hang = object : Http { override fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpResponse { Thread.sleep(5_000); return HttpResponse(200, "", emptyMap()) } }
        val t0 = System.currentTimeMillis()
        try { Gateway("https://gw.test", hang, 150).session("T"); fail() } catch (e: GatewayError.Network) { assertTrue(e.message == "timeout") }
        assertTrue(System.currentTimeMillis() - t0 < 3_000)
    }

    @Test fun `the real transport refuses a cleartext address`() {
        try { UrlConnectionHttp.request("GET", "http://gw.test/", emptyMap(), null); fail() } catch (e: IllegalArgumentException) { /* https only */ }
    }
}

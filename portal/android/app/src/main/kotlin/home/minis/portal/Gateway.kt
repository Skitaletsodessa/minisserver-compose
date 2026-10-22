package home.minis.portal

import org.json.JSONException
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ExecutionException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException
import javax.net.ssl.SSLException

/** What can go wrong talking to the gateway, in terms the UI understands. No variant carries a password, a token or a cookie. */
sealed class GatewayError(message: String) : Exception(message) {
    class Network(message: String) : GatewayError(message)                 // no route, timeout, DNS: Tailscale off, server down
    class Tls(message: String) : GatewayError(message)                     // certificate or clock problem
    class BadLogin : GatewayError("invalid credentials")                   // 401 from /v1/app/login (the gateway never says which part was wrong)
    class Throttled(val retryAfterS: Int?) : GatewayError("throttled")      // 429
    class DeviceLimit : GatewayError("device limit")                       // 409 device_limit
    class Revoked : GatewayError("token revoked or expired")                // 401 from /v1/app/session or /v1/app/logout
    class Server(val status: Int, val code: String?) : GatewayError("server error $status")
    class Protocol(message: String) : GatewayError(message)                // an answer that is not what the contract says
}

data class Person(val id: String, val name: String, val role: String, val parent: Boolean)
data class LoginResult(val phoneId: String, val token: String, val person: Person)
data class SessionResult(val cookieHeader: String, val person: Person)

data class HttpResponse(val status: Int, val body: String, val headers: Map<String, List<String>>) {
    fun header(name: String): String? = headers.entries.firstOrNull { it.key.equals(name, ignoreCase = true) }?.value?.firstOrNull()
    fun headerAll(name: String): List<String> = headers.entries.filter { it.key.equals(name, ignoreCase = true) }.flatMap { it.value }
}

/** The one place that opens a connection. HTTPS only, the platform's certificate and host-name checks (never replaced), no redirects, no cache, bounded time and size. */
interface Http {
    @Throws(IOException::class)
    fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpResponse
}

object UrlConnectionHttp : Http {
    private const val MAX_BYTES = 256 * 1024

    override fun request(method: String, url: String, headers: Map<String, String>, body: String?): HttpResponse {
        require(url.startsWith("https://")) { "https only" }
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.instanceFollowRedirects = false
            c.useCaches = false
            c.connectTimeout = 10_000
            c.readTimeout = 25_000
            headers.forEach { (k, v) -> c.setRequestProperty(k, v) }
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
            }
            val status = c.responseCode
            val stream = if (status >= 400) c.errorStream else c.inputStream
            val out = ByteArrayOutputStream()
            if (stream != null) {
                stream.use { s ->
                    val buf = ByteArray(8192)
                    while (true) {
                        val n = s.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        if (out.size() > MAX_BYTES) throw IOException("response too large")
                    }
                }
            }
            val hs = HashMap<String, List<String>>()
            c.headerFields.forEach { (k, v) -> if (k != null) hs[k] = v }
            return HttpResponse(status, out.toString("UTF-8"), hs)
        } finally {
            c.disconnect()
        }
    }
}

class Gateway(private val base: String, private val http: Http = UrlConnectionHttp, private val totalTimeoutMs: Long = 30_000) {
    private companion object {
        val POOL: ExecutorService = Executors.newCachedThreadPool { r -> Thread(r, "gateway-call").apply { isDaemon = true } }
    }


    /** One call, bounded in TOTAL time: the platform timeouts do not cover a name lookup that hangs (Tailscale off, odd DNS), so the call runs on its own thread and is
     *  abandoned after [totalTimeoutMs]. */
    private fun call(method: String, path: String, headers: Map<String, String>, body: String?): HttpResponse {
        val f = POOL.submit<HttpResponse> { http.request(method, base + path, headers + mapOf("Accept" to "application/json"), body) }
        try {
            return f.get(totalTimeoutMs, TimeUnit.MILLISECONDS)
        } catch (e: TimeoutException) {
            f.cancel(true)
            throw GatewayError.Network("timeout")
        } catch (e: ExecutionException) {
            val c = e.cause
            when (c) {
                is SSLException -> throw GatewayError.Tls(c.javaClass.simpleName)
                is IOException -> throw GatewayError.Network(c.javaClass.simpleName)
                else -> throw GatewayError.Network("unexpected")
            }
        } catch (e: InterruptedException) {
            f.cancel(true)
            throw GatewayError.Network("interrupted")
        }
    }

    private fun errorCode(r: HttpResponse): String? = try {
        JSONObject(r.body).optJSONObject("error")?.optString("code")
    } catch (e: JSONException) {
        null
    }

    private fun person(o: JSONObject): Person = Person(o.getString("id"), o.getString("name"), o.getString("role"), o.optBoolean("parent", false))

    /** Signs in ONCE with the person's own credentials. [password] is turned into the request body and the caller wipes its array afterwards. */
    fun login(name: String, password: CharArray, totp: String?, deviceName: String, model: String, appVersion: String): LoginResult {
        val body = JSONObject().put("username", name).put("password", String(password)).put("device", JSONObject().put("name", deviceName).put("model", model).put("app_version", appVersion))
        if (totp != null) body.put("totp", totp)
        val r = call("POST", "/v1/app/login", emptyMap(), body.toString())
        when (r.status) {
            201 -> try {
                val j = JSONObject(r.body)
                return LoginResult(j.getString("phone_id"), j.getString("token"), person(j.getJSONObject("person")))
            } catch (e: JSONException) {
                throw GatewayError.Protocol("login answer")
            }
            401 -> throw GatewayError.BadLogin()
            409 -> if (errorCode(r) == "device_limit") throw GatewayError.DeviceLimit() else throw GatewayError.Server(r.status, errorCode(r))
            429 -> throw GatewayError.Throttled(r.header("Retry-After")?.toIntOrNull())
            else -> throw GatewayError.Server(r.status, errorCode(r))
        }
    }

    /** Trades the device token for a web session: the cookie header to hand to the WebView's cookie jar. */
    fun session(token: String): SessionResult {
        val r = call("POST", "/v1/app/session", mapOf("Authorization" to "Bearer $token"), null)
        when (r.status) {
            200 -> {
                val cookie = r.headerAll("Set-Cookie").firstOrNull { it.startsWith("gw_session=") } ?: throw GatewayError.Protocol("no session cookie")
                try {
                    return SessionResult(cookie, person(JSONObject(r.body).getJSONObject("person")))
                } catch (e: JSONException) {
                    throw GatewayError.Protocol("session answer")
                }
            }
            401 -> throw GatewayError.Revoked()
            429 -> throw GatewayError.Throttled(r.header("Retry-After")?.toIntOrNull())
            else -> throw GatewayError.Server(r.status, errorCode(r))
        }
    }

    /** Revokes THIS device's token on the server (the gateway identifies the device by the token itself). */
    fun logout(token: String) {
        val r = call("POST", "/v1/app/logout", mapOf("Authorization" to "Bearer $token"), null)
        when (r.status) {
            204 -> return
            401 -> throw GatewayError.Revoked()
            else -> throw GatewayError.Server(r.status, errorCode(r))
        }
    }
}

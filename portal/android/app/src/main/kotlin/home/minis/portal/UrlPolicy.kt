package home.minis.portal

import java.net.URI

/** What the WebView may do with a link. The WebView shows the gateway's origin and nothing else. */
object UrlPolicy {
    enum class Action { ALLOW, ABOUT, EXTERNAL, BLOCK }

    const val ABOUT_PATH = "/app/about"
    const val LOGIN_PATH = "/login"

    private fun parts(raw: String): Triple<String, String, Int>? = try {
        val u = URI(raw)
        val scheme = u.scheme?.lowercase()
        val host = u.host?.lowercase()
        if (scheme == null || host == null) null else Triple(scheme, host, if (u.port == -1) (if (scheme == "https") 443 else 80) else u.port)
    } catch (e: Exception) {
        null
    }

    fun sameOrigin(base: String, url: String): Boolean {
        val b = parts(base) ?: return false
        val u = parts(url) ?: return false
        return b.first == "https" && b == u
    }

    fun path(url: String): String = try { URI(url).rawPath ?: "" } catch (e: Exception) { "" }

    /** ALLOW = same origin over https; ABOUT = the native About screen (a link the page shows only in the app); EXTERNAL = another https site, handed to the system browser;
     *  BLOCK = everything else (http, intent:, file:, javascript:, data:, a malformed address). */
    fun classify(base: String, url: String): Action {
        if (sameOrigin(base, url)) return if (path(url) == ABOUT_PATH) Action.ABOUT else Action.ALLOW
        val p = parts(url) ?: return Action.BLOCK
        return if (p.first == "https") Action.EXTERNAL else Action.BLOCK
    }
}

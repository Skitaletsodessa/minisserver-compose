package home.minis.portal

import java.net.URI

/** Validation of the sign-in form (pure; the password is never put into a message, a log or an exception). */
object SignInForm {
    const val DEFAULT_URL = "https://minisserver.tail6bf4d5.ts.net:17443"

    data class Result(val ok: Boolean, val nameError: String? = null, val passwordError: String? = null, val totpError: String? = null, val urlError: String? = null,
                      val name: String = "", val totp: String? = null, val baseUrl: String = DEFAULT_URL)

    fun validate(name: String, password: CharArray, totp: String, url: String): Result {
        val n = name.trim()
        val nameError = if (n.isEmpty() || n.length > 60) Texts.ERR_NAME_EMPTY else null
        val passwordError = if (password.isEmpty() || password.size > 200) Texts.ERR_PASSWORD_EMPTY else null
        val t = totp.trim()
        val totpError = if (t.isNotEmpty() && !(t.length == 6 && t.all { it in '0'..'9' })) Texts.ERR_TOTP_FORMAT else null
        val base = normaliseUrl(url)
        val urlError = if (base == null) Texts.ERR_URL_BAD else null
        val ok = nameError == null && passwordError == null && totpError == null && urlError == null
        return Result(ok, nameError, passwordError, totpError, urlError, n, if (t.isEmpty()) null else t, base ?: DEFAULT_URL)
    }

    /** "https://host[:port]" with no path, query or user info; an empty field means the default. Anything else (http, a path, a space) is refused. */
    fun normaliseUrl(raw: String): String? {
        val s = raw.trim().trimEnd('/')
        if (s.isEmpty()) return DEFAULT_URL
        return try {
            val u = URI(s)
            if (u.scheme?.lowercase() != "https" || u.host.isNullOrEmpty() || u.userInfo != null || !(u.rawPath.isNullOrEmpty()) || u.rawQuery != null || u.rawFragment != null) null
            else s.lowercase()
        } catch (e: Exception) {
            null
        }
    }
}

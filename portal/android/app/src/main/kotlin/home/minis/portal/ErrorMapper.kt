package home.minis.portal

/** Turns what went wrong into a screen: a title, a text, and which buttons make sense. Pure. */
object ErrorMapper {
    enum class Kind { NETWORK, TLS, SERVER, SIGN_IN_AGAIN }

    data class Screen(val kind: Kind, val title: String, val text: String, val showTailscale: Boolean, val showRetry: Boolean, val showSignIn: Boolean)

    fun forStartup(e: GatewayError): Screen = when (e) {
        is GatewayError.Network -> Screen(Kind.NETWORK, Texts.NET_TITLE, Texts.NET_TEXT, showTailscale = true, showRetry = true, showSignIn = false)
        is GatewayError.Tls -> Screen(Kind.TLS, Texts.TLS_TITLE, Texts.TLS_TEXT, showTailscale = false, showRetry = true, showSignIn = false)
        is GatewayError.Revoked -> Screen(Kind.SIGN_IN_AGAIN, Texts.BTN_SIGN_IN_AGAIN, Texts.RELOGIN_REVOKED, showTailscale = false, showRetry = false, showSignIn = true)
        is GatewayError.Throttled, is GatewayError.Server, is GatewayError.Protocol, is GatewayError.BadLogin, is GatewayError.DeviceLimit ->
            Screen(Kind.SERVER, Texts.SERVER_TITLE, Texts.SERVER_TEXT, showTailscale = false, showRetry = true, showSignIn = false)
    }

    /** The message under the sign-in form. A wrong name, password or code are one message on purpose (the gateway never says which). */
    fun forSignIn(e: GatewayError): String = when (e) {
        is GatewayError.BadLogin -> Texts.LOGIN_BAD
        is GatewayError.Throttled -> Texts.LOGIN_THROTTLED
        is GatewayError.DeviceLimit -> Texts.LOGIN_DEVICE_LIMIT
        is GatewayError.Network -> Texts.NET_TITLE + ". " + Texts.NET_TEXT
        is GatewayError.Tls -> Texts.TLS_TITLE + ". " + Texts.TLS_TEXT
        else -> Texts.LOGIN_GENERIC
    }
}

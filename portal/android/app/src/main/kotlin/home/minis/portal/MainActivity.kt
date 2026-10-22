package home.minis.portal

import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsets
import android.view.WindowManager
import android.view.autofill.AutofillManager
import android.webkit.CookieManager
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebStorage
import android.webkit.WebView
import android.webkit.WebViewClient
import android.net.http.SslError
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import android.app.AlertDialog
import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import java.util.Arrays
import java.util.concurrent.Executors
import javax.crypto.Cipher

/**
 * The whole app: one activity, five screens (sign-in, locked, loading, the WebView, an error). The token is only ever in memory while the app is unlocked; at rest it
 * is AES-GCM ciphertext that only the fingerprint can open (see AndroidStorage.kt). The WebView shows the gateway's origin and nothing else.
 */
class MainActivity : FragmentActivity() {
    private enum class State { SIGN_IN, LOCKED, LOADING, WEB, ERROR }

    private lateinit var store: TokenStore
    private lateinit var root: FrameLayout
    private lateinit var signInView: ScrollView
    private lateinit var lockView: LinearLayout
    private lateinit var loadingView: FrameLayout
    private lateinit var errorView: LinearLayout
    private lateinit var webView: WebView

    private lateinit var nameEdit: EditText
    private lateinit var passwordEdit: EditText
    private lateinit var totpEdit: EditText
    private lateinit var urlEdit: EditText
    private lateinit var signInMessage: TextView
    private lateinit var signInButton: Button
    private lateinit var errorTitle: TextView
    private lateinit var errorText: TextView
    private lateinit var errorTailscale: Button
    private lateinit var errorRetry: Button
    private lateinit var errorSignIn: Button

    private val executor = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private var state = State.SIGN_IN
    private var token: String? = null                 // in memory only while unlocked
    private var base = SignInForm.DEFAULT_URL
    private var person: Person? = null
    private var backgroundedAt: Long? = null
    private var retryAction: () -> Unit = {}

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // no screenshots, no recents thumbnail of family data. Debug builds (emulator tests) skip it so the tests can take screenshots; the release build always sets it.
        if (!BuildConfig.DEBUG) window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)
        store = TokenStore(PrefsStore(this), KeystoreBox())
        buildUi()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (state == State.WEB && webView.canGoBack()) webView.goBack() else finish()
            }
        })
        // a fresh process always starts locked
        if (store.hasToken()) {
            base = store.baseUrl() ?: SignInForm.DEFAULT_URL
            person = store.person()
            showLock(autoPrompt = true)
        } else {
            showSignIn(null)
        }
    }

    override fun onStop() {
        super.onStop()
        if (state == State.WEB) backgroundedAt = SystemClock.elapsedRealtime()
    }

    override fun onStart() {
        super.onStart()
        val was = backgroundedAt
        backgroundedAt = null
        if (state == State.WEB && LockPolicy.shouldLock(was, SystemClock.elapsedRealtime())) lockNow()
    }

    override fun onDestroy() {
        executor.shutdownNow()
        if (::webView.isInitialized) webView.destroy()
        super.onDestroy()
    }

    // ── screens ────────────────────────────────────────────────────────────────────────────────────────────────────
    private fun show(v: View) {
        for (c in listOf<View>(signInView, lockView, loadingView, errorView, webView)) c.visibility = if (c === v) View.VISIBLE else View.GONE
    }

    private fun showSignIn(message: String?) {
        state = State.SIGN_IN
        token = null
        setBusy(false)
        passwordEdit.text.clear()
        totpEdit.text.clear()
        signInMessage.text = message ?: ""
        signInMessage.visibility = if (message.isNullOrEmpty()) View.GONE else View.VISIBLE
        show(signInView)
    }

    private fun showLock(autoPrompt: Boolean) {
        state = State.LOCKED
        show(lockView)
        if (autoPrompt) main.post { promptUnlock() }
    }

    private fun showError(s: ErrorMapper.Screen, retry: () -> Unit) {
        state = State.ERROR
        errorTitle.text = s.title
        errorText.text = s.text
        errorTailscale.visibility = if (s.showTailscale) View.VISIBLE else View.GONE
        errorRetry.visibility = if (s.showRetry) View.VISIBLE else View.GONE
        errorSignIn.visibility = if (s.showSignIn) View.VISIBLE else View.GONE
        retryAction = retry
        show(errorView)
    }

    private fun setBusy(busy: Boolean) {
        signInButton.isEnabled = !busy
        signInButton.text = if (busy) Texts.WORKING else Texts.SIGN_IN_BUTTON
    }

    // ── sign-in (once) ─────────────────────────────────────────────────────────────────────────────────────────────
    private fun onSignInClicked() {
        val pw = CharArray(passwordEdit.length())
        passwordEdit.text.getChars(0, pw.size, pw, 0)
        val r = SignInForm.validate(nameEdit.text.toString(), pw, totpEdit.text.toString(), urlEdit.text.toString())
        if (!r.ok) {
            Arrays.fill(pw, '\u0000')
            nameEdit.error = r.nameError
            passwordEdit.error = r.passwordError
            totpEdit.error = r.totpError
            if (r.urlError != null) { urlEdit.visibility = View.VISIBLE; urlEdit.error = r.urlError }
            return
        }
        if (!canUseFingerprint()) {
            Arrays.fill(pw, '\u0000')
            signInMessage.text = Texts.NO_SECURE_LOCK
            signInMessage.visibility = View.VISIBLE
            return
        }
        signInMessage.visibility = View.GONE
        setBusy(true)
        passwordEdit.text.clear()
        val device = ("${Build.MANUFACTURER} ${Build.MODEL}").trim().take(60)
        executor.execute {
            try {
                val res = Gateway(r.baseUrl).login(r.name, pw, r.totp, device, Build.MODEL.take(60), BuildConfig.VERSION_NAME)
                Arrays.fill(pw, '\u0000')
                main.post { sealAfterLogin(res, r.baseUrl, device) }
            } catch (e: GatewayError) {
                Arrays.fill(pw, '\u0000')
                main.post { setBusy(false); signInMessage.text = ErrorMapper.forSignIn(e); signInMessage.visibility = View.VISIBLE }
            }
        }
    }

    private fun sealAfterLogin(res: LoginResult, url: String, device: String) {
        val cipher = try { store.encryptCipher() } catch (e: Exception) { abandonLogin(res, url); return }
        authenticate(cipher, Texts.PROMPT_SEAL_TITLE, Texts.PROMPT_SEAL_SUBTITLE, onOk = { c ->
            try {
                store.seal(c, res.token, res.person, url, device)
                base = url
                person = res.person
                token = res.token
                startSession()
            } catch (e: Exception) {
                abandonLogin(res, url)
            }
        }, onFail = { _ -> abandonLogin(res, url) })
    }

    /** The fingerprint was not confirmed: the access the gateway just issued is revoked again, nothing is kept on the phone. */
    private fun abandonLogin(res: LoginResult, url: String) {
        executor.execute { try { Gateway(url).logout(res.token) } catch (_: GatewayError) { /* best effort */ } }
        store.wipe()
        showSignIn(Texts.SIGN_IN_CANCELLED)
    }

    // ── lock ───────────────────────────────────────────────────────────────────────────────────────────────────────
    private fun canUseFingerprint(): Boolean =
        BiometricManager.from(this).canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG) == BiometricManager.BIOMETRIC_SUCCESS

    private fun authenticate(cipher: Cipher, title: String, subtitle: String, onOk: (Cipher) -> Unit, onFail: (Int) -> Unit) {
        val cb = object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                val c = result.cryptoObject?.cipher
                if (c == null) onFail(-1) else onOk(c)
            }

            override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                onFail(errorCode)
            }
        }
        val info = BiometricPrompt.PromptInfo.Builder()
            .setTitle(title)
            .setSubtitle(subtitle)
            .setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG)
            .setNegativeButtonText(Texts.BTN_CANCEL)
            .build()
        BiometricPrompt(this, ContextCompat.getMainExecutor(this), cb).authenticate(info, BiometricPrompt.CryptoObject(cipher))
    }

    private fun promptUnlock() {
        if (state != State.LOCKED) return
        val cipher = try {
            store.decryptCipher()
        } catch (e: KeyInvalidated) {
            wipeAndSignIn(Texts.RELOGIN_NEW_FINGERPRINT); return
        } catch (e: Exception) {
            wipeAndSignIn(Texts.RELOGIN_KEY_LOST); return
        }
        authenticate(cipher, Texts.PROMPT_UNLOCK_TITLE, Texts.PROMPT_UNLOCK_SUBTITLE, onOk = { c ->
            val t = store.open(c)
            if (t == null) wipeAndSignIn(Texts.RELOGIN_KEY_LOST) else { token = t; startSession() }
        }, onFail = { _ -> /* cancelled or locked out: stay on the lock screen, the button asks again; there is no way around the prompt */ })
    }

    private fun lockNow() {
        token = null
        clearWebData()
        showLock(autoPrompt = true)
    }

    private fun wipeAndSignIn(message: String) {
        wipeLocal()
        showSignIn(message)
    }

    private fun wipeLocal() {
        store.wipe()
        token = null
        person = null
        clearWebData()
    }

    private fun clearWebData() {
        if (!::webView.isInitialized) return
        webView.loadUrl("about:blank")
        webView.clearHistory()
        webView.clearCache(true)
        CookieManager.getInstance().removeAllCookies(null)
        CookieManager.getInstance().flush()
        WebStorage.getInstance().deleteAllData()
    }

    // ── the session and the WebView ────────────────────────────────────────────────────────────────────────────────
    private fun startSession() {
        val t = token ?: run { showLock(autoPrompt = true); return }
        state = State.LOADING
        show(loadingView)
        executor.execute {
            try {
                val s = Gateway(base).session(t)
                main.post { openWeb(s) }
            } catch (e: GatewayError) {
                main.post { onSessionError(e) }
            }
        }
    }

    private fun onSessionError(e: GatewayError) {
        if (e is GatewayError.Revoked) { wipeAndSignIn(Texts.RELOGIN_REVOKED); return }
        showError(ErrorMapper.forStartup(e)) { startSession() }
    }

    private fun openWeb(s: SessionResult) {
        person = s.person
        val cm = CookieManager.getInstance()
        cm.setAcceptCookie(true)
        cm.setAcceptThirdPartyCookies(webView, false)
        cm.setCookie(base, s.cookieHeader)
        cm.flush()
        state = State.WEB
        show(webView)
        webView.loadUrl(base + "/home")
    }

    private fun openTailscale() {
        val i = packageManager.getLaunchIntentForPackage("com.tailscale.ipn")
        if (i == null) Toast.makeText(this, Texts.TAILSCALE_MISSING, Toast.LENGTH_LONG).show() else startActivity(i)
    }

    private fun openExternal(url: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE))
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(this, Texts.BLOCKED_LINK, Toast.LENGTH_SHORT).show()
        }
    }

    private fun showAbout() {
        val p = person
        val msg = buildString {
            append("Версия: ${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})\n")
            append("Сервер: $base\n")
            append("Человек: ${p?.name ?: "—"}\n")
            append("Роль: ${p?.role ?: "—"}${if (p?.parent == true) ", родитель" else ""}\n")
            append("Устройство: ${store.deviceName() ?: "—"}\n\n")
            append(Texts.ABOUT_SIGN_OUT_NOTE)
        }
        AlertDialog.Builder(this)
            .setTitle(Texts.ABOUT_TITLE)
            .setMessage(msg)
            .setPositiveButton(Texts.BTN_CLOSE, null)
            .setNegativeButton(Texts.ABOUT_SIGN_OUT) { _, _ -> signOut() }
            .show()
    }

    private fun signOut() {
        val t = token
        executor.execute {
            var online = true
            if (t != null) {
                try { Gateway(base).logout(t) } catch (_: GatewayError.Revoked) { /* already revoked */ } catch (_: GatewayError) { online = false }
            }
            main.post {
                wipeLocal()
                showSignIn(if (online) null else Texts.ABOUT_SIGNED_OUT_OFFLINE)
            }
        }
    }

    private fun mainFrameFailed(s: ErrorMapper.Screen) {
        if (state != State.WEB) return
        showError(s) { startSession() }
    }

    @SuppressLint("SetJavaScriptEnabled")           // the portal's own pages need it; the WebView only ever shows the gateway origin (UrlPolicy) and has no JavaScript bridge
    private fun configureWebView(w: WebView) {
        val s = w.settings
        s.javaScriptEnabled = true                       // the portal's pages need it
        s.domStorageEnabled = false
        s.allowFileAccess = false
        s.allowContentAccess = false
        s.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        s.cacheMode = WebSettings.LOAD_NO_CACHE          // no answer of the portal is ever stored on disk
        s.setSupportMultipleWindows(false)
        s.javaScriptCanOpenWindowsAutomatically = false
        s.setGeolocationEnabled(false)
        s.safeBrowsingEnabled = true
        s.builtInZoomControls = false
        w.isSaveEnabled = false
        w.setDownloadListener { _, _, _, _, _ -> /* downloads are not part of this app */ }
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
        // no addJavascriptInterface: nothing in the page can call into the app
        w.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url.toString()
                return when (UrlPolicy.classify(base, url)) {
                    UrlPolicy.Action.ALLOW -> if (UrlPolicy.path(url) == UrlPolicy.LOGIN_PATH) { startSession(); true } else false      // the session ended: make a new one from the token
                    UrlPolicy.Action.ABOUT -> { showAbout(); true }
                    UrlPolicy.Action.EXTERNAL -> { openExternal(url); true }
                    UrlPolicy.Action.BLOCK -> { Toast.makeText(this@MainActivity, Texts.BLOCKED_LINK, Toast.LENGTH_SHORT).show(); true }
                }
            }

            override fun onPageStarted(view: WebView, url: String, favicon: Bitmap?) {
                if (state == State.WEB && url != "about:blank" && !UrlPolicy.sameOrigin(base, url)) view.stopLoading()
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (!request.isForMainFrame) return
                val e: GatewayError = if (error.errorCode == WebViewClient.ERROR_FAILED_SSL_HANDSHAKE) GatewayError.Tls("ssl") else GatewayError.Network("web")
                mainFrameFailed(ErrorMapper.forStartup(e))
            }

            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, errorResponse: WebResourceResponse) {
                if (request.isForMainFrame && errorResponse.statusCode >= 500) mainFrameFailed(ErrorMapper.forStartup(GatewayError.Server(errorResponse.statusCode, null)))
            }

            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                handler.cancel()                         // never proceed past a certificate problem
                mainFrameFailed(ErrorMapper.forStartup(GatewayError.Tls("ssl")))
            }

            override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
                mainFrameFailed(ErrorMapper.forStartup(GatewayError.Network("render")))
                return true
            }
        }
    }

    // ── building the views ─────────────────────────────────────────────────────────────────────────────────────────
    private fun dp(n: Int): Int = (n * resources.displayMetrics.density).toInt()

    private fun label(text: String, size: Float = 16f, bold: Boolean = false): TextView = TextView(this).apply {
        this.text = text
        textSize = size
        if (bold) setTypeface(typeface, android.graphics.Typeface.BOLD)
        setPadding(0, dp(6), 0, dp(6))
    }

    private fun field(hint: String, password: Boolean = false, digits: Boolean = false): EditText = EditText(this).apply {
        this.hint = hint
        inputType = when {
            password -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            digits -> InputType.TYPE_CLASS_NUMBER
            else -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        }
        minHeight = dp(48)
        if (password || digits) importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS      // no autofill cache of the secret fields
        isSaveEnabled = false
    }

    private fun button(text: String, onClick: () -> Unit): Button = Button(this).apply {
        this.text = text
        minHeight = dp(48)
        setOnClickListener { onClick() }
    }

    private fun column(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(24), dp(24), dp(24), dp(24))
    }

    private fun buildUi() {
        root = FrameLayout(this)
        root.setOnApplyWindowInsetsListener { v, insets ->
            val i = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.ime())
            v.setPadding(i.left, i.top, i.right, i.bottom)
            WindowInsets.CONSUMED
        }

        // sign-in
        val form = column()
        form.addView(label(Texts.SIGN_IN_TITLE, 22f, true))
        form.addView(label(Texts.SIGN_IN_INTRO))
        nameEdit = field(Texts.FIELD_NAME).also { form.addView(it) }
        passwordEdit = field(Texts.FIELD_PASSWORD, password = true).also { form.addView(it) }
        totpEdit = field(Texts.FIELD_TOTP, digits = true).apply { filters = arrayOf(android.text.InputFilter.LengthFilter(6)) }.also { form.addView(it) }
        urlEdit = field(Texts.FIELD_URL).apply { setText(SignInForm.DEFAULT_URL); visibility = View.GONE }
        form.addView(label(Texts.ADVANCED).apply {
            setPadding(0, dp(14), 0, dp(14))
            setOnClickListener { urlEdit.visibility = if (urlEdit.visibility == View.VISIBLE) View.GONE else View.VISIBLE }
        })
        form.addView(urlEdit)
        signInMessage = label("").apply { visibility = View.GONE; setTextColor(0xFFB3261E.toInt()) }
        form.addView(signInMessage)
        signInButton = button(Texts.SIGN_IN_BUTTON) { onSignInClicked() }
        form.addView(signInButton)
        signInView = ScrollView(this).apply { isFillViewport = true; addView(form) }

        // locked
        lockView = column().apply {
            gravity = Gravity.CENTER
            addView(label(Texts.LOCK_TITLE, 22f, true))
            addView(button(Texts.LOCK_BUTTON) { promptUnlock() })
        }

        // loading
        loadingView = FrameLayout(this).apply { addView(ProgressBar(context), FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER)) }

        // error
        errorTitle = label("", 22f, true)
        errorText = label("")
        errorTailscale = button(Texts.BTN_TAILSCALE) { openTailscale() }
        errorRetry = button(Texts.BTN_RETRY) { retryAction() }
        errorSignIn = button(Texts.BTN_SIGN_IN_AGAIN) { wipeAndSignIn("") }
        errorView = column().apply {
            gravity = Gravity.CENTER
            addView(errorTitle); addView(errorText); addView(errorTailscale); addView(errorRetry); addView(errorSignIn)
        }

        webView = WebView(this)
        configureWebView(webView)

        for (v in listOf<View>(signInView, lockView, loadingView, errorView, webView)) {
            root.addView(v, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
            v.visibility = View.GONE
        }
        setContentView(root)
        getSystemService(AutofillManager::class.java)?.cancel()
    }
}

package home.minis.portal

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.KeyStore
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Public keys of the manifest signature, compiled in (see app/build.gradle.kts): the current server key and a spare, so that the key can rotate with a new app build. */
object UpdateKeys {
    val PUBLIC: List<ByteArray> = BuildConfig.MANIFEST_PUBKEYS.split(",").filter { it.isNotBlank() }.map { ManifestVerifier.hexToBytes(it.trim()) }
}

/** Reads signing certificates through the platform: the same call the system makes (it verifies the signature while collecting the certificates). */
class PackageInspector(private val ctx: Context) : ApkInspector {
    private fun sha(b: ByteArray) = ManifestVerifier.sha256Hex(b)

    override fun signerSha256(path: String): Set<String>? = try {
        val info = ctx.packageManager.getPackageArchiveInfo(path, PackageManager.GET_SIGNING_CERTIFICATES)
        info?.signingInfo?.apkContentsSigners?.map { sha(it.toByteArray()) }?.toSet()
    } catch (e: Exception) {
        null
    }

    override fun installedSignerSha256(): Set<String> = try {
        ctx.packageManager.getPackageInfo(ctx.packageName, PackageManager.GET_SIGNING_CERTIFICATES).signingInfo?.apkContentsSigners?.map { sha(it.toByteArray()) }?.toSet() ?: emptySet()
    } catch (e: Exception) {
        emptySet()
    }
}

/** Streams the APK into a file: HTTPS only, no redirects, never more bytes than the manifest announced, bounded connect/read time. */
class HttpDownloader : Downloader {
    @Throws(IOException::class)
    override fun fetch(url: String, headers: Map<String, String>, dest: File, expectedSize: Long) {
        require(url.startsWith("https://")) { "https only" }
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.instanceFollowRedirects = false
            c.useCaches = false
            c.connectTimeout = 15_000
            c.readTimeout = 30_000
            headers.forEach { (k, v) -> c.setRequestProperty(k, v) }
            if (c.responseCode != 200) throw IOException("status " + c.responseCode)
            var total = 0L
            val deadline = System.currentTimeMillis() + 10 * 60_000L
            c.inputStream.use { i ->
                dest.outputStream().use { o ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = i.read(buf)
                        if (n < 0) break
                        total += n
                        if (total > expectedSize) throw IOException("more bytes than announced")
                        if (System.currentTimeMillis() > deadline) throw IOException("too slow")
                        o.write(buf, 0, n)
                    }
                    o.flush()
                }
            }
            if (total != expectedSize) throw IOException("fewer bytes than announced")
        } finally {
            c.disconnect()
        }
    }
}

/** The update-only key at rest: AES-GCM with an Android Keystore key that needs NO user authentication (the background check must work while the app is locked). The key can read
 *  no family data and call no endpoint but the two update paths, so this is protection against casual reading of the app's files, not a secret that guards anything private. */
class UpdateKeyStore(private val kv: KeyValueStore) {
    private val ks: KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun key(): SecretKey {
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val spec = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setKeySize(256).build()
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply { init(spec) }.generateKey()
    }

    fun has(): Boolean = kv.get("uk_ct") != null

    fun save(value: String) {
        try {
            val c = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
            kv.put("uk_iv", Base64.getEncoder().encodeToString(c.iv))
            kv.put("uk_ct", Base64.getEncoder().encodeToString(c.doFinal(value.toByteArray(Charsets.UTF_8))))
        } catch (e: Exception) { /* the next sign-in or unlock mints it again */ }
    }

    fun get(): String? = try {
        val iv = Base64.getDecoder().decode(kv.get("uk_iv") ?: return null)
        val ct = Base64.getDecoder().decode(kv.get("uk_ct") ?: return null)
        String(Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv)) }.doFinal(ct), Charsets.UTF_8)
    } catch (e: Exception) {
        null
    }

    fun clear() {
        kv.remove("uk_ct"); kv.remove("uk_iv")
    }

    private companion object { const val ALIAS = "portal_update_key_v1" }
}

/** Notification for the two cases that need a person: an install that the system wants to confirm with one tap, and an update that needs the "install unknown apps" switch. */
object UpdateNotifier {
    private const val CHANNEL = "updates"
    private const val ID_TAP = 41
    private const val ID_OPEN = 42

    private fun channel(ctx: Context): NotificationManager {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) nm.createNotificationChannel(NotificationChannel(CHANNEL, Texts.UPD_CHANNEL, NotificationManager.IMPORTANCE_DEFAULT))
        return nm
    }

    private fun can(ctx: Context) = ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    fun confirm(ctx: Context, confirm: Intent) {
        if (!can(ctx)) return
        val pi = PendingIntent.getActivity(ctx, ID_TAP, confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        channel(ctx).notify(ID_TAP, Notification.Builder(ctx, CHANNEL).setSmallIcon(android.R.drawable.stat_sys_download_done).setContentTitle(Texts.UPD_NOTIF_TITLE)
            .setContentText(Texts.UPD_NOTIF_TAP).setContentIntent(pi).setAutoCancel(true).build())
    }

    fun openApp(ctx: Context, text: String) {
        if (!can(ctx)) return
        val launch = ctx.packageManager.getLaunchIntentForPackage(ctx.packageName) ?: return
        val pi = PendingIntent.getActivity(ctx, ID_OPEN, launch, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        channel(ctx).notify(ID_OPEN, Notification.Builder(ctx, CHANNEL).setSmallIcon(android.R.drawable.stat_sys_download_done).setContentTitle(Texts.UPD_NOTIF_TITLE)
            .setContentText(text).setContentIntent(pi).setAutoCancel(true).build())
    }

    fun clear(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        nm.cancel(ID_TAP); nm.cancel(ID_OPEN)
    }
}

/** Process-wide flags the receiver and the job need: is the app on screen (then it handles a pending confirmation itself)? */
object UpdateEvents {
    @Volatile var visible = false
    @Volatile var confirmHandler: ((Intent) -> Unit)? = null
}

/** Hands the downloaded, verified APK to the system's PackageInstaller. USER_ACTION_NOT_REQUIRED is requested: for a self-update by an installer that holds
 *  REQUEST_INSTALL_PACKAGES the platform then installs without a prompt (Android 12+; the conditions are in ADR 0010). Whether it did is observed, not assumed. */
class UpdateInstaller(private val ctx: Context) {
    fun canRequestInstalls(): Boolean = ctx.packageManager.canRequestPackageInstalls()

    private fun abandonOwn() {
        val pi = ctx.packageManager.packageInstaller
        pi.mySessions.forEach { try { pi.abandonSession(it.sessionId) } catch (_: Exception) { /* gone already */ } }
    }

    @Throws(IOException::class)
    fun install(apk: File) {
        val pi = ctx.packageManager.packageInstaller
        abandonOwn()
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        params.setAppPackageName(ctx.packageName)
        params.setSize(apk.length())
        params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        val id = pi.createSession(params)
        val s = pi.openSession(id)
        try {
            apk.inputStream().use { i -> s.openWrite("base.apk", 0, apk.length()).use { o -> i.copyTo(o); s.fsync(o) } }
            val intent = Intent(ctx, InstallReceiver::class.java).setAction(InstallReceiver.ACTION).setPackage(ctx.packageName)
            val pend = PendingIntent.getBroadcast(ctx, id, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
            s.commit(pend.intentSender)
        } catch (e: Exception) {
            s.abandon()
            throw IOException("install session failed: " + e.javaClass.simpleName)
        } finally {
            s.close()
        }
    }
}

class InstallReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val state = UpdateState(PrefsStore(ctx, "updates"))
        when (intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -1)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                val confirm = intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java) ?: return
                state.lastError = null
                val h = UpdateEvents.confirmHandler
                if (UpdateEvents.visible && h != null) h(confirm) else UpdateNotifier.confirm(ctx, confirm)
            }
            PackageInstaller.STATUS_SUCCESS -> { state.lastError = null; state.installingCode = 0; UpdateNotifier.clear(ctx) }
            PackageInstaller.STATUS_FAILURE_ABORTED -> { state.installingCode = 0 }                       // the person declined the one-tap confirmation: not an error, no loop
            else -> { state.lastError = "install"; state.installingCode = 0; UpdateNotifier.clear(ctx) }
        }
    }

    companion object { const val ACTION = "home.minis.portal.INSTALL_RESULT" }
}

/** The periodic background check: about every 6 hours, only with a network, tolerant of failure, no retry storm (a failed run just waits for the next period). */
class UpdateJob : JobService() {
    override fun onStartJob(params: JobParameters): Boolean {
        Thread {
            try { UpdateService(applicationContext).backgroundRun() } catch (_: Exception) { /* a failed run waits for the next period */ } finally { jobFinished(params, false) }
        }.start()
        return true
    }

    override fun onStopJob(params: JobParameters): Boolean = false

    companion object {
        const val JOB_ID = 4101
        const val PERIOD_MS = 6 * 60 * 60 * 1000L

        fun schedule(ctx: Context) {
            val js = ctx.getSystemService(JobScheduler::class.java)
            try {
                if (js.getPendingJob(JOB_ID) != null) return                                  // already scheduled: do not reset its clock at every launch
                js.schedule(JobInfo.Builder(JOB_ID, ComponentName(ctx, UpdateJob::class.java)).setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setPeriodic(PERIOD_MS, 60 * 60 * 1000L).build())
            } catch (_: Exception) { /* a scheduling failure must never take the app down: the foreground check still runs at every launch */ }
        }
    }
}

/** Everything the activity and the job share: check, download, verify, install. All blocking; the callers run it on a background thread. */
class UpdateService(private val ctx: Context) {
    private val prefs = PrefsStore(ctx, "updates")
    val state = UpdateState(prefs)
    val keys = UpdateKeyStore(prefs)
    private val dir = File(ctx.filesDir, "updates")

    private fun installedCode(): Int = BuildConfig.VERSION_CODE

    private fun core() = UpdateCore(state, UrlConnectionHttp, UpdateKeys.PUBLIC, ::installedCode, { System.currentTimeMillis() }, BuildConfig.PINNED_CERT_SHA256.ifEmpty { null })

    fun baseUrl(): String? = TokenStore(PrefsStore(ctx), KeystoreBox()).baseUrl()

    /** The APK of a release that is already installed (or older) is dead weight: removed at the next check. Partial or newer files are left to the next download, which clears the folder. */
    private fun cleanInstalled() {
        dir.listFiles()?.forEach { f ->
            val code = Regex("^portal-([0-9]+)\\.apk$").matchEntire(f.name)?.groupValues?.get(1)?.toIntOrNull()
            if (code != null && code <= installedCode()) f.delete()
        }
    }

    fun check(): CheckOutcome {
        cleanInstalled()
        val base = baseUrl() ?: return CheckOutcome.Failed("no_key")
        return core().check(base, keys.get())
    }

    /** Download + verify. Returns the verified file or the failure code. */
    fun download(m: Manifest): Pair<File?, String?> {
        val base = baseUrl() ?: return Pair(null, "no_key")
        val key = keys.get() ?: return Pair(null, "no_key")
        return core().fetchAndVerify(base, key, m, dir, HttpDownloader(), PackageInspector(ctx))
    }

    fun install(file: File, code: Int): String? {
        val inst = UpdateInstaller(ctx)
        if (!inst.canRequestInstalls()) { state.lastError = "blocked"; return "blocked" }
        return try {
            state.installingCode = code
            inst.install(file)
            null
        } catch (e: Exception) {
            state.lastError = "install"; state.installingCode = 0
            "install"
        }
    }

    /** The periodic run. While the app is on screen it does nothing (the foreground flow handles it); otherwise it checks, downloads, verifies and installs, or leaves a notification
     *  when a person has to do something (the "install unknown apps" switch, a one-tap confirmation). */
    fun backgroundRun() {
        if (UpdateEvents.visible) return
        val out = check()
        if (out !is CheckOutcome.Available) return
        if (state.installingCode == out.manifest.versionCode) return                    // an install is already in flight for this version
        val (file, err) = download(out.manifest)
        if (file == null) return
        if (!UpdateInstaller(ctx).canRequestInstalls()) { state.lastError = "blocked"; UpdateNotifier.openApp(ctx, Texts.UPD_NOTIF_OPEN); return }
        install(file, out.manifest.versionCode)
    }
}

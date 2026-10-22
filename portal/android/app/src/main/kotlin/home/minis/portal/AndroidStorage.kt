package home.minis.portal

import android.annotation.SuppressLint
import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyPermanentlyInvalidatedException
import android.security.keystore.KeyProperties
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

@SuppressLint("ApplySharedPref")      // commit() on purpose: a wipe must be on disk before the sign-in screen is shown
class PrefsStore(context: Context) : KeyValueStore {
    // Private app storage only; not backed up (see data_extraction_rules.xml). What is in it is ciphertext plus non-secret labels.
    private val p = context.getSharedPreferences("sealed", Context.MODE_PRIVATE)
    override fun get(key: String): String? = p.getString(key, null)
    override fun put(key: String, value: String) { p.edit().putString(key, value).commit() }
    override fun remove(key: String) { p.edit().remove(key).commit() }
    override fun clear() { p.edit().clear().commit() }
}

/**
 * The Android Keystore key that seals the device token: AES-256-GCM, not exportable, **the user must authenticate for every single use** (fingerprint, or - through the
 * system's own prompt - the device PIN), and it is **invalidated when a new fingerprint is enrolled** (then the app asks to sign in again).
 */
class KeystoreBox : KeyBox {
    private val ks: KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun create() {
        val spec = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .setUserAuthenticationRequired(true)
            // 0 = authenticate for every use; BIOMETRIC ONLY on purpose: a key that also accepts the device PIN is NOT invalidated by a new fingerprint (measured on the emulator),
            // and the PIN would be a way around the fingerprint. The phone's own biometric lockout is cleared by unlocking the phone with its PIN.
            .setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)
            .setInvalidatedByBiometricEnrollment(true)
            .build()
        KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply { init(spec) }.generateKey()
    }

    private fun key(): SecretKey? = ks.getKey(ALIAS, null) as? SecretKey

    override fun encryptCipher(): Cipher {
        deleteKey()                                   // a fresh key for every sign-in: nothing sealed earlier can ever be opened with it
        create()
        val k = key() ?: throw KeyInvalidated("key not created")
        return Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.ENCRYPT_MODE, k) }
    }

    override fun decryptCipher(iv: ByteArray): Cipher {
        val k = try { key() } catch (e: Exception) { null } ?: throw KeyInvalidated("key missing")
        return try {
            Cipher.getInstance(TRANSFORMATION).apply { init(Cipher.DECRYPT_MODE, k, GCMParameterSpec(128, iv)) }
        } catch (e: KeyPermanentlyInvalidatedException) {
            throw KeyInvalidated("a new fingerprint was enrolled")
        }
    }

    override fun deleteKey() {
        try { if (ks.containsAlias(ALIAS)) ks.deleteEntry(ALIAS) } catch (_: Exception) { /* nothing to delete */ }
    }

    private companion object {
        const val ALIAS = "portal_token_key_v1"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}

package home.minis.portal

import java.util.Base64
import javax.crypto.Cipher

/** Small key-value storage (SharedPreferences on the phone, a map in tests). */
interface KeyValueStore {
    fun get(key: String): String?
    fun put(key: String, value: String)
    fun remove(key: String)
    fun clear()
}

/** Gives out initialised ciphers. On the phone they are bound to an Android Keystore key that needs the user's authentication for EVERY use; in tests, to a software key. */
interface KeyBox {
    /** A cipher in ENCRYPT mode (the key is created if missing). */
    fun encryptCipher(): Cipher

    /** A cipher in DECRYPT mode for the given IV. Throws [KeyInvalidated] when the key was invalidated (a new fingerprint was enrolled) or is gone. */
    fun decryptCipher(iv: ByteArray): Cipher

    fun deleteKey()
}

class KeyInvalidated(message: String) : Exception(message)

/**
 * The device token, sealed: AES-GCM ciphertext + IV in the preferences, never the token itself, never in a log. Sealing and opening need a cipher that the caller got
 * from [KeyBox] and had authorised by the fingerprint (BiometricPrompt.CryptoObject): without that the ciphertext is just bytes.
 */
class TokenStore(private val prefs: KeyValueStore, private val keys: KeyBox) {
    private val b64 = Base64.getEncoder()
    private val unb64 = Base64.getDecoder()

    fun hasToken(): Boolean = prefs.get(K_CT) != null && prefs.get(K_IV) != null

    /** The IV of the sealed token (needed to build the decrypt cipher), or null when nothing is stored. */
    fun iv(): ByteArray? = prefs.get(K_IV)?.let { runCatching { unb64.decode(it) }.getOrNull() }

    fun decryptCipher(): Cipher {
        val iv = iv() ?: throw KeyInvalidated("no sealed token")
        return keys.decryptCipher(iv)
    }

    fun encryptCipher(): Cipher = keys.encryptCipher()

    /** [cipher] = the ENCRYPT cipher after the user's authentication. */
    fun seal(cipher: Cipher, token: String, person: Person, baseUrl: String, deviceName: String) {
        val ct = cipher.doFinal(token.toByteArray(Charsets.UTF_8))
        prefs.put(K_IV, b64.encodeToString(cipher.iv))
        prefs.put(K_CT, b64.encodeToString(ct))
        prefs.put(K_PERSON, listOf(person.id, person.name, person.role, person.parent.toString()).joinToString("\u0001"))
        prefs.put(K_URL, baseUrl)
        prefs.put(K_DEVICE, deviceName)
    }

    /** [cipher] = the DECRYPT cipher after the user's authentication. Returns null when the sealed bytes do not verify (tampered or from another key). */
    fun open(cipher: Cipher): String? {
        val ct = prefs.get(K_CT)?.let { runCatching { unb64.decode(it) }.getOrNull() } ?: return null
        return try {
            String(cipher.doFinal(ct), Charsets.UTF_8)
        } catch (e: Exception) {
            null
        }
    }

    fun baseUrl(): String? = prefs.get(K_URL)
    fun deviceName(): String? = prefs.get(K_DEVICE)
    fun person(): Person? = prefs.get(K_PERSON)?.split("\u0001")?.takeIf { it.size == 4 }?.let { Person(it[0], it[1], it[2], it[3] == "true") }

    /** Forget everything: the sealed token, who it belonged to, the key. */
    fun wipe() {
        prefs.clear()
        keys.deleteKey()
    }

    private companion object {
        const val K_IV = "iv"
        const val K_CT = "ct"
        const val K_PERSON = "person"
        const val K_URL = "url"
        const val K_DEVICE = "device"
    }
}

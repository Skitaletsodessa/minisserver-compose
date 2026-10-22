package home.minis.portal

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** The sealing logic with a SOFTWARE key standing in for the Keystore key (the real one needs a phone and a fingerprint; see the report for what that leaves unproven). */
class TokenStoreTest {
    private class MemStore : KeyValueStore {
        val m = HashMap<String, String>()
        override fun get(key: String) = m[key]
        override fun put(key: String, value: String) { m[key] = value }
        override fun remove(key: String) { m.remove(key) }
        override fun clear() { m.clear() }
    }

    private class SoftBox : KeyBox {
        var key: SecretKey? = null
        var invalidated = false
        override fun encryptCipher(strict: Boolean): Cipher {
            key = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
            return Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key) }
        }
        override fun decryptCipher(iv: ByteArray): Cipher {
            if (invalidated || key == null) throw KeyInvalidated("gone")
            return Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, iv)) }
        }
        override fun deleteKey() { key = null }
    }

    private val ivan = Person("ivan", "Иван", "admin", true)
    private val token = "tok-" + "x".repeat(40)

    private fun sealed(): Triple<TokenStore, MemStore, SoftBox> {
        val p = MemStore(); val k = SoftBox()
        val s = TokenStore(p, k)
        s.seal(s.encryptCipher(true), token, ivan, "https://gw.test", "Pixel 8")
        return Triple(s, p, k)
    }

    @Test fun `seal then open round-trips and the preferences never hold the token`() {
        val (s, p, _) = sealed()
        assertTrue(s.hasToken())
        assertEquals(token, s.open(s.decryptCipher()))
        for (v in p.m.values) assertFalse("the token appears in the stored values", v.contains(token) || v.contains("tok-"))
        assertEquals(ivan, s.person()); assertEquals("https://gw.test", s.baseUrl()); assertEquals("Pixel 8", s.deviceName())
    }

    @Test fun `a nothing-stored phone has no token and cannot build a decrypt cipher`() {
        val s = TokenStore(MemStore(), SoftBox())
        assertFalse(s.hasToken())
        try { s.decryptCipher(); fail() } catch (e: KeyInvalidated) { /* ok */ }
    }

    @Test fun `tampered ciphertext does not open`() {
        val (s, p, _) = sealed()
        val ct = java.util.Base64.getDecoder().decode(p.m["ct"]!!)
        ct[0] = (ct[0].toInt() xor 1).toByte()
        p.m["ct"] = java.util.Base64.getEncoder().encodeToString(ct)
        assertNull(s.open(s.decryptCipher()))
    }

    @Test fun `a new fingerprint (invalidated key) is reported as such and the sealed token is unreadable`() {
        val (s, _, k) = sealed()
        k.invalidated = true
        try { s.decryptCipher(); fail() } catch (e: KeyInvalidated) { /* the app then wipes and asks to sign in again */ }
    }

    @Test fun `a sign-out wipes the ciphertext, the labels and the key`() {
        val (s, p, k) = sealed()
        s.wipe()
        assertFalse(s.hasToken()); assertTrue(p.m.isEmpty()); assertNull(k.key); assertNull(s.person())
    }

    @Test fun `the lock mode is remembered - strict by default and fallback only when it was made so`() {
        val p = MemStore(); val k = SoftBox(); val s = TokenStore(p, k)
        assertTrue(s.isStrict())
        s.seal(s.encryptCipher(false), "t", ivan, "u", "d", strict = false)
        assertFalse(s.isStrict())
        s.seal(s.encryptCipher(true), "t2", ivan, "u", "d", strict = true)
        assertTrue(s.isStrict())
        s.wipe()
        assertTrue(s.isStrict())
    }

    @Test fun `sealing twice uses a fresh key and iv each time and the old ciphertext is useless with the new key`() {
        val p = MemStore(); val k = SoftBox(); val s = TokenStore(p, k)
        s.seal(s.encryptCipher(true), "first-token", ivan, "u", "d")
        val oldCt = p.m["ct"]!!; val oldIv = p.m["iv"]!!
        s.seal(s.encryptCipher(true), "second-token", ivan, "u", "d")
        assertNotEquals(oldCt, p.m["ct"]); assertNotEquals(oldIv, p.m["iv"])
        assertEquals("second-token", s.open(s.decryptCipher()))
        p.m["ct"] = oldCt                                              // the old ciphertext under the new key (and the new iv) does not verify
        assertNull(s.open(s.decryptCipher()))
    }
}

package home.minis.portal

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.KeyPairGenerator
import java.security.Signature

class Ed25519Test {
    private fun h(s: String) = ManifestVerifier.hexToBytes(s)

    @Test fun rfc8032Vectors() {
        // RFC 8032 section 7.1, tests 1 and 2
        assertTrue(Ed25519.verify(h("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"), ByteArray(0),
            h("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")))
        assertTrue(Ed25519.verify(h("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"), h("72"),
            h("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")))
        assertFalse(Ed25519.verify(h("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"), h("73"),
            h("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")))
    }

    @Test fun agreesWithTheJdkAndRejectsMalformedInput() {
        val kp = KeyPairGenerator.getInstance("Ed25519").generateKeyPair()
        val pub = kp.public.encoded.takeLast(32).toByteArray()
        val msg = "каждый байт важен".toByteArray()
        val sig = Signature.getInstance("Ed25519").apply { initSign(kp.private); update(msg) }.sign()
        assertTrue(Ed25519.verify(pub, msg, sig))
        for (i in listOf(0, 31, 32, 63)) assertFalse(Ed25519.verify(pub, msg, sig.copyOf().also { it[i] = (it[i].toInt() xor 1).toByte() }))
        assertFalse(Ed25519.verify(pub, msg + 1, sig))
        assertFalse(Ed25519.verify(pub, msg, sig.copyOf(63)))
        assertFalse(Ed25519.verify(pub.copyOf(31), msg, sig))
        assertFalse(Ed25519.verify(ByteArray(32) { 0xFF.toByte() }, msg, sig))
    }
}

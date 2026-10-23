package home.minis.portal

import java.math.BigInteger
import java.security.MessageDigest

/**
 * Ed25519 signature VERIFICATION (RFC 8032), in plain Kotlin on java.math.BigInteger. The Android platform's own `Signature`/`KeyFactory` for Ed25519 is not reliably reachable
 * (on Android 15 `KeyFactory.getInstance("Ed25519")` resolves to the AndroidKeyStore provider, which refuses a public-key spec: found on the emulator), and the update channel must
 * not depend on which provider answers. This is the same reference algorithm as the gateway's gw/ed25519.py; the unit tests check it against signatures made by the gateway's
 * Python code and by the JDK's own Ed25519. Verification only: the app never signs anything.
 */
object Ed25519 {
    private val P = BigInteger.ONE.shiftLeft(255).subtract(BigInteger.valueOf(19))
    private val Q = BigInteger.ONE.shiftLeft(252).add(BigInteger("27742317777372353535851937790883648493"))
    private val D = BigInteger.valueOf(-121665).multiply(BigInteger.valueOf(121666).modInverse(P)).mod(P)
    private val SQRT_M1 = BigInteger.TWO.modPow(P.subtract(BigInteger.ONE).shiftRight(2), P)

    private class Pt(val x: BigInteger, val y: BigInteger, val z: BigInteger, val t: BigInteger)

    private fun add(a: Pt, b: Pt): Pt {
        val aa = a.y.subtract(a.x).multiply(b.y.subtract(b.x)).mod(P)
        val bb = a.y.add(a.x).multiply(b.y.add(b.x)).mod(P)
        val cc = a.t.multiply(b.t).multiply(D).shiftLeft(1).mod(P)
        val dd = a.z.multiply(b.z).shiftLeft(1).mod(P)
        val e = bb.subtract(aa)
        val f = dd.subtract(cc)
        val g = dd.add(cc)
        val h = bb.add(aa)
        return Pt(e.multiply(f).mod(P), g.multiply(h).mod(P), f.multiply(g).mod(P), e.multiply(h).mod(P))
    }

    private fun mul(scalar: BigInteger, point: Pt): Pt {
        var r = Pt(BigInteger.ZERO, BigInteger.ONE, BigInteger.ONE, BigInteger.ZERO)
        var p = point
        var s = scalar
        while (s.signum() > 0) {
            if (s.testBit(0)) r = add(r, p)
            p = add(p, p)
            s = s.shiftRight(1)
        }
        return r
    }

    private fun same(a: Pt, b: Pt): Boolean =
        a.x.multiply(b.z).subtract(b.x.multiply(a.z)).mod(P).signum() == 0 && a.y.multiply(b.z).subtract(b.y.multiply(a.z)).mod(P).signum() == 0

    private fun recoverX(y: BigInteger, sign: Int): BigInteger? {
        if (y >= P) return null
        val x2 = y.multiply(y).subtract(BigInteger.ONE).multiply(D.multiply(y).multiply(y).add(BigInteger.ONE).modInverse(P)).mod(P)
        if (x2.signum() == 0) return if (sign != 0) null else BigInteger.ZERO
        var x = x2.modPow(P.add(BigInteger.valueOf(3)).shiftRight(3), P)
        if (x.multiply(x).subtract(x2).mod(P).signum() != 0) x = x.multiply(SQRT_M1).mod(P)
        if (x.multiply(x).subtract(x2).mod(P).signum() != 0) return null
        if ((if (x.testBit(0)) 1 else 0) != sign) x = P.subtract(x)
        return x
    }

    private val G: Pt = run {
        val gy = BigInteger.valueOf(4).multiply(BigInteger.valueOf(5).modInverse(P)).mod(P)
        val gx = recoverX(gy, 0)!!
        Pt(gx, gy, BigInteger.ONE, gx.multiply(gy).mod(P))
    }

    private fun le(bytes: ByteArray, from: Int, to: Int): BigInteger = BigInteger(1, bytes.copyOfRange(from, to).reversedArray())

    private fun decompress(s: ByteArray): Pt? {
        if (s.size != 32) return null
        val sign = (s[31].toInt() shr 7) and 1
        val c = s.copyOf()
        c[31] = (c[31].toInt() and 0x7F).toByte()
        val y = le(c, 0, 32)
        val x = recoverX(y, sign) ?: return null
        return Pt(x, y, BigInteger.ONE, x.multiply(y).mod(P))
    }

    /** True only for a valid signature; false (never an exception) for anything malformed. */
    fun verify(pub: ByteArray, msg: ByteArray, sig: ByteArray): Boolean {
        return try {
            if (pub.size != 32 || sig.size != 64) return false
            val a = decompress(pub) ?: return false
            val rs = sig.copyOfRange(0, 32)
            val r = decompress(rs) ?: return false
            val s = le(sig, 32, 64)
            if (s >= Q) return false
            val md = MessageDigest.getInstance("SHA-512")
            md.update(rs); md.update(pub); md.update(msg)
            val h = le(md.digest(), 0, 64).mod(Q)
            same(mul(s, G), add(r, mul(h, a)))
        } catch (e: Exception) {
            false
        }
    }
}

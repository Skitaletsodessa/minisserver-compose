package home.minis.portal

/** When the fingerprint is asked again: at every launch (a fresh process starts locked) and after the app was in the background for 2 minutes. */
object LockPolicy {
    const val BACKGROUND_LIMIT_MS = 2 * 60 * 1000L

    /** [backgroundedAt] = monotonic time when the app left the screen, or null when it never did. A clock that went backwards locks (never trust a negative gap). */
    fun shouldLock(backgroundedAt: Long?, now: Long): Boolean {
        if (backgroundedAt == null) return false
        val gap = now - backgroundedAt
        return gap < 0 || gap >= BACKGROUND_LIMIT_MS
    }
}

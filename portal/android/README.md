# portal-android — the «Дом» app (Task 30, ADR 0009)

A thin native shell around the portal's web UI: sign in once with the person's own name and password (+ the 6-digit code for the admin), then open by fingerprint only.
One activity, no Google services, no analytics, no background work. `minSdk 30` (Android 11), `targetSdk 36` (Android 16), Kotlin, one dependency (`androidx.biometric 1.1.0`).

## Build (Windows, user-level tools, nothing installed system-wide)

```
JDK 17 (Temurin)           C:\Users\Skit\dev-tools\jdk-17
Android SDK                C:\Users\Skit\dev-tools\android-sdk   (platform 35+36, build-tools 36.0.0, cmdline-tools, platform-tools; emulator + system-images;android-35;google_apis;x86_64 for the emulator test)
Gradle                     the wrapper (8.14.3, sha256 pinned in gradle/wrapper/gradle-wrapper.properties)
Signing key                C:\Users\Skit\dev-keys\portal-release.jks + keystore.properties (OUTSIDE this repository and outside the server; see docs/portal/runbook-app-ru.md)
```

```powershell
$env:JAVA_HOME="C:\Users\Skit\dev-tools\jdk-17"; $env:ANDROID_HOME="C:\Users\Skit\dev-tools\android-sdk"
$env:PORTAL_KEYSTORE_PROPS="C:\Users\Skit\dev-keys\keystore.properties"     # without it the release APK is unsigned
.\gradlew.bat clean testDebugUnitTest lintRelease assembleRelease --no-daemon
& "$env:ANDROID_HOME\build-tools\36.0.0\apksigner.bat" verify --print-certs app\build\outputs\apk\release\app-release.apk
```

`versionCode` in `app/build.gradle.kts` must go up with every build that is handed out, never down (an update installs only over a lower versionCode signed with the same key).
The build is reproducible: two clean builds give byte-identical APKs (checked 2026-10-22, same machine).

## Tests
`app/src/test` (JVM, 33 tests): sign-in form, URL policy, lock policy, error mapping, gateway client against a fake transport (incl. the total-timeout), token store with a software key.
The emulator test (sign-in → fingerprint → pages, lock, revoke, new fingerprint, sign-out, offline) used the DEBUG build: `app/src/debug/` holds a throw-away self-signed certificate (public part only) that only debug builds trust, and debug builds skip FLAG_SECURE so screenshots work. The release build trusts the system CAs only and sets FLAG_SECURE.
The server side of the emulator test is `portal/gateway/tools/app_scratch.py` (scratch gateway + TLS proxy, synthetic accounts).

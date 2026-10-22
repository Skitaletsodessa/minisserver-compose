import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// The signing key lives OUTSIDE the repository and outside the server. Point PORTAL_KEYSTORE_PROPS at a properties file (storeFile, storePassword, keyAlias, keyPassword)
// kept in a user-profile folder; without it the release build is simply unsigned (assembleRelease still works, the APK cannot be installed until it is signed).
val signingProps = Properties().apply {
    val p = System.getenv("PORTAL_KEYSTORE_PROPS")
    if (p != null && File(p).isFile) File(p).inputStream().use { load(it) }
}

android {
    namespace = "home.minis.portal"
    compileSdk = 36

    defaultConfig {
        applicationId = "home.minis.portal"
        minSdk = 30              // Android 11: fingerprint-bound Keystore keys that the device PIN can also unlock through the system prompt
        targetSdk = 36           // Android 16
        versionCode = 1          // must go up with every build and never down: an update installs only over a lower versionCode with the same signing key
        versionName = "1.0.0"
    }

    signingConfigs {
        if (signingProps.containsKey("storeFile")) {
            create("release") {
                storeFile = File(signingProps.getProperty("storeFile"))
                storePassword = signingProps.getProperty("storePassword")
                keyAlias = signingProps.getProperty("keyAlias")
                keyPassword = signingProps.getProperty("keyPassword")
                enableV1Signing = false
                enableV2Signing = true
                enableV3Signing = true
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures { buildConfig = true }

    lint {
        abortOnError = true
        warningsAsErrors = false
        checkReleaseBuilds = true
    }
    packaging {
        resources { excludes += setOf("META-INF/AL2.0", "META-INF/LGPL2.1") }
    }
    dependenciesInfo {                          // no dependency-metadata blob in the APK (it would only make the build less reproducible)
        includeInApk = false
        includeInBundle = false
    }
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
}

dependencies {
    implementation("androidx.biometric:biometric:1.1.0")     // BiometricPrompt; pulls androidx.core / fragment / appcompat (Apache-2.0)
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")             // org.json for the JVM unit tests (Android has it built in)
}

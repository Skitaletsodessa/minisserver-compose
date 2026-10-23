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
        minSdk = 33              // Android 13: every family phone is Android 13+ (Ivan, 2026-10-22); the platform then has Ed25519 (the update manifest signature) without an extra library
        targetSdk = 36           // Android 16
        versionCode = (project.findProperty("vc") as String? ?: "4").toInt()          // must go up with every build and never down: an update installs only over a lower versionCode with the same signing key
        versionName = (project.findProperty("vn") as String? ?: "1.1.0")
        // Task 30c: the update manifest is verified with one of these public keys (raw Ed25519, hex): k1 = the server key (made on the host by tools/release_keygen.py), k2 = a spare whose private half is in Ivan's dev-keys
        buildConfigField("String", "MANIFEST_PUBKEYS", "\"ce618a1f6d5a0f2f2efc562798f1ba0f53f8e1b6cd885aaa7062bf44ae492bd0,9244e426f36b5d024cee84a25e04dfdcf73b2a745094a50e1f9b89cce230eadc\"")
        buildConfigField("String", "PINNED_CERT_SHA256", "\"2d17dd84230ef69f61740d569be5c6c4ccd918ac7a2d6da89adf5d4cacca4dfc\"")
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
        debug {
            // the emulator tests run against a scratch gateway whose manifest key is the TEST seed (bytes 0..31); only debug builds (which trust only the test CA anyway) carry it
            buildConfigField("String", "MANIFEST_PUBKEYS", "\"03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8\"")
            buildConfigField("String", "PINNED_CERT_SHA256", "\"\"")
        }
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

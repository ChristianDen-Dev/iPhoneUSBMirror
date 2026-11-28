plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Signing key comes from GitHub secrets (never committed). Same key every build,
// so a new APK installs over the old one and keeps your settings.
val ksFile = System.getenv("DDL_KEYSTORE")
val ksPass = System.getenv("DDL_KEYSTORE_PASSWORD")

android {
    namespace = "com.dendev.live"
    compileSdk = 37

    defaultConfig {
        applicationId = "com.dendev.live"
        minSdk = 29                 // Android 10: needed to record the game sound
        targetSdk = 37
        versionCode = 1
        versionName = "1.0"
    }
    signingConfigs {
        if (ksFile != null && ksPass != null) {
            create("release") {
                storeFile = file(ksFile)
                storePassword = ksPass
                keyAlias = "dendevlive"
                keyPassword = ksPass
                storeType = "pkcs12"
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }
    buildFeatures { compose = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("com.github.pedroSG94.RootEncoder:library:2.8.2")
    implementation(platform("androidx.compose:compose-bom:2025.01.00"))
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.activity:activity-compose:1.10.0")
    implementation("androidx.core:core-ktx:1.15.0")
}

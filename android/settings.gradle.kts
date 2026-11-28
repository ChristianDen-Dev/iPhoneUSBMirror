// Den-Dev Live for Android (Go Live from the phone, floating comments, Studio look).
// Built in the cloud by .github/workflows/build-android.yml (no Android Studio needed).
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        maven { url = uri("https://jitpack.io") }      // RootEncoder (RTMP streaming library)
    }
}
rootProject.name = "DenDevLive"
include(":app")

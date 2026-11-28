// Settings of Den-Dev Live for Android: platforms, quality, stream look.
// The stream key is encrypted with a key that never leaves the phone
// (Android Keystore) and is never sent anywhere except to the live server.

package com.dendev.live

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

enum class Platform(val title: String, val server: String, val keyHelp: String, val keyPage: String, val chatPage: String) {
    FACEBOOK("Facebook", "rtmps://live-api-s.facebook.com:443/rtmp/",
        "facebook.com/live/producer → Streaming software → turn on \"Use a persistent stream key\" → copy the key.",
        "https://www.facebook.com/live/producer", "https://www.facebook.com/live/producer"),
    YOUTUBE("YouTube", "rtmps://a.rtmps.youtube.com:443/live2",
        "YouTube Studio → Create → Go live → Stream → copy the stream key.",
        "https://studio.youtube.com", "https://studio.youtube.com"),
    TIKTOK("TikTok", "",
        "Only accounts with LIVE access get a server URL + stream key from TikTok. Paste both.",
        "https://www.tiktok.com/live/studio", "https://www.tiktok.com/live"),
    CUSTOM("Custom", "", "Any RTMP / RTMPS server: paste its server URL and stream key.", "", "");
}

data class Quality(val id: String, val title: String, val detail: String,
                   val width: Int, val height: Int, val fps: Int, val bitrate: Int) {
    companion object {
        val all = listOf(
            Quality("saver", "Data saver", "480p · 30 fps", 854, 480, 30, 1_200_000),
            Quality("hd", "HD", "720p · 30 fps", 1280, 720, 30, 3_000_000),
            Quality("hd60", "HD Smooth", "720p · 60 fps", 1280, 720, 60, 4_500_000),
            Quality("fhd", "Full HD", "1080p · 30 fps", 1920, 1080, 30, 6_000_000),
        )
        fun find(id: String) = all.firstOrNull { it.id == id } ?: all[1]
    }
}

/** Everything the live needs. Read again every second while live (look + controls). */
data class LiveConfig(
    val platform: Platform = Platform.FACEBOOK,
    val server: String = "",
    val key: String = "",
    val quality: Quality = Quality.find("hd"),
    val portrait: Boolean = false,
    val fill: Boolean = false,              // false = fit (black bars), true = fill (crop)
    val gameVolume: Float = 1f,
    val micVolume: Float = 1f,
    val micMuted: Boolean = false,
    val privacy: Boolean = false,           // viewers see "Be right back"
    val logoPath: String = "",              // PNG in the app's files ("" = none)
    val logoCorner: Int = 1,                // 0 TL, 1 TR, 2 BL, 3 BR
    val logoScale: Float = 0.16f,
    val banner: String = "",
    val bannerColor: Int = 0,               // 0 red, 1 blue, 2 black, 3 purple
    val badge: Boolean = true,
    val brbText: String = "Be right back",
    val chatLink: String = "",
    val chatOpacity: Float = 0.8f,
) {
    /** rtmp(s)://server/app + "/" + key */
    val publishUrl: String?
        get() {
            var s = server.trim()
            val k = key.trim()
            if (s.isEmpty() || k.isEmpty()) return null
            if (!s.startsWith("rtmp://") && !s.startsWith("rtmps://")) return null
            while (s.endsWith("/")) s = s.dropLast(1)
            return "$s/$k"
        }

    /** The page the comment reader opens (a YouTube video link becomes its live chat). */
    val chatUrl: String
        get() {
            val l = chatLink.trim()
            if (l.isEmpty()) return platform.chatPage
            val full = if (l.lowercase().startsWith("http")) l else "https://$l"
            youTubeId(full)?.let { return "https://www.youtube.com/live_chat?is_popout=1&v=$it" }
            return full
        }

    private fun youTubeId(u: String): String? {
        val m1 = Regex("youtu\\.be/([\\w-]{6,})").find(u)
        if (m1 != null) return m1.groupValues[1]
        if (!u.contains("youtube.com") || u.contains("studio.")) return null
        Regex("[?&]v=([\\w-]{6,})").find(u)?.let { return it.groupValues[1] }
        Regex("/(?:live|shorts)/([\\w-]{6,})").find(u)?.let { return it.groupValues[1] }
        return null
    }
}

/** Saved settings (plain prefs + encrypted stream keys). */
class Prefs(context: Context) {
    private val p = context.getSharedPreferences("dendev", Context.MODE_PRIVATE)

    fun load(): LiveConfig {
        val plat = runCatching { Platform.valueOf(p.getString("platform", "FACEBOOK")!!) }.getOrDefault(Platform.FACEBOOK)
        return LiveConfig(
            platform = plat,
            server = p.getString("server.$plat", null) ?: plat.server,
            key = KeyBox.decrypt(p.getString("key.$plat", "") ?: ""),
            quality = Quality.find(p.getString("quality", "hd")!!),
            portrait = p.getBoolean("portrait", false),
            fill = p.getBoolean("fill", false),
            gameVolume = p.getFloat("gameVolume", 1f),
            micVolume = p.getFloat("micVolume", 1f),
            micMuted = p.getBoolean("micMuted", false),
            privacy = p.getBoolean("privacy", false),
            logoPath = p.getString("logoPath", "")!!,
            logoCorner = p.getInt("logoCorner", 1),
            logoScale = p.getFloat("logoScale", 0.16f),
            banner = p.getString("banner", "")!!,
            bannerColor = p.getInt("bannerColor", 0),
            badge = p.getBoolean("badge", true),
            brbText = p.getString("brb", "Be right back")!!,
            chatLink = p.getString("chat.$plat", "")!!,
            chatOpacity = p.getFloat("chatOpacity", 0.8f),
        )
    }

    fun save(c: LiveConfig) {
        p.edit()
            .putString("platform", c.platform.name)
            .putString("server.${c.platform}", c.server)
            .putString("key.${c.platform}", KeyBox.encrypt(c.key))
            .putString("quality", c.quality.id)
            .putBoolean("portrait", c.portrait)
            .putBoolean("fill", c.fill)
            .putFloat("gameVolume", c.gameVolume)
            .putFloat("micVolume", c.micVolume)
            .putBoolean("micMuted", c.micMuted)
            .putBoolean("privacy", c.privacy)
            .putString("logoPath", c.logoPath)
            .putInt("logoCorner", c.logoCorner)
            .putFloat("logoScale", c.logoScale)
            .putString("banner", c.banner)
            .putInt("bannerColor", c.bannerColor)
            .putBoolean("badge", c.badge)
            .putString("brb", c.brbText)
            .putString("chat.${c.platform}", c.chatLink)
            .putFloat("chatOpacity", c.chatOpacity)
            .apply()
    }

    /** Switching platform: load that platform's own server / key / chat link. */
    fun forPlatform(c: LiveConfig, plat: Platform): LiveConfig = c.copy(
        platform = plat,
        server = p.getString("server.$plat", null) ?: plat.server,
        key = KeyBox.decrypt(p.getString("key.$plat", "") ?: ""),
        chatLink = p.getString("chat.$plat", "")!!,
    )
}

/** AES-GCM with a key kept in the Android Keystore (cannot be copied off the phone). */
object KeyBox {
    private const val ALIAS = "dendev.streamkey"

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .build())
        return gen.generateKey()
    }

    fun encrypt(plain: String): String {
        if (plain.isEmpty()) return ""
        return runCatching {
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.ENCRYPT_MODE, key())
            val out = c.iv + c.doFinal(plain.toByteArray())
            Base64.encodeToString(out, Base64.NO_WRAP)
        }.getOrDefault("")
    }

    fun decrypt(box: String): String {
        if (box.isEmpty()) return ""
        return runCatching {
            val all = Base64.decode(box, Base64.NO_WRAP)
            val c = Cipher.getInstance("AES/GCM/NoPadding")
            c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, all, 0, 12))
            String(c.doFinal(all, 12, all.size - 12))
        }.getOrDefault("")
    }
}

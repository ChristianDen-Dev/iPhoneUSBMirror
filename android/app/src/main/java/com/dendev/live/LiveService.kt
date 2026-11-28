// The live: screen + game sound + your voice -> RTMP(S) server (Facebook,
// YouTube, TikTok, custom). Runs as a foreground service (notification) so it
// keeps going while you play.
//
// Streaming library: RootEncoder (pedroSG94, Apache-2.0).
//   ScreenSource   = the screen (MediaProjection)
//   MixAudioSource = game sound (AudioPlaybackCapture) + microphone, mixed
//                    on the phone; Android does NOT turn the game down for this.

package com.dendev.live

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings as AndroidSettings
import android.util.Log
import com.pedro.common.ConnectChecker
import com.pedro.encoder.input.gl.render.filters.`object`.ImageFilterRender
import com.pedro.encoder.input.sources.audio.InternalAudioSource
import com.pedro.encoder.input.sources.audio.MixAudioSource
import com.pedro.encoder.input.sources.video.ScreenSource
import com.pedro.encoder.utils.gl.AspectRatioMode
import com.pedro.library.generic.GenericStream
import com.pedro.library.util.BitrateAdapter
import kotlinx.coroutines.flow.MutableStateFlow

/** Live status shown in the app and the floating buttons. */
data class LiveStatus(
    val state: String = "idle",         // idle / connecting / live / reconnecting / error / ended
    val seconds: Int = 0,
    val kbps: Int = 0,
    val reconnects: Int = 0,
    val message: String = "",
    val gameSound: Boolean = true,      // false = the game blocks sound recording
)

/** Shared between the screens and the service. */
object Live {
    val config = MutableStateFlow(LiveConfig())
    val status = MutableStateFlow(LiveStatus())
    val running get() = status.value.state in setOf("connecting", "live", "reconnecting")
}

class LiveService : Service(), ConnectChecker {
    companion object {
        private const val TAG = "DenDevLive"
        private const val CHANNEL = "live"
        private const val NOTE_ID = 7
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val ACTION_MIC = "mic"
        const val ACTION_SCREEN = "screen"
        const val EXTRA_CODE = "code"
        const val EXTRA_DATA = "data"

        fun send(context: Context, action: String) {
            context.startService(Intent(context, LiveService::class.java).setAction(action))
        }
    }

    private val main = Handler(Looper.getMainLooper())
    private var projection: MediaProjection? = null
    private var stream: GenericStream? = null
    private var mix: MixAudioSource? = null
    private var look: ImageFilterRender? = null
    private var bitrate: BitrateAdapter? = null
    private var startedAt = 0L
    private var lastLook: LiveConfig? = null
    private var lastSecond = -1
    private var wake: PowerManager.WakeLock? = null
    private var overlay: Overlay? = null
    private var width = 1280
    private var height = 720
    private var stopping = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                val code = intent.getIntExtra(EXTRA_CODE, 0)
                @Suppress("DEPRECATION")
                val data: Intent? = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(EXTRA_DATA, Intent::class.java)
                                    else intent.getParcelableExtra(EXTRA_DATA)
                if (data == null) { stopSelf(); return START_NOT_STICKY }
                begin(code, data)
            }
            ACTION_STOP -> end("ended")
            ACTION_MIC -> Live.config.value = Live.config.value.copy(micMuted = !Live.config.value.micMuted).also { Prefs(this).save(it) }
            ACTION_SCREEN -> Live.config.value = Live.config.value.copy(privacy = !Live.config.value.privacy).also { Prefs(this).save(it) }
        }
        return START_NOT_STICKY
    }

    // ---------------- start / stop ----------------

    private fun begin(code: Int, data: Intent) {
        val cfg = Live.config.value
        val url = cfg.publishUrl
        if (url == null) { setStatus { it.copy(state = "error", message = "Server or stream key missing.") }; stopSelf(); return }
        stopping = false
        val hasMic = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

        // Android 14+: the notification must be up BEFORE the screen capture is used
        val type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or
                   (if (hasMic) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0)
        startForeground(NOTE_ID, notification("Connecting…"), type)

        val mpm = getSystemService(MediaProjectionManager::class.java)
        val mp = runCatching { mpm.getMediaProjection(code, data) }.getOrNull()
        if (mp == null) { fail("Screen capture was not allowed."); return }
        projection = mp
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { main.post { end("ended") } }      // "Stop sharing" from Android
        }, main)

        val q = cfg.quality
        width = if (cfg.portrait) q.height else q.width
        height = if (cfg.portrait) q.width else q.height
        val audio = if (hasMic) MixAudioSource(mp).also { mix = it } else InternalAudioSource(mp)
        val s = GenericStream(this, this, ScreenSource(this, mp), audio)
        stream = s
        val ok = runCatching {
            s.prepareVideo(q.width, q.height, q.bitrate, q.fps, 2, if (cfg.portrait) 90 else 0) &&
                s.prepareAudio(48_000, true, 128_000)
        }.getOrDefault(false)
        if (!ok) { fail("This phone cannot encode ${q.title} (${q.detail}). Pick a lower quality."); return }

        val gl = s.getGlInterface()
        gl.setForceRender(true, q.fps)          // keeps sending frames when the screen is still
        gl.setAspectRatioMode(if (cfg.fill) AspectRatioMode.Fill else AspectRatioMode.Adjust)
        s.getStreamClient().setReTries(30)
        bitrate = BitrateAdapter { b -> s.setVideoBitrateOnFly(b) }.apply { setMaxBitrate(q.bitrate) }

        wake = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DenDevLive:live").apply { acquire(6 * 60 * 60 * 1000L) }
        setStatus { LiveStatus(state = "connecting") }
        applyControls(cfg)
        s.startStream(url)

        look = ImageFilterRender().also { gl.addFilter(it) }
        lastLook = null; lastSecond = -1
        if (AndroidSettings.canDrawOverlays(this)) overlay = Overlay(this).also { it.show() }
        main.post(ticker)
    }

    private fun end(state: String) {
        if (stopping) return
        stopping = true
        main.removeCallbacks(ticker)
        overlay?.hide(); overlay = null
        runCatching { stream?.let { if (it.isStreaming) it.stopStream(); it.release() } }
        stream = null; mix = null; look = null
        runCatching { projection?.stop() }
        projection = null
        runCatching { wake?.release() }
        wake = null
        startedAt = 0L
        setStatus { it.copy(state = state, kbps = 0) }
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun fail(msg: String) {
        Log.w(TAG, "live stopped: $msg")
        setStatus { it.copy(state = "error", message = msg) }
        end("error")
        setStatus { it.copy(state = "error", message = msg) }
    }

    override fun onDestroy() {
        end(if (Live.status.value.state == "error") "error" else "ended")
        super.onDestroy()
    }

    // ---------------- every second: look, controls, status ----------------

    private val ticker = object : Runnable {
        override fun run() {
            val s = stream ?: return
            val cfg = Live.config.value
            applyControls(cfg)
            val sec = if (startedAt > 0) ((System.currentTimeMillis() - startedAt) / 1000).toInt() else -1
            if (cfg != lastLook || (cfg.badge && sec != lastSecond)) {
                look?.setImage(StreamLook.draw(width, height, cfg, if (sec >= 0) sec else null))
                lastLook = cfg; lastSecond = sec
            }
            if (sec >= 0) setStatus { it.copy(seconds = sec) }
            updateNotification()
            if (s.isStreaming || Live.running) main.postDelayed(this, 1000)
        }
    }

    private fun applyControls(cfg: LiveConfig) {
        mix?.let {
            it.internalVolume = cfg.gameVolume
            it.microphoneVolume = if (cfg.micMuted) 0f else cfg.micVolume
        }
    }

    // ---------------- connection (ConnectChecker, called from a background thread) ----------------

    override fun onConnectionStarted(url: String) {}

    override fun onConnectionSuccess() {
        main.post {
            if (startedAt == 0L) startedAt = System.currentTimeMillis()
            setStatus { it.copy(state = "live", message = "") }
        }
    }

    override fun onConnectionFailed(reason: String) {
        main.post {
            val s = stream ?: return@post
            Log.w(TAG, "connection failed: $reason")
            if (s.getStreamClient().reTry(3000, reason)) {        // try again in 3 s (up to 30 times)
                setStatus { it.copy(state = "reconnecting", reconnects = it.reconnects + 1, message = reason) }
            } else {
                fail("Could not connect: $reason. Check the internet, the stream key and that the live is set up on the website.")
            }
        }
    }

    override fun onNewBitrate(bitrate: Long) {
        this.bitrate?.adaptBitrate(bitrate, stream?.getStreamClient()?.hasCongestion() ?: false)
        main.post { setStatus { it.copy(kbps = (bitrate / 1000).toInt()) } }
    }

    override fun onDisconnect() {}
    override fun onAuthError() { main.post { fail("The server refused the stream key.") } }
    override fun onAuthSuccess() {}

    // ---------------- notification ----------------

    private fun setStatus(f: (LiveStatus) -> LiveStatus) { Live.status.value = f(Live.status.value) }

    private fun notification(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Live", NotificationManager.IMPORTANCE_LOW))
        }
        fun action(title: String, act: String, req: Int) = Notification.Action.Builder(null, title,
            PendingIntent.getService(this, req, Intent(this, LiveService::class.java).setAction(act),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)).build()
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)
        val cfg = Live.config.value
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_note)
            .setContentTitle("Den-Dev Live")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(action(if (cfg.micMuted) "Mic on" else "Mic off", ACTION_MIC, 1))
            .addAction(action(if (cfg.privacy) "Show screen" else "Hide screen", ACTION_SCREEN, 2))
            .addAction(action("Stop", ACTION_STOP, 3))
            .build()
    }

    private fun updateNotification() {
        val st = Live.status.value
        val text = when (st.state) {
            "live" -> "LIVE %d:%02d · %d kbps".format(st.seconds / 60, st.seconds % 60, st.kbps)
            "reconnecting" -> "Reconnecting…"
            else -> "Connecting…"
        }
        getSystemService(NotificationManager::class.java).notify(NOTE_ID, notification(text))
    }
}

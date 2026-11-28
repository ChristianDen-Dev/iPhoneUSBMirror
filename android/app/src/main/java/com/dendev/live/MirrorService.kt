// Mirror this Android phone to the laptop over Wi-Fi, exactly like the iPhone
// app: the phone listens on port 2345 (picture + sound) and 2346 (sound only),
// the laptop program finds it by itself (same wire format, docs/PROTOCOL.md).
//
// Picture: screen -> H.264 hardware encoder (Annex-B), VIDEO_CONFIG before each keyframe.
// Sound:   game sound (AudioPlaybackCapture) -> AUDIO_PCM, your mic -> AUDIO_MIC,
//          48 kHz 16-bit stereo. The laptop mixes the mic into the stream only.
// USB on Android needs no app (the laptop uses adb + scrcpy-server).

package com.dendev.live

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings as AndroidSettings
import android.util.DisplayMetrics
import android.util.Log
import android.view.Display
import kotlinx.coroutines.flow.MutableStateFlow
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

data class MirrorStatus(
    val running: Boolean = false,
    val connected: Boolean = false,       // the laptop is receiving
    val width: Int = 0,
    val height: Int = 0,
    val message: String = "",
)

object Mirror {
    val status = MutableStateFlow(MirrorStatus())
    val micOn = MutableStateFlow(false)   // your voice to the laptop stream
}

class MirrorService : Service() {
    companion object {
        private const val TAG = "DenDevMirror"
        private const val CHANNEL = "mirror"
        private const val NOTE_ID = 8
        const val ACTION_START = "start"
        const val ACTION_STOP = "stop"
        const val ACTION_MIC = "mic"
        const val MAIN_PORT = 2345
        const val AUDIO_PORT = 2346

        // packet types (docs/PROTOCOL.md)
        const val T_VIDEO_CONFIG = 1
        const val T_VIDEO_FRAME = 2
        const val T_AUDIO_PCM = 3
        const val T_ORIENTATION = 4
        const val T_HEARTBEAT = 5
        const val T_INFO = 6
        const val T_AUDIO_MIC = 7

        fun packet(type: Int, flags: Int, tsUs: Long, payload: ByteArray, off: Int = 0, len: Int = payload.size): ByteArray {
            val b = ByteBuffer.allocate(14 + len).order(ByteOrder.BIG_ENDIAN)
            b.put(type.toByte()); b.put(flags.toByte()); b.putLong(tsUs); b.putInt(len)
            b.put(payload, off, len)
            return b.array()
        }
        fun nowUs() = System.nanoTime() / 1000
    }

    /** One laptop connection with its own sender thread (never blocks the encoder). */
    private inner class Client(val socket: Socket, val name: String) {
        val queue = LinkedBlockingQueue<ByteArray>()
        val queued = AtomicLong(0)
        @Volatile var alive = true
        private val out: OutputStream = socket.getOutputStream()

        init {
            socket.tcpNoDelay = true
            socket.sendBufferSize = 1 shl 20
            thread(name = "send-$name", isDaemon = true) {
                try {
                    while (alive) {
                        val p = queue.take()
                        if (p.isEmpty()) break
                        out.write(p)
                        queued.addAndGet(-p.size.toLong())
                    }
                } catch (_: Exception) {
                }
                close()
            }
        }
        fun send(p: ByteArray) { if (alive) { queued.addAndGet(p.size.toLong()); queue.offer(p) } }
        fun close() {
            if (!alive) return
            alive = false
            queue.offer(ByteArray(0))
            runCatching { socket.close() }
            main.post { refreshStatus() }
        }
    }

    private val main = Handler(Looper.getMainLooper())
    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null
    @Volatile private var encoder: MediaCodec? = null
    @Volatile private var config: ByteArray? = null     // SPS + PPS (Annex-B)
    @Volatile private var needKey = true
    @Volatile private var running = false
    @Volatile private var mainClient: Client? = null
    @Volatile private var audioClient: Client? = null
    private var servers = mutableListOf<ServerSocket>()
    private var width = 0
    private var height = 0
    private var dpi = 320
    private var overlay: Overlay? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                val code = intent.getIntExtra(LiveService.EXTRA_CODE, 0)
                @Suppress("DEPRECATION")
                val data: Intent? = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(LiveService.EXTRA_DATA, Intent::class.java)
                                    else intent.getParcelableExtra(LiveService.EXTRA_DATA)
                if (data == null) { stopSelf(); return START_NOT_STICKY }
                begin(code, data)
            }
            ACTION_STOP -> stopAll()
            ACTION_MIC -> Mirror.micOn.value = !Mirror.micOn.value
        }
        return START_NOT_STICKY
    }

    // ---------------- start / stop ----------------

    private fun begin(code: Int, data: Intent) {
        if (running) return
        val hasMic = checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        val type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION or
                   (if (hasMic) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0)
        startForeground(NOTE_ID, notification("Waiting for the laptop…"), type)
        val mp = runCatching { getSystemService(MediaProjectionManager::class.java).getMediaProjection(code, data) }.getOrNull()
        if (mp == null) { Mirror.status.value = MirrorStatus(message = "Screen capture was not allowed."); stopAll(); return }
        projection = mp
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { main.post { stopAll() } }
        }, main)
        running = true

        measure()
        val enc = makeEncoder(width, height)
        display = mp.createVirtualDisplay("DenDevMirror", width, height, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, enc.createInputSurface().also { enc.start() }, null, null)
        startEncoderLoop(enc)
        getSystemService(DisplayManager::class.java).registerDisplayListener(rotationWatch, main)

        listen(MAIN_PORT) { s -> onMainClient(s) }
        listen(AUDIO_PORT) { s -> audioClient?.close(); audioClient = Client(s, "audio") }
        startGameSound(mp)
        startMic()
        thread(name = "heartbeat", isDaemon = true) {
            while (running) {
                val hb = packet(T_HEARTBEAT, 0, nowUs(), ByteArray(0))
                mainClient?.send(hb); audioClient?.send(hb)
                Thread.sleep(1000)
            }
        }
        if (AndroidSettings.canDrawOverlays(this)) overlay = Overlay(this, mirror = true).also { it.show() }
        refreshStatus()
    }

    private fun stopAll() {
        if (!running && projection == null) { stopSelf(); return }
        running = false
        overlay?.hide(); overlay = null
        runCatching { getSystemService(DisplayManager::class.java).unregisterDisplayListener(rotationWatch) }
        servers.forEach { runCatching { it.close() } }
        servers.clear()
        mainClient?.close(); audioClient?.close()
        mainClient = null; audioClient = null
        runCatching { display?.release() }
        display = null
        encoder?.let { e -> encoder = null; runCatching { e.stop() }; runCatching { e.release() } }
        runCatching { projection?.stop() }
        projection = null
        Mirror.status.value = MirrorStatus()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() { stopAll(); super.onDestroy() }

    // ---------------- network ----------------

    private fun listen(port: Int, onClient: (Socket) -> Unit) {
        val ss = ServerSocket()
        ss.reuseAddress = true
        try {
            ss.bind(InetSocketAddress(port))
        } catch (e: Exception) {
            Log.w(TAG, "port $port busy: $e"); return
        }
        servers += ss
        thread(name = "listen-$port", isDaemon = true) {
            while (running) {
                val s = runCatching { ss.accept() }.getOrNull() ?: break
                onClient(s)
            }
        }
    }

    private fun onMainClient(s: Socket) {
        mainClient?.close()                       // one laptop at a time
        val c = Client(s, "main")
        mainClient = c
        c.send(packet(T_ORIENTATION, 0, nowUs(), byteArrayOf(1)))     // already upright
        c.send(packet(T_INFO, 0, nowUs(), "Android ${Build.MANUFACTURER} ${Build.MODEL} (Android ${Build.VERSION.RELEASE})".toByteArray()))
        requestKey()
        main.post { refreshStatus() }
    }

    private fun refreshStatus() {
        val connected = mainClient?.alive == true
        Mirror.status.value = MirrorStatus(running = running, connected = connected, width = width, height = height)
        if (running) getSystemService(NotificationManager::class.java)
            .notify(NOTE_ID, notification(if (connected) "Mirroring to the laptop" else "Waiting for the laptop…"))
    }

    // ---------------- picture ----------------

    /** Screen size now (long side max 1920, multiples of 16). */
    @SuppressLint("NewApi")
    private fun measure() {
        val d = getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
        val m = DisplayMetrics()
        @Suppress("DEPRECATION") d.getRealMetrics(m)
        dpi = m.densityDpi
        val scale = minOf(1f, 1920f / maxOf(m.widthPixels, m.heightPixels))
        width = ((m.widthPixels * scale).toInt() / 16) * 16
        height = ((m.heightPixels * scale).toInt() / 16) * 16
    }

    private fun makeEncoder(w: Int, h: Int): MediaCodec {
        val f = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h)
        f.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
        f.setInteger(MediaFormat.KEY_BIT_RATE, 12_000_000)
        f.setInteger(MediaFormat.KEY_FRAME_RATE, 60)
        f.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2)
        f.setLong(MediaFormat.KEY_REPEAT_PREVIOUS_FRAME_AFTER, 500_000)   // still screen: repeat the picture
        f.setInteger(MediaFormat.KEY_PRIORITY, 0)                         // real time
        if (Build.VERSION.SDK_INT >= 30) f.setInteger(MediaFormat.KEY_LATENCY, 1)
        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        codec.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        encoder = codec
        return codec
    }

    private fun requestKey() {
        needKey = true
        encoder?.let { e -> runCatching { e.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0) }) } }
    }

    private fun startEncoderLoop(codec: MediaCodec) {
        thread(name = "encoder", isDaemon = true) {
            val info = MediaCodec.BufferInfo()
            val w = width; val h = height
            try {
                while (running && encoder === codec) {
                    val i = codec.dequeueOutputBuffer(info, 100_000)
                    if (i < 0) continue
                    val buf = codec.getOutputBuffer(i)
                    if (buf != null && info.size > 0) {
                        val bytes = ByteArray(info.size)
                        buf.position(info.offset); buf.get(bytes)
                        if (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) {
                            config = bytes
                        } else {
                            sendFrame(bytes, info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME != 0, info.presentationTimeUs, w, h)
                        }
                    }
                    codec.releaseOutputBuffer(i, false)
                }
            } catch (e: Exception) {
                Log.w(TAG, "encoder loop ended: $e")
            }
        }
    }

    private fun sendFrame(frame: ByteArray, key: Boolean, tsUs: Long, w: Int, h: Int) {
        val c = mainClient ?: return
        if (!c.alive) return
        if (c.queued.get() > 1_000_000 && !key) { requestKey(); return }   // laptop busy: skip until the next keyframe
        if (needKey && !key) return
        if (key) {
            needKey = false
            val cfg = config ?: return
            val head = ByteBuffer.allocate(4 + cfg.size).order(ByteOrder.BIG_ENDIAN)
            head.putShort(w.toShort()); head.putShort(h.toShort()); head.put(cfg)
            c.send(packet(T_VIDEO_CONFIG, 0, tsUs, head.array()))
        }
        c.send(packet(T_VIDEO_FRAME, if (key) 1 else 0, tsUs, frame))
    }

    /** Phone turned (portrait <-> landscape): new encoder size, same screen capture. */
    private val rotationWatch = object : DisplayManager.DisplayListener {
        override fun onDisplayChanged(displayId: Int) {
            if (displayId != Display.DEFAULT_DISPLAY || !running) return
            val oldW = width; val oldH = height
            measure()
            if (width == oldW && height == oldH) return
            Log.i(TAG, "rotation: ${width}x$height")
            val old = encoder
            val enc = makeEncoder(width, height)
            val surface = enc.createInputSurface()
            enc.start()
            display?.resize(width, height, dpi)
            display?.surface = surface
            startEncoderLoop(enc)
            old?.let { runCatching { it.stop() }; runCatching { it.release() } }
            config = null
            requestKey()
            refreshStatus()
        }
        override fun onDisplayAdded(displayId: Int) {}
        override fun onDisplayRemoved(displayId: Int) {}
    }

    // ---------------- sound ----------------

    private fun audioTarget(): Client? = audioClient?.takeIf { it.alive } ?: mainClient?.takeIf { it.alive }

    @SuppressLint("MissingPermission")
    private fun startGameSound(mp: MediaProjection) {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) return
        thread(name = "game-sound", isDaemon = true) {
            val cfg = AudioPlaybackCaptureConfiguration.Builder(mp)
                .addMatchingUsage(AudioAttributes.USAGE_GAME)
                .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)
                .build()
            val fmt = AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(48_000).setChannelMask(AudioFormat.CHANNEL_IN_STEREO).build()
            val rec = runCatching {
                AudioRecord.Builder().setAudioFormat(fmt).setAudioPlaybackCaptureConfig(cfg)
                    .setBufferSizeInBytes(48_000 * 4 / 5).build()
            }.getOrNull() ?: return@thread
            val buf = ByteArray(960 * 4)                      // 20 ms
            rec.startRecording()
            try {
                while (running) {
                    val n = rec.read(buf, 0, buf.size)
                    if (n <= 0) continue
                    val ts = nowUs() - n / 4 * 1_000_000L / 48_000
                    audioTarget()?.let { c -> if (c.queued.get() < 2_000_000) c.send(packet(T_AUDIO_PCM, 0, ts, buf, 0, n)) }
                }
            } finally {
                runCatching { rec.stop() }; rec.release()
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun startMic() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) return
        thread(name = "mic", isDaemon = true) {
            var rec: AudioRecord? = null
            val mono = ByteArray(960 * 2)
            val stereo = ByteArray(960 * 4)
            try {
                while (running) {
                    if (!Mirror.micOn.value) {
                        rec?.let { runCatching { it.stop() }; it.release() }; rec = null
                        Thread.sleep(200); continue
                    }
                    if (rec == null) {
                        rec = runCatching {
                            AudioRecord(MediaRecorder.AudioSource.MIC, 48_000, AudioFormat.CHANNEL_IN_MONO,
                                AudioFormat.ENCODING_PCM_16BIT, 48_000)
                        }.getOrNull()
                        val made = rec
                        if (made == null || made.state != AudioRecord.STATE_INITIALIZED) { made?.release(); rec = null; Thread.sleep(1000); continue }
                        made.startRecording()
                    }
                    val r = rec ?: continue
                    val n = r.read(mono, 0, mono.size)
                    if (n <= 0) continue
                    for (i in 0 until n / 2) {                  // mono -> both ears
                        stereo[i * 4] = mono[i * 2]; stereo[i * 4 + 1] = mono[i * 2 + 1]
                        stereo[i * 4 + 2] = mono[i * 2]; stereo[i * 4 + 3] = mono[i * 2 + 1]
                    }
                    val ts = nowUs() - n / 2 * 1_000_000L / 48_000
                    audioTarget()?.send(packet(T_AUDIO_MIC, 0, ts, stereo, 0, n * 2))
                }
            } finally {
                rec?.let { runCatching { it.stop() }; it.release() }
            }
        }
    }

    // ---------------- notification ----------------

    private fun notification(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Mirror", NotificationManager.IMPORTANCE_LOW))
        }
        val stop = PendingIntent.getService(this, 11, Intent(this, MirrorService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val mic = PendingIntent.getService(this, 12, Intent(this, MirrorService::class.java).setAction(ACTION_MIC),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP), PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_note)
            .setContentTitle("Den-Dev Live · Wi-Fi mirror")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, if (Mirror.micOn.value) "Mic off" else "Mic on", mic).build())
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }
}

// Mirror this Android phone to the laptop, exactly like the iPhone app's
// broadcast: the phone listens on port 2345 (picture + sound) and 2346 (sound
// only) with the same wire format (docs/PROTOCOL.md).
//   USB  : the laptop reaches the ports through "adb forward" (like usbmux on
//          the iPhone). Connections then come from 127.0.0.1 on the phone.
//   Wi-Fi: the laptop finds the phone on the network by itself.
// The capture keeps running when the USB link blinks; the laptop simply
// connects again (newest laptop connection wins, like the iPhone).
//
// Picture: screen -> H.264 hardware encoder (Annex-B), VIDEO_CONFIG before each keyframe.
//          USB 20 Mbps; Wi-Fi starts at 8 Mbps and adapts 12 -> 3 Mbps (same as iPhone).
// Sound:   game sound (AudioPlaybackCapture) -> AUDIO_PCM, your mic -> AUDIO_MIC,
//          48 kHz 16-bit stereo. The laptop mixes the mic into the stream only.
// Every 5 s an INFO text (same fields as the iPhone: heat, sound, mic, link, bitrate).

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
import android.net.wifi.WifiManager
import android.os.Build
import android.os.PowerManager
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
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

data class MirrorStatus(
    val running: Boolean = false,
    val connected: Boolean = false,       // the laptop is receiving
    val usb: Boolean = false,             // laptop connected through the USB cable (adb)
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

        // same numbers as the iPhone (SampleHandler / MirrorServer)
        const val USB_BITRATE = 20_000_000
        val WIFI_LEVELS = intArrayOf(12_000_000, 8_000_000, 5_000_000, 3_000_000)
        const val USB_BACKLOG = 1_000_000       // bytes waiting before video frames are dropped
        const val WIFI_BACKLOG = 300_000
        const val AUDIO_BACKLOG = 2_000_000

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
        val usb = socket.inetAddress?.isLoopbackAddress == true   // adb forward = USB cable
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
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    // bitrate + statistics (like the iPhone)
    @Volatile private var bitrate = USB_BITRATE
    @Volatile private var wifiLevel = 1
    private var cleanSeconds = 0
    private val dropped = AtomicInteger(0)          // this second
    private val droppedTotal = AtomicInteger(0)     // since the last INFO
    private val audioPieces = AtomicInteger(0)
    private val micPieces = AtomicInteger(0)
    @Volatile private var lastAudioAt = 0L
    @Volatile private var audioMaxGapMs = 0L
    @Volatile private var screenW = 0                // phone screen before scaling
    @Volatile private var screenH = 0

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
        holdLocks()
        thread(name = "tick", isDaemon = true) {
            var n = 0
            while (running) {
                Thread.sleep(1000)
                val hb = packet(T_HEARTBEAT, 0, nowUs(), ByteArray(0))
                mainClient?.send(hb); audioClient?.send(hb)
                if (mainClient?.alive == true) {
                    adaptBitrate()
                    if (n++ % 5 == 0) sendInfo()
                }
            }
        }
        if (AndroidSettings.canDrawOverlays(this)) overlay = Overlay(this, mirror = true).also { it.show() }
        refreshStatus()
    }

    private fun stopAll() {
        if (!running && projection == null) { stopSelf(); return }
        running = false
        overlay?.hide(); overlay = null
        runCatching { wakeLock?.release() }; wakeLock = null
        runCatching { wifiLock?.release() }; wifiLock = null
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
        mainClient?.close()                       // one laptop at a time, newest wins
        val c = Client(s, "main")
        mainClient = c
        c.send(packet(T_ORIENTATION, 0, nowUs(), byteArrayOf(1)))     // already upright
        c.send(packet(T_INFO, 0, nowUs(), identity().toByteArray()))
        wifiLevel = 1                              // Wi-Fi starts at 8 Mbps, climbs if clean
        cleanSeconds = 0
        applyBitrate()
        requestKey()
        main.post { refreshStatus() }
    }

    private fun identity() = "Android ${Build.MANUFACTURER} ${Build.MODEL} (Android ${Build.VERSION.RELEASE})"

    private fun refreshStatus() {
        val c = mainClient
        val connected = c?.alive == true
        val usb = connected && c?.usb == true
        Mirror.status.value = MirrorStatus(running = running, connected = connected, usb = usb, width = width, height = height)
        if (running) getSystemService(NotificationManager::class.java).notify(NOTE_ID, notification(
            when { !connected -> "Waiting for the laptop…"; usb -> "Mirroring to the laptop · USB"; else -> "Mirroring to the laptop · Wi-Fi" }))
    }

    /** Keep the CPU and Wi-Fi awake while mirroring (no sleep-related drops). */
    @SuppressLint("WakelockTimeout")
    private fun holdLocks() {
        runCatching {
            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DenDev:mirror").apply { setReferenceCounted(false); acquire() }
        }
        runCatching {
            val wm = applicationContext.getSystemService(WifiManager::class.java)
            @Suppress("DEPRECATION")
            val mode = if (Build.VERSION.SDK_INT >= 29) WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifiLock = wm.createWifiLock(mode, "DenDev:mirror").apply { setReferenceCounted(false); acquire() }
        }
    }

    // ---------------- bitrate + INFO (same rules as the iPhone) ----------------

    private fun targetBitrate(): Int {
        val c = mainClient
        return if (c == null || c.usb) USB_BITRATE else WIFI_LEVELS[wifiLevel]
    }

    private fun applyBitrate() {
        val b = targetBitrate()
        if (b == bitrate) return
        bitrate = b
        encoder?.let { e -> runCatching { e.setParameters(Bundle().apply { putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, b) }) } }
        Log.i(TAG, "bitrate -> ${b / 1_000_000} Mbps")
    }

    /** Once per second: frames dropped because the network is behind -> step down; clean -> step up. */
    private fun adaptBitrate() {
        val d = dropped.getAndSet(0)
        val c = mainClient ?: return
        if (!c.usb) {
            if (d >= 3) {
                cleanSeconds = 0
                if (wifiLevel < WIFI_LEVELS.size - 1) wifiLevel++
            } else {
                cleanSeconds++
                if (cleanSeconds >= 6 && wifiLevel > 0) { wifiLevel--; cleanSeconds = 0 }
            }
        }
        applyBitrate()
    }

    private fun thermal(): String {
        if (Build.VERSION.SDK_INT < 29) return "nominal"
        return when (getSystemService(PowerManager::class.java).currentThermalStatus) {
            PowerManager.THERMAL_STATUS_NONE, PowerManager.THERMAL_STATUS_LIGHT -> "nominal"
            PowerManager.THERMAL_STATUS_MODERATE -> "fair"
            PowerManager.THERMAL_STATUS_SEVERE -> "SERIOUS"
            else -> "CRITICAL"
        }
    }

    private fun sendInfo() {
        val c = mainClient ?: return
        val rt = Runtime.getRuntime()
        val memMb = (rt.totalMemory() - rt.freeMemory()) / 1_048_576.0
        val lowPower = getSystemService(PowerManager::class.java).isPowerSaveMode
        val pieces = audioPieces.getAndSet(0)
        val mics = micPieces.getAndSet(0)
        val gap = audioMaxGapMs; audioMaxGapMs = 0
        val text = "%s | source %dx%d -> encode %dx%d | orient 1 | mem %.1f MB | thermal %s | lowPower %s".format(
            identity(), screenW, screenH, width, height, memMb, thermal(), if (lowPower) "on" else "off") +
            " | audio in %d pieces, max gap %d ms, channel %s | mic %s | link %s | bitrate %d Mbps, dropped %d".format(
            pieces, gap, if (audioClient?.alive == true) "separate" else "main", if (mics > 0) "on" else "off",
            if (c.usb) "usb" else "wifi", bitrate / 1_000_000, droppedTotal.getAndSet(0))
        c.send(packet(T_INFO, 0, nowUs(), text.toByteArray()))
    }

    // ---------------- picture ----------------

    /** Screen size now (long side max 1920, multiples of 16). */
    @SuppressLint("NewApi")
    private fun measure() {
        val d = getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
        val m = DisplayMetrics()
        @Suppress("DEPRECATION") d.getRealMetrics(m)
        dpi = m.densityDpi
        screenW = m.widthPixels; screenH = m.heightPixels
        val scale = minOf(1f, 1920f / maxOf(m.widthPixels, m.heightPixels))
        width = ((m.widthPixels * scale).toInt() / 16) * 16
        height = ((m.heightPixels * scale).toInt() / 16) * 16
    }

    private fun makeEncoder(w: Int, h: Int): MediaCodec {
        val f = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h)
        f.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
        f.setInteger(MediaFormat.KEY_BIT_RATE, bitrate)
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
        if (c.queued.get() > (if (c.usb) USB_BACKLOG else WIFI_BACKLOG) && !key) {   // laptop/network behind:
            dropped.incrementAndGet(); droppedTotal.incrementAndGet()                // drop, restart at a keyframe
            requestKey(); return
        }
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
                    val now = System.nanoTime() / 1_000_000
                    if (lastAudioAt > 0) audioMaxGapMs = maxOf(audioMaxGapMs, now - lastAudioAt)
                    lastAudioAt = now
                    audioPieces.incrementAndGet()
                    val ts = nowUs() - n / 4 * 1_000_000L / 48_000
                    audioTarget()?.let { c -> if (c.queued.get() < AUDIO_BACKLOG) c.send(packet(T_AUDIO_PCM, 0, ts, buf, 0, n)) }
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
                    micPieces.incrementAndGet()
                    val ts = nowUs() - n / 2 * 1_000_000L / 48_000
                    audioTarget()?.let { c -> if (c.queued.get() < AUDIO_BACKLOG) c.send(packet(T_AUDIO_MIC, 0, ts, stereo, 0, n * 2)) }
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
            .setContentTitle("Den-Dev Live · Mirror")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, if (Mirror.micOn.value) "Mic off" else "Mic on", mic).build())
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }
}

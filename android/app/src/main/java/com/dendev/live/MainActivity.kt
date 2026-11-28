// Den-Dev Live for Android - same app as on the iPhone:
//   Home: USB / Wi-Fi / Go Live
//   USB     : mirror to the laptop with the cable (no app needed: USB debugging)
//   Wi-Fi   : mirror to the laptop over Wi-Fi (this app sends the screen)
//   Go Live : the phone streams by itself to Facebook / YouTube / TikTok / RTMP
//   Studio  : what viewers see (landscape / portrait, fit / fill, logo, banner, badge, BRB)
//   Comments: sign in to your live chat; comments float over the game while live

package com.dendev.live

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.projection.MediaProjectionManager
import android.net.ConnectivityManager
import android.net.Uri
import android.os.Bundle
import android.provider.Settings as AndroidSettings
import android.view.ViewGroup
import android.widget.FrameLayout
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import java.io.File
import java.net.Inet4Address

// ---------------- colors (same as the iPhone app) ----------------
object Theme {
    val bgTop = Color(0xFF0D0F1F)
    val bgBottom = Color(0xFF030308)
    val card = Color.White.copy(alpha = 0.06f)
    val stroke = Color.White.copy(alpha = 0.10f)
    val dim = Color.White.copy(alpha = 0.62f)
    val faint = Color.White.copy(alpha = 0.38f)
    val usb = listOf(Color(0xFF337AFF), Color(0xFF734DFF))
    val wifi = listOf(Color(0xFF00C7B3), Color(0xFF1A8CF2))
    val live = listOf(Color(0xFFFF385C), Color(0xFFFF7A33))
}

enum class Screen { HOME, USB, WIFI, LIVE, STUDIO, CHAT }

class MainActivity : ComponentActivity() {
    private lateinit var prefs: Prefs

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        if (!Live.running) Live.config.value = prefs.load()
        setContent { MaterialTheme(colorScheme = darkColorScheme()) { App(prefs) } }
    }
}

// ---------------- app ----------------

@Composable
fun App(prefs: Prefs) {
    var screen by remember { mutableStateOf(Screen.HOME) }
    BackHandler(enabled = screen != Screen.HOME) {
        screen = if (screen == Screen.STUDIO || screen == Screen.CHAT) Screen.LIVE else Screen.HOME
    }
    val cfg by Live.config.collectAsState()
    val update: (LiveConfig) -> Unit = { c -> Live.config.value = c; prefs.save(c) }

    Box(Modifier.fillMaxSize().background(Brush.verticalGradient(listOf(Theme.bgTop, Theme.bgBottom)))) {
        when (screen) {
            Screen.HOME -> HomeScreen { screen = it }
            Screen.USB -> UsbScreen { screen = Screen.HOME }
            Screen.WIFI -> WifiScreen { screen = Screen.HOME }
            Screen.LIVE -> GoLiveScreen(cfg, update, prefs, open = { screen = it }, back = { screen = Screen.HOME })
            Screen.STUDIO -> StudioScreen(cfg, update) { screen = Screen.LIVE }
            Screen.CHAT -> ChatScreen(cfg) { screen = Screen.LIVE }
        }
    }
}

// ---------------- shared pieces ----------------

@Composable
fun Page(title: String?, back: (() -> Unit)?, content: @Composable ColumnScope.() -> Unit) {
    Column(Modifier.fillMaxSize().systemBarsPadding().imePadding().verticalScroll(rememberScrollState()).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)) {
        if (back != null) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("‹ Back", color = Color.White, fontSize = 17.sp, modifier = Modifier.clickable { back() }.padding(end = 16.dp, top = 4.dp, bottom = 4.dp))
                if (title != null) Text(title, color = Color.White, fontWeight = FontWeight.Bold, fontSize = 17.sp)
            }
        }
        content()
    }
}

@Composable
fun Card(content: @Composable ColumnScope.() -> Unit) {
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(22.dp)).background(Theme.card)
        .border(1.dp, Theme.stroke, RoundedCornerShape(22.dp)).padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp), content = content)
}

@Composable
fun BigButton(title: String, subtitle: String?, colors: List<Color>, enabled: Boolean = true, onClick: () -> Unit) {
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(20.dp))
        .background(if (enabled) Brush.linearGradient(colors) else Brush.linearGradient(listOf(Color.DarkGray, Color.DarkGray)))
        .clickable(enabled = enabled) { onClick() }.padding(vertical = 18.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Text(title, color = Color.White, fontWeight = FontWeight.ExtraBold, fontSize = 20.sp)
        if (subtitle != null) Text(subtitle, color = Color.White.copy(alpha = 0.85f), fontSize = 13.sp)
    }
}

@Composable
fun Step(n: Int, text: String, color: Color) {
    Row(verticalAlignment = Alignment.Top) {
        Box(Modifier.size(26.dp).clip(CircleShape).background(color), contentAlignment = Alignment.Center) {
            Text("$n", color = Color.White, fontWeight = FontWeight.Bold, fontSize = 13.sp)
        }
        Spacer(Modifier.width(12.dp))
        Text(text, color = Color.White, fontSize = 15.sp)
    }
}

@Composable
fun Chip(label: String, selected: Boolean, color: Color, onClick: () -> Unit) {
    Box(Modifier.clip(RoundedCornerShape(14.dp)).background(if (selected) color else Theme.card)
        .border(1.dp, if (selected) color else Theme.stroke, RoundedCornerShape(14.dp))
        .clickable { onClick() }.padding(horizontal = 14.dp, vertical = 10.dp)) {
        Text(label, color = Color.White, fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal, fontSize = 14.sp)
    }
}

@Composable
fun Hint(text: String) = Text(text, color = Theme.faint, fontSize = 12.sp)

/** Ask for mic + notifications, then the screen capture; then start the service. */
@Composable
fun rememberCaptureStarter(service: Class<*>, onStarted: () -> Unit = {}): () -> Unit {
    val context = LocalContext.current
    val capture = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        if (r.resultCode == Activity.RESULT_OK && r.data != null) {
            val i = Intent(context, service).setAction(LiveService.ACTION_START)
                .putExtra(LiveService.EXTRA_CODE, r.resultCode).putExtra(LiveService.EXTRA_DATA, r.data)
            context.startForegroundService(i)
            onStarted()
        }
    }
    val perms = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        capture.launch(context.getSystemService(MediaProjectionManager::class.java).createScreenCaptureIntent())
    }
    return {
        val want = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (android.os.Build.VERSION.SDK_INT >= 33) want += Manifest.permission.POST_NOTIFICATIONS
        perms.launch(want.toTypedArray())
    }
}

@Composable
fun OverlayCard() {
    val context = LocalContext.current
    var allowed by remember { mutableStateOf(AndroidSettings.canDrawOverlays(context)) }
    LaunchedEffect(Unit) { while (true) { allowed = AndroidSettings.canDrawOverlays(context); kotlinx.coroutines.delay(1000) } }
    if (!allowed) Card {
        Text("Floating comments + buttons", color = Color.White, fontWeight = FontWeight.Bold)
        Text("Allow \"Display over other apps\" so the comments and the Mic / Stop buttons float over your game.", color = Theme.dim, fontSize = 14.sp)
        BigButton("Allow", null, Theme.live) {
            context.startActivity(Intent(AndroidSettings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:${context.packageName}")))
        }
    }
}

fun formatTime(s: Int) = if (s >= 3600) "%d:%02d:%02d".format(s / 3600, s / 60 % 60, s % 60) else "%d:%02d".format(s / 60, s % 60)

// ---------------- home ----------------

@Composable
fun HomeScreen(open: (Screen) -> Unit) {
    val live by Live.status.collectAsState()
    val mirror by Mirror.status.collectAsState()
    Page(null, null) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 12.dp, bottom = 6.dp)) {
            Box(Modifier.size(56.dp).clip(RoundedCornerShape(16.dp)).background(Brush.linearGradient(Theme.live)), contentAlignment = Alignment.Center) {
                Text("📱", fontSize = 26.sp)
            }
            Spacer(Modifier.width(14.dp))
            Column {
                Text("Den-Dev Live", color = Color.White, fontWeight = FontWeight.ExtraBold, fontSize = 32.sp)
                Text("Mirror · Stream · Go Live", color = Theme.dim, fontSize = 15.sp)
            }
        }
        if (Live.running) LiveBar(live) { open(Screen.LIVE) }
        if (mirror.running) MirrorBar(mirror) { open(Screen.WIFI) }
        ModeCard("USB", "Cable to the laptop. Lowest delay.", "🔌", "BEST", Theme.usb) { open(Screen.USB) }
        ModeCard("Wi-Fi", "No cable. The laptop finds this phone.", "📶", "EASY", Theme.wifi) { open(Screen.WIFI) }
        ModeCard("Go Live", "Stream from the phone to Facebook, YouTube, TikTok.", "🔴", "NO PC", Theme.live) { open(Screen.LIVE) }
        Hint("Tip: on the laptop, start Den-Dev Live (Desktop shortcut). The phone appears by itself.")
    }
}

@Composable
fun ModeCard(title: String, subtitle: String, symbol: String, badge: String, colors: List<Color>, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(26.dp)).background(Theme.card)
        .border(1.dp, Theme.stroke, RoundedCornerShape(26.dp)).clickable { onClick() }.padding(18.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(64.dp).clip(RoundedCornerShape(18.dp)).background(Brush.linearGradient(colors)), contentAlignment = Alignment.Center) {
            Text(symbol, fontSize = 28.sp)
        }
        Spacer(Modifier.width(16.dp))
        Column(Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(title, color = Color.White, fontWeight = FontWeight.ExtraBold, fontSize = 22.sp)
                Spacer(Modifier.width(8.dp))
                Text(badge, color = colors[0], fontWeight = FontWeight.Bold, fontSize = 11.sp,
                    modifier = Modifier.clip(RoundedCornerShape(8.dp)).background(colors[0].copy(alpha = 0.18f)).padding(horizontal = 7.dp, vertical = 3.dp))
            }
            Text(subtitle, color = Theme.dim, fontSize = 14.sp)
        }
        Text("›", color = Theme.faint, fontSize = 28.sp)
    }
}

@Composable
fun LiveBar(s: LiveStatus, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Color.Red.copy(alpha = 0.22f)).clickable { onClick() }.padding(14.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(10.dp).clip(CircleShape).background(Color.Red))
        Spacer(Modifier.width(10.dp))
        Text(if (s.state == "live") "LIVE" else s.state.replaceFirstChar { it.uppercase() }, color = Color.White, fontWeight = FontWeight.ExtraBold)
        Spacer(Modifier.width(10.dp))
        Text(formatTime(s.seconds), color = Color.White)
        Spacer(Modifier.weight(1f))
        Text("${s.kbps} kbps", color = Theme.dim)
    }
}

@Composable
fun MirrorBar(s: MirrorStatus, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp)).background(Theme.wifi[0].copy(alpha = 0.22f)).clickable { onClick() }.padding(14.dp)) {
        Text(if (s.connected) "Mirroring to the laptop" else "Wi-Fi mirror on · waiting for the laptop", color = Color.White, fontWeight = FontWeight.Bold)
    }
}

// ---------------- USB ----------------

@Composable
fun UsbScreen(back: () -> Unit) {
    val context = LocalContext.current
    val c = Theme.usb[0]
    Page("USB", back) {
        Text("Mirror with the cable", color = Color.White, fontWeight = FontWeight.ExtraBold, fontSize = 28.sp)
        Text("Lowest delay, sharpest picture, steady sound. On Android the laptop does the work: nothing to start here.", color = Theme.dim)
        Card {
            Text("One time: turn on USB debugging", color = Color.White, fontWeight = FontWeight.Bold)
            Step(1, "Settings → About phone → tap \"Build number\" 7 times.", c)
            Step(2, "Settings → Developer options → USB debugging ON.", c)
            BigButton("Open Developer options", null, Theme.usb) {
                runCatching { context.startActivity(Intent(AndroidSettings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS)) }
                    .onFailure { context.startActivity(Intent(AndroidSettings.ACTION_DEVICE_INFO_SETTINGS)) }
            }
        }
        Card {
            Text("Every time", color = Color.White, fontWeight = FontWeight.Bold)
            Step(1, "Start Den-Dev Live on the laptop.", c)
            Step(2, "Plug in the phone → \"Allow USB debugging?\" → tick Always allow → Allow.", c)
            Step(3, "Your screen + game sound appear on the laptop. The mouse works as your finger.", c)
        }
        Hint("Sound needs Android 11+. On the laptop: right-click → \"Android: switch to Wi-Fi\" to go wireless with adb.")
    }
}

// ---------------- Wi-Fi ----------------

fun wifiAddress(context: Context): String? {
    val cm = context.getSystemService(ConnectivityManager::class.java)
    val lp = cm.getLinkProperties(cm.activeNetwork) ?: return null
    return lp.linkAddresses.map { it.address }.firstOrNull { it is Inet4Address && !it.isLoopbackAddress }?.hostAddress
}

@Composable
fun WifiScreen(back: () -> Unit) {
    val context = LocalContext.current
    val status by Mirror.status.collectAsState()
    val mic by Mirror.micOn.collectAsState()
    var ip by remember { mutableStateOf(wifiAddress(context)) }
    LaunchedEffect(Unit) { while (true) { ip = wifiAddress(context); kotlinx.coroutines.delay(2000) } }
    val start = rememberCaptureStarter(MirrorService::class.java)
    val c = Theme.wifi[0]
    Page("Wi-Fi", back) {
        Text("Mirror over Wi-Fi", color = Color.White, fontWeight = FontWeight.ExtraBold, fontSize = 28.sp)
        Text("No cable. The laptop finds this phone by itself. A little more delay than USB.", color = Theme.dim)
        Card {
            Text("This phone's Wi-Fi address", color = Theme.dim, fontSize = 13.sp)
            Text(ip ?: "Not on Wi-Fi", color = Color.White, fontWeight = FontWeight.ExtraBold, fontSize = 30.sp)
        }
        OverlayCard()
        if (status.running) {
            Card {
                Text(if (status.connected) "✅ Mirroring to the laptop" else "⏳ Waiting for the laptop…", color = Color.White, fontWeight = FontWeight.Bold)
                if (status.width > 0) Hint("${status.width} × ${status.height}")
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Your mic to the stream", color = Color.White, modifier = Modifier.weight(1f))
                    Switch(checked = mic, onCheckedChange = { Mirror.micOn.value = it })
                }
            }
            BigButton("Stop mirroring", null, listOf(Color(0xFF555566), Color(0xFF333344))) {
                context.startService(Intent(context, MirrorService::class.java).setAction(MirrorService.ACTION_STOP))
            }
        } else {
            Card {
                Text("How to start", color = Color.White, fontWeight = FontWeight.Bold)
                Step(1, "Phone and laptop on the same Wi-Fi (5 GHz is best), or the laptop on this phone's hotspot.", c)
                Step(2, "Open Den-Dev Live on the laptop.", c)
                Step(3, "Tap Start mirroring → Start now.", c)
                Step(4, "Open your game. Stop from the notification or the floating bubble.", c)
            }
            BigButton("Start mirroring", "Over Wi-Fi", Theme.wifi) { start() }
        }
        Hint("On the laptop: first toolbar button = Auto or Wi-Fi. W searches now.")
        Hint("Game sound needs Android 10+, and the game must allow sound recording.")
    }
}

// ---------------- Go Live ----------------

@Composable
fun GoLiveScreen(cfg: LiveConfig, update: (LiveConfig) -> Unit, prefs: Prefs, open: (Screen) -> Unit, back: () -> Unit) {
    val context = LocalContext.current
    val status by Live.status.collectAsState()
    val start = rememberCaptureStarter(LiveService::class.java)
    var showKey by remember { mutableStateOf(false) }
    val running = Live.running
    Page("Go Live", back) {
        if (running || status.state == "error") Dashboard(cfg, status, update)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Box(Modifier.weight(1f)) { BigButton("🎨 Studio", "What viewers see", listOf(Color(0xFF8C32DC), Color(0xFF3C46DC))) { open(Screen.STUDIO) } }
            Box(Modifier.weight(1f)) { BigButton("💬 Comments", "Sign in / read", listOf(Color(0xFF1A8CF2), Color(0xFF00C7B3))) { open(Screen.CHAT) } }
        }
        OverlayCard()
        Card {
            Text("Platform", color = Color.White, fontWeight = FontWeight.Bold)
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Platform.entries.forEach { p -> Chip(p.title, cfg.platform == p, Theme.live[0]) { update(prefs.forPlatform(cfg, p)) } }
            }
            Hint(cfg.platform.keyHelp)
            if (cfg.platform.keyPage.isNotEmpty()) Text("Open ${cfg.platform.title} to get the key ›", color = Theme.wifi[0], fontSize = 14.sp,
                modifier = Modifier.clickable { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(cfg.platform.keyPage))) })
            OutlinedTextField(cfg.server, { update(cfg.copy(server = it)) }, label = { Text("Server URL (rtmp:// or rtmps://)") },
                singleLine = true, modifier = Modifier.fillMaxWidth(), enabled = !running)
            OutlinedTextField(cfg.key, { update(cfg.copy(key = it)) }, label = { Text("Stream key (secret)") }, singleLine = true,
                visualTransformation = if (showKey) VisualTransformation.None else PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                trailingIcon = { Text(if (showKey) "Hide" else "Show", color = Theme.dim, modifier = Modifier.clickable { showKey = !showKey }.padding(8.dp)) },
                modifier = Modifier.fillMaxWidth(), enabled = !running)
            Hint("The key stays on this phone (encrypted). Never share it or show it on stream.")
        }
        Card {
            Text("Quality", color = Color.White, fontWeight = FontWeight.Bold)
            Quality.all.forEach { q ->
                Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(if (cfg.quality.id == q.id) Theme.live[0].copy(alpha = 0.25f) else Color.Transparent)
                    .clickable(enabled = !running) { update(cfg.copy(quality = q)) }.padding(10.dp)) {
                    Text(q.title, color = Color.White, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                    Text("${q.detail} · ${q.bitrate / 1_000_000.0} Mbps", color = Theme.dim, fontSize = 13.sp)
                }
            }
        }
        Card {
            Text("Sound", color = Color.White, fontWeight = FontWeight.Bold)
            Text("Game sound", color = Theme.dim, fontSize = 13.sp)
            Slider(cfg.gameVolume, { update(cfg.copy(gameVolume = it)) }, valueRange = 0f..2f)
            Text("Your voice (mic)", color = Theme.dim, fontSize = 13.sp)
            Slider(cfg.micVolume, { update(cfg.copy(micVolume = it)) }, valueRange = 0f..2f)
            Hint("Game + your voice are mixed on the phone; the game sound is not turned down. " +
                 "Game voice chat (teammates) cannot be recorded on Android, and while the game's own mic is on, Android may give the mic to the game.")
        }
        if (!running) {
            val ready = cfg.publishUrl != null
            BigButton("GO LIVE", if (ready) "${cfg.platform.title} · ${cfg.quality.detail}" else "Add the server + stream key first", Theme.live, enabled = ready) {
                Chat.show(context, cfg.chatUrl)
                start()
            }
            Hint("First test with privacy \"Only me\". Android asks to record the screen: choose \"Entire screen\" → Start.")
        }
    }
}

@Composable
fun Dashboard(cfg: LiveConfig, s: LiveStatus, update: (LiveConfig) -> Unit) {
    val context = LocalContext.current
    Card {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(12.dp).clip(CircleShape).background(if (s.state == "live") Color.Red else Color(0xFFFFA000)))
            Spacer(Modifier.width(10.dp))
            Text(when (s.state) { "live" -> "LIVE"; "connecting" -> "Connecting…"; "reconnecting" -> "Reconnecting…"; "error" -> "Stopped"; else -> s.state },
                color = Color.White, fontWeight = FontWeight.ExtraBold, fontSize = 22.sp)
            Spacer(Modifier.weight(1f))
            Text(formatTime(s.seconds), color = Color.White, fontSize = 22.sp)
        }
        if (Live.running) {
            Text("${s.kbps} kbps · reconnects ${s.reconnects}", color = Theme.dim, fontSize = 13.sp)
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Chip(if (cfg.micMuted) "Mic off" else "Mic on", !cfg.micMuted, Theme.live[0]) { update(cfg.copy(micMuted = !cfg.micMuted)) }
                Chip(if (cfg.privacy) "Screen hidden" else "Screen shown", !cfg.privacy, Theme.live[0]) { update(cfg.copy(privacy = !cfg.privacy)) }
            }
            BigButton("Stop live", null, listOf(Color(0xFF555566), Color(0xFF333344))) { LiveService.send(context, LiveService.ACTION_STOP) }
        }
        if (s.message.isNotEmpty()) Text(s.message, color = Color(0xFFFFB0B0), fontSize = 13.sp)
    }
}

// ---------------- Studio ----------------

@Composable
fun StudioScreen(cfg: LiveConfig, update: (LiveConfig) -> Unit, back: () -> Unit) {
    val context = LocalContext.current
    val pick = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri != null) saveLogo(context, uri)?.let { update(cfg.copy(logoPath = it)) }
    }
    val w = if (cfg.portrait) 360 else 640
    val h = if (cfg.portrait) 640 else 360
    val preview = remember(cfg) { StreamLook.draw(w, h, cfg, 754) }
    val running = Live.running
    Page("Studio", back) {
        // preview: fake game picture + the overlay viewers see
        Box(Modifier.fillMaxWidth().aspectRatio(w.toFloat() / h).clip(RoundedCornerShape(16.dp))
            .background(Brush.linearGradient(listOf(Color(0xFF1B5E20), Color(0xFF263238), Color(0xFF4A148C))))) {
            Text("your game", color = Color.White.copy(alpha = 0.25f), fontSize = 28.sp, fontWeight = FontWeight.Bold, modifier = Modifier.align(Alignment.Center))
            Image(preview.asImageBitmap(), null, Modifier.fillMaxSize(), contentScale = ContentScale.FillBounds)
        }
        Card {
            Text("Layout", color = Color.White, fontWeight = FontWeight.Bold)
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Chip("Landscape", !cfg.portrait, Theme.live[0]) { if (!running) update(cfg.copy(portrait = false)) }
                Chip("Portrait", cfg.portrait, Theme.live[0]) { if (!running) update(cfg.copy(portrait = true)) }
            }
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Chip("Fit", !cfg.fill, Theme.live[0]) { if (!running) update(cfg.copy(fill = false)) }
                Chip("Fill", cfg.fill, Theme.live[0]) { if (!running) update(cfg.copy(fill = true)) }
            }
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Chip("Be right back", cfg.privacy, Theme.live[0]) { update(cfg.copy(privacy = !cfg.privacy)) }
            }
            Hint(if (running) "Layout changes start with the next live. Logo, text and badge change right away." else "Landscape for most games, Portrait for TikTok-style.")
        }
        Card {
            Text("Logo", color = Color.White, fontWeight = FontWeight.Bold)
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Chip(if (cfg.logoPath.isEmpty()) "Pick from Photos" else "Change", false, Theme.live[0]) {
                    pick.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
                }
                if (cfg.logoPath.isNotEmpty()) Chip("Remove", false, Theme.live[0]) { update(cfg.copy(logoPath = "")) }
            }
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                listOf("↖", "↗", "↙", "↘").forEachIndexed { i, a -> Chip(a, cfg.logoCorner == i, Theme.live[0]) { update(cfg.copy(logoCorner = i)) } }
            }
            Text("Size", color = Theme.dim, fontSize = 13.sp)
            Slider(cfg.logoScale, { update(cfg.copy(logoScale = it)) }, valueRange = 0.08f..0.32f)
        }
        Card {
            Text("Text", color = Color.White, fontWeight = FontWeight.Bold)
            OutlinedTextField(cfg.banner, { update(cfg.copy(banner = it.take(60))) }, label = { Text("Banner (e.g. Rank push tonight!)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("Red", "Blue", "Black", "Purple").forEachIndexed { i, n ->
                    Chip(n, cfg.bannerColor == i, Color(StreamLook.bannerColors[i])) { update(cfg.copy(bannerColor = i)) }
                }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("LIVE badge with timer", color = Color.White, modifier = Modifier.weight(1f))
                Switch(cfg.badge, { update(cfg.copy(badge = it)) })
            }
            OutlinedTextField(cfg.brbText, { update(cfg.copy(brbText = it.take(40))) }, label = { Text("\"Be right back\" text") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        }
    }
}

/** Picture from Photos -> small PNG (max 400 px) in the app's files. */
fun saveLogo(context: Context, uri: Uri): String? = runCatching {
    val src = context.contentResolver.openInputStream(uri).use { BitmapFactory.decodeStream(it) } ?: return null
    val s = minOf(1f, 400f / maxOf(src.width, src.height))
    val small = Bitmap.createScaledBitmap(src, maxOf(1, (src.width * s).toInt()), maxOf(1, (src.height * s).toInt()), true)
    val f = File(context.filesDir, "stream-logo-${System.currentTimeMillis()}.png")
    context.filesDir.listFiles { x -> x.name.startsWith("stream-logo-") }?.forEach { it.delete() }
    f.outputStream().use { small.compress(Bitmap.CompressFormat.PNG, 100, it) }
    f.absolutePath
}.getOrNull()

// ---------------- Comments ----------------

@Composable
fun ChatScreen(cfg: LiveConfig, back: () -> Unit) {
    val context = LocalContext.current
    var link by remember { mutableStateOf(cfg.chatLink) }
    Page("Comments", back) {
        Card {
            Text("Your live chat", color = Color.White, fontWeight = FontWeight.Bold)
            OutlinedTextField(link, { link = it }, label = { Text("Live video / chat link (empty = ${cfg.platform.title} page)") },
                singleLine = true, modifier = Modifier.fillMaxWidth())
            Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Chip("Open", false, Theme.wifi[0]) {
                    val c = Live.config.value.copy(chatLink = link)
                    Live.config.value = c; Prefs(context).save(c)
                    Chat.show(context, c.chatUrl, force = true)
                }
                Chip("Reload", false, Theme.wifi[0]) { Chat.reload() }
            }
            Text("See-through comments", color = Theme.dim, fontSize = 13.sp)
            Slider(cfg.chatOpacity, { val c = Live.config.value.copy(chatOpacity = it); Live.config.value = c; Prefs(context).save(c) }, valueRange = 0.3f..0.8f)
            Hint("Sign in below once. While live, tap the floating bubble to see comments over the game; tap the game to hide them.")
        }
        LaunchedEffect(Unit) { Chat.show(context, Live.config.value.chatUrl) }
        if (Live.running || Mirror.status.value.running) {
            Hint("The chat page is in the floating bubble while live: use \"Full chat\" there.")
        } else {
            AndroidView(factory = { ctx ->
                FrameLayout(ctx).apply {
                    Chat.detach()
                    addView(Chat.view(ctx), FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                }
            }, onRelease = { Chat.detach() }, modifier = Modifier.fillMaxWidth().height(560.dp).clip(RoundedCornerShape(16.dp)))
        }
    }
}

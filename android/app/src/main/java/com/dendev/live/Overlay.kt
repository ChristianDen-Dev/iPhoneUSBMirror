// Floating controls over the game (needs "Display over other apps"):
//  - a round bubble you can drag to any side (it snaps to the nearest edge);
//  - tap it -> see-through comments float next to it + live buttons
//    (Mic, Screen, Full chat, Stop); tap the game again -> they hide;
//  - "Full chat" opens the chat page itself to sign in / reply.
// Comments window lets touches pass through to the game.

package com.dendev.live

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.util.TypedValue
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import kotlin.math.abs

class Overlay(private val context: Context, private val mirror: Boolean = false) {
    private val wm = context.getSystemService(WindowManager::class.java)
    private val main = Handler(Looper.getMainLooper())
    private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), context.resources.displayMetrics).toInt()

    private lateinit var bubbleRoot: LinearLayout
    private lateinit var controls: LinearLayout
    private lateinit var feedRoot: LinearLayout
    private lateinit var chatRoot: LinearLayout
    private lateinit var bubbleLp: WindowManager.LayoutParams
    private lateinit var feedLp: WindowManager.LayoutParams
    private lateinit var chatLp: WindowManager.LayoutParams
    private var open = false
    private var fullChat = false
    private var shown = false
    private var lastCount = -1
    private var onLeft = false
    private val micBtn by lazy { button("") { toggleMic() } }
    private val screenBtn by lazy { button("") { toggleScreen() } }

    private val type = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY

    // ---------------- build ----------------

    @SuppressLint("ClickableViewAccessibility")
    fun show() {
        if (shown) return
        shown = true
        val cfg = Live.config.value

        // bubble + buttons (touchable, watches touches outside = "tap the game to hide")
        bubbleRoot = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER_HORIZONTAL }
        val bubble = TextView(context).apply {
            text = "💬"                                  // speech bubble
            textSize = 22f
            gravity = Gravity.CENTER
            background = GradientDrawable(GradientDrawable.Orientation.TL_BR,
                intArrayOf(Color.rgb(255, 56, 92), Color.rgb(255, 122, 51))).apply { shape = GradientDrawable.OVAL }
            alpha = 0.85f
            layoutParams = LinearLayout.LayoutParams(dp(52), dp(52))
        }
        controls = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
            setPadding(0, dp(6), 0, 0)
        }
        controls.addView(micBtn)
        if (!mirror) controls.addView(screenBtn)
        controls.addView(button("Full chat") { openFullChat() })
        controls.addView(button("Stop") {
            if (mirror) context.startService(android.content.Intent(context, MirrorService::class.java).setAction(MirrorService.ACTION_STOP))
            else LiveService.send(context, LiveService.ACTION_STOP)
        })
        bubbleRoot.addView(bubble)
        bubbleRoot.addView(controls)
        refreshButtons()

        bubbleLp = WindowManager.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT, type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT).apply {
            gravity = Gravity.TOP or Gravity.START
            val dm = context.resources.displayMetrics
            x = dm.widthPixels - dp(60); y = dm.heightPixels / 3
        }

        // drag anywhere, snap to the nearest side; a short tap opens / closes
        var downX = 0f; var downY = 0f; var startX = 0; var startY = 0; var moved = false
        bubble.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> { downX = e.rawX; downY = e.rawY; startX = bubbleLp.x; startY = bubbleLp.y; moved = false }
                MotionEvent.ACTION_MOVE -> {
                    val dx = e.rawX - downX; val dy = e.rawY - downY
                    if (abs(dx) > dp(8) || abs(dy) > dp(8)) moved = true
                    if (moved) { bubbleLp.x = startX + dx.toInt(); bubbleLp.y = startY + dy.toInt(); wm.updateViewLayout(bubbleRoot, bubbleLp) }
                }
                MotionEvent.ACTION_UP -> if (moved) snap() else toggle()
            }
            true
        }
        bubbleRoot.setOnTouchListener { _, e ->
            if (e.actionMasked == MotionEvent.ACTION_OUTSIDE && open && !fullChat) setOpen(false)   // back to the game
            false
        }

        // floating comments (touches pass through to the game)
        feedRoot = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.BOTTOM
            visibility = View.GONE
        }
        feedLp = WindowManager.LayoutParams(dp(300), ViewGroup.LayoutParams.WRAP_CONTENT, type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT).apply {
            gravity = Gravity.TOP or Gravity.START
            alpha = cfg.chatOpacity.coerceIn(0.3f, 0.8f)     // Android lets touches through only up to 80 %
        }

        // the chat page: tiny + invisible while reading, big when "Full chat"
        chatRoot = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL; setBackgroundColor(Color.argb(235, 12, 14, 30)) }
        chatLp = WindowManager.LayoutParams(1, 1, type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
            PixelFormat.TRANSLUCENT).apply { gravity = Gravity.TOP or Gravity.START; alpha = 0f }

        Chat.show(context, cfg.chatUrl)
        Chat.detach()?.let { chatRoot.addView(it, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)) }

        wm.addView(chatRoot, chatLp)
        wm.addView(feedRoot, feedLp)
        wm.addView(bubbleRoot, bubbleLp)
        snap()
        main.post(poll)
    }

    fun hide() {
        if (!shown) return
        shown = false
        main.removeCallbacks(poll)
        Chat.detach()
        for (v in listOf(bubbleRoot, feedRoot, chatRoot)) runCatching { wm.removeView(v) }
    }

    private fun button(label: String, onTap: () -> Unit) = TextView(context).apply {
        text = label
        setTextColor(Color.WHITE)
        textSize = 13f
        typeface = Typeface.DEFAULT_BOLD
        gravity = Gravity.CENTER
        setPadding(dp(12), dp(8), dp(12), dp(8))
        background = GradientDrawable().apply { cornerRadius = dp(18).toFloat(); setColor(Color.argb(190, 20, 20, 30)) }
        layoutParams = LinearLayout.LayoutParams(dp(104), ViewGroup.LayoutParams.WRAP_CONTENT).apply { topMargin = dp(6) }
        setOnClickListener { onTap(); refreshButtons() }
    }

    // ---------------- actions ----------------

    private fun toggle() = setOpen(!open)

    private fun setOpen(value: Boolean) {
        open = value
        controls.visibility = if (open) View.VISIBLE else View.GONE
        feedRoot.visibility = if (open) View.VISIBLE else View.GONE
        if (open) { lastCount = -1; renderFeed() }
        placeBubble()
    }

    /** Keep the bubble against its edge (its width changes when the buttons show). */
    private fun placeBubble() {
        val dm = context.resources.displayMetrics
        bubbleRoot.measure(View.MeasureSpec.UNSPECIFIED, View.MeasureSpec.UNSPECIFIED)
        bubbleLp.x = if (onLeft) 0 else dm.widthPixels - bubbleRoot.measuredWidth
        wm.updateViewLayout(bubbleRoot, bubbleLp)
        placeFeed()
    }

    private fun snap() {
        val dm = context.resources.displayMetrics
        onLeft = bubbleLp.x + dp(26) < dm.widthPixels / 2
        bubbleLp.y = bubbleLp.y.coerceIn(0, dm.heightPixels - dp(80))
        bubbleRoot.gravity = if (onLeft) Gravity.START else Gravity.END
        placeBubble()
    }

    /** Comments next to the bubble, on the open side of the screen. */
    private fun placeFeed() {
        if (!shown) return
        val dm = context.resources.displayMetrics
        feedLp.width = minOf(dp(320), (dm.widthPixels * 0.55f).toInt())
        feedLp.x = if (onLeft) dp(112) else dm.widthPixels - dp(112) - feedLp.width
        feedLp.y = (bubbleLp.y - dp(40)).coerceAtLeast(0)
        feedRoot.gravity = if (onLeft) Gravity.START else Gravity.END
        runCatching { wm.updateViewLayout(feedRoot, feedLp) }
    }

    private fun toggleMic() {
        if (mirror) Mirror.micOn.value = !Mirror.micOn.value
        else LiveService.send(context, LiveService.ACTION_MIC)
        main.postDelayed({ refreshButtons() }, 150)
    }

    private fun toggleScreen() {
        LiveService.send(context, LiveService.ACTION_SCREEN)
        main.postDelayed({ refreshButtons() }, 150)
    }

    private fun refreshButtons() {
        if (mirror) {
            micBtn.text = if (Mirror.micOn.value) "Mic: ON" else "Mic: OFF"
        } else {
            val c = Live.config.value
            micBtn.text = if (c.micMuted) "Mic: OFF" else "Mic: ON"
            screenBtn.text = if (c.privacy) "Screen: hidden" else "Screen: shown"
        }
    }

    private fun openFullChat() {
        val dm = context.resources.displayMetrics
        fullChat = true
        setOpen(false)
        if (chatRoot.childCount < 2) {
            val bar = TextView(context).apply {
                text = "✕  Close chat"
                setTextColor(Color.WHITE); textSize = 15f; typeface = Typeface.DEFAULT_BOLD
                setPadding(dp(16), dp(12), dp(16), dp(12))
                setOnClickListener { closeFullChat() }
            }
            chatRoot.addView(bar, 0)
        }
        chatRoot.getChildAt(0).visibility = View.VISIBLE
        chatLp.width = (dm.widthPixels * 0.92f).toInt()
        chatLp.height = (dm.heightPixels * 0.75f).toInt()
        chatLp.x = (dm.widthPixels - chatLp.width) / 2
        chatLp.y = (dm.heightPixels - chatLp.height) / 2
        chatLp.alpha = 1f
        chatLp.flags = WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL          // can type (keyboard)
        wm.updateViewLayout(chatRoot, chatLp)
    }

    private fun closeFullChat() {
        fullChat = false
        if (chatRoot.childCount > 1) chatRoot.getChildAt(0).visibility = View.GONE
        chatLp.width = 1; chatLp.height = 1; chatLp.alpha = 0f
        chatLp.flags = WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
        wm.updateViewLayout(chatRoot, chatLp)
    }

    // ---------------- comments ----------------

    private val poll = object : Runnable {
        override fun run() {
            if (!shown) return
            if (open) renderFeed()
            refreshButtons()
            val a = Live.config.value.chatOpacity.coerceIn(0.3f, 0.8f)
            if (feedLp.alpha != a) { feedLp.alpha = a; runCatching { wm.updateViewLayout(feedRoot, feedLp) } }
            main.postDelayed(this, 700)
        }
    }

    private fun renderFeed() {
        val list = Chat.messages.value
        if (list.size == lastCount && lastCount >= 0) return
        lastCount = list.size
        feedRoot.removeAllViews()
        val show = list.takeLast(8)
        if (show.isEmpty()) {
            feedRoot.addView(line("", "No comments yet. Tap Full chat once to sign in to your live."))
            return
        }
        show.forEach { m -> feedRoot.addView(line(m.author, m.text)) }
    }

    private fun line(author: String, text: String) = TextView(context).apply {
        val s = android.text.SpannableStringBuilder()
        if (author.isNotBlank()) {
            s.append(author, android.text.style.ForegroundColorSpan(Color.rgb(255, 214, 102)), 0)
            s.setSpan(android.text.style.StyleSpan(Typeface.BOLD), 0, author.length, 0)
            s.append("  ")
        }
        s.append(text)
        this.text = s
        setTextColor(Color.WHITE)
        textSize = 14f
        setShadowLayer(6f, 0f, 1f, Color.BLACK)                // readable on any game, no box
        setPadding(dp(4), dp(3), dp(4), dp(3))
        maxLines = 3
    }
}

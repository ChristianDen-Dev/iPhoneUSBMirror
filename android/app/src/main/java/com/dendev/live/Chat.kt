// Reads live comments from the chat page (YouTube live chat, TikTok LIVE web,
// Facebook comments) in one shared, signed-in web view - same reader as the
// iPhone app. Sign in once in "Full chat"; the comments then float over the game.

package com.dendev.live

import android.annotation.SuppressLint
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import kotlinx.coroutines.flow.MutableStateFlow
import org.json.JSONArray

data class ChatMessage(val id: Int, val author: String, val text: String)

object Chat {
    val messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    private var web: WebView? = null
    private var loaded = ""
    private val seen = HashSet<String>()
    private var nextId = 0
    private val main = Handler(Looper.getMainLooper())

    private const val DESKTOP_AGENT =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

    /** [author, text] pairs from the page (newest last). Same as the iPhone app. */
    private const val READ_SCRIPT = """
    (function(){
      var out=[];
      function add(a,t){ t=(t||'').trim(); if(t) out.push([(a||'').trim(), t.slice(0,200)]); }
      document.querySelectorAll('yt-live-chat-text-message-renderer').forEach(function(e){
        var a=e.querySelector('#author-name'), m=e.querySelector('#message'); add(a&&a.innerText, m&&m.innerText); });
      if(!out.length) document.querySelectorAll('[data-e2e="chat-message"]').forEach(function(e){
        var a=e.querySelector('[data-e2e="message-owner-name"]'); var t=e.innerText||'';
        if(a){ t=t.replace(a.innerText,''); } add(a&&a.innerText, t.replace(/^\s*:\s*/,'')); });
      if(!out.length) document.querySelectorAll('[role="article"][aria-label]').forEach(function(e){
        var l=e.getAttribute('aria-label')||''; if(!/comment/i.test(l)) return;
        var name=l.replace(/^(Comment|Reply) by /i,'').replace(/\s+\d+\s*\w+\s*ago${'$'}/i,'').replace(/ (?:a|an|one) \w+ ago${'$'}/i,'');
        var parts=[]; e.querySelectorAll('div[dir="auto"]').forEach(function(d){ if(d.innerText) parts.push(d.innerText); });
        add(name, parts.join(' ')); });
      return JSON.stringify(out.slice(-40));
    })()
    """

    @SuppressLint("SetJavaScriptEnabled")
    fun view(context: Context): WebView {
        web?.let { return it }
        val w = WebView(context.applicationContext)
        w.settings.javaScriptEnabled = true
        w.settings.domStorageEnabled = true
        w.settings.mediaPlaybackRequiresUserGesture = true      // never plays your own live (echo)
        w.settings.cacheMode = WebSettings.LOAD_DEFAULT
        CookieManager.getInstance().setAcceptCookie(true)        // stays signed in
        CookieManager.getInstance().setAcceptThirdPartyCookies(w, true)
        w.webViewClient = WebViewClient()
        web = w
        main.post(reader)
        return w
    }

    /** Open the chat page for this live (only reloads when the page changes). */
    fun show(context: Context, url: String, force: Boolean = false) {
        if (url.isBlank()) return
        val w = view(context)
        if (!force && url == loaded) return
        loaded = url
        val desktop = url.contains("facebook.com/live/producer") || url.contains("studio.youtube")
        w.settings.userAgentString = if (desktop) DESKTOP_AGENT else WebSettings.getDefaultUserAgent(context)
        w.loadUrl(url)
    }

    fun reload() { web?.reload() }

    /** Take the web view out of its current window (it can only be in one at a time). */
    fun detach(): WebView? {
        val w = web ?: return null
        (w.parent as? ViewGroup)?.removeView(w)
        return w
    }

    private val reader = object : Runnable {
        override fun run() {
            val w = web
            if (w != null && loaded.isNotEmpty()) {
                w.evaluateJavascript(READ_SCRIPT) { result -> parse(result) }
            }
            main.postDelayed(this, 1000)
        }
    }

    private fun parse(result: String?) {
        if (result == null || result == "null") return
        // the page returns a string, which Android hands over JSON-quoted
        val json = runCatching { org.json.JSONTokener(result).nextValue() as? String }.getOrNull() ?: return
        val arr = runCatching { JSONArray(json) }.getOrNull() ?: return
        val added = ArrayList<ChatMessage>()
        for (i in 0 until arr.length()) {
            val p = arr.optJSONArray(i) ?: continue
            if (p.length() != 2) continue
            val a = p.optString(0); val t = p.optString(1)
            if (seen.add(a + "\u0001" + t)) added += ChatMessage(++nextId, a, t)
        }
        if (added.isNotEmpty()) messages.value = (messages.value + added).takeLast(60)
    }
}

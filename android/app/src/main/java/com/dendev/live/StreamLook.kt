// What viewers see on top of the game: logo, banner (lower third), LIVE badge
// with timer, and the full "Be right back" card when the screen is hidden.
// Drawn into one transparent picture of the stream size, put over the video
// by the streaming library (OpenGL), redrawn once per second.

package com.dendev.live

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import java.io.File

object StreamLook {
    val bannerColors = intArrayOf(
        Color.rgb(230, 30, 60),     // red
        Color.rgb(20, 110, 240),    // blue
        Color.rgb(20, 20, 24),      // black
        Color.rgb(140, 50, 220),    // purple
    )
    private var logoCache: Pair<String, Bitmap?>? = null

    private fun logo(path: String): Bitmap? {
        if (path.isEmpty()) return null
        logoCache?.let { if (it.first == path) return it.second }
        val b = runCatching { if (File(path).isFile) BitmapFactory.decodeFile(path) else null }.getOrNull()
        logoCache = path to b
        return b
    }

    /** One overlay picture (width x height = the stream size). */
    fun draw(w: Int, h: Int, c: LiveConfig, seconds: Int?): Bitmap {
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        val cv = Canvas(bmp)
        val u = minOf(w, h) / 720f                       // 1 unit = 1 px at 720p
        val paint = Paint(Paint.ANTI_ALIAS_FLAG)

        if (c.privacy) {                                  // "Be right back" card covers the game
            paint.shader = LinearGradient(0f, 0f, w.toFloat(), h.toFloat(),
                Color.rgb(20, 24, 60), Color.rgb(90, 20, 50), Shader.TileMode.CLAMP)
            cv.drawRect(0f, 0f, w.toFloat(), h.toFloat(), paint)
            paint.shader = null
            paint.color = Color.WHITE
            paint.typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
            paint.textSize = 64 * u
            paint.textAlign = Paint.Align.CENTER
            cv.drawText(c.brbText.ifBlank { "Be right back" }, w / 2f, h / 2f + 20 * u, paint)
            paint.textAlign = Paint.Align.LEFT
        }

        // logo in a corner
        logo(c.logoPath)?.let { lg ->
            val lh = h * c.logoScale.coerceIn(0.05f, 0.4f)
            val lw = lh * lg.width / lg.height.coerceAtLeast(1)
            val m = 24 * u
            val x = if (c.logoCorner % 2 == 0) m else w - lw - m
            val y = if (c.logoCorner < 2) m else h - lh - m - (if (c.banner.isNotBlank()) 90 * u else 0f)
            cv.drawBitmap(lg, null, RectF(x, y, x + lw, y + lh), Paint(Paint.FILTER_BITMAP_FLAG))
        }

        // LIVE badge + timer (top-left, or top-right when the logo is top-left)
        if (c.badge && seconds != null) {
            val t = "%d:%02d".format(seconds / 60, seconds % 60).let { if (seconds >= 3600) "%d:%02d:%02d".format(seconds / 3600, seconds / 60 % 60, seconds % 60) else it }
            paint.typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
            paint.textSize = 26 * u
            val live = "LIVE"
            val lw = paint.measureText(live)
            val tw = paint.measureText(t)
            val padX = 14 * u; val hgt = 40 * u; val gap = 10 * u
            val total = padX + lw + padX + gap + tw + padX
            val x0 = if (c.logoCorner == 0 && c.logoPath.isNotEmpty()) w - total - 24 * u else 24 * u
            val y0 = 24 * u
            paint.color = Color.rgb(235, 25, 60)
            cv.drawRoundRect(RectF(x0, y0, x0 + padX * 2 + lw, y0 + hgt), 8 * u, 8 * u, paint)
            paint.color = Color.argb(150, 0, 0, 0)
            cv.drawRoundRect(RectF(x0 + padX * 2 + lw, y0, x0 + total, y0 + hgt), 8 * u, 8 * u, paint)
            paint.color = Color.WHITE
            cv.drawText(live, x0 + padX, y0 + hgt * 0.7f, paint)
            cv.drawText(t, x0 + padX * 2 + lw + gap, y0 + hgt * 0.7f, paint)
        }

        // banner (lower third)
        if (c.banner.isNotBlank()) {
            paint.typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
            paint.textSize = 34 * u
            val text = c.banner.trim()
            val tw = paint.measureText(text).coerceAtMost(w - 120 * u)
            val bh = 62 * u
            val x0 = 24 * u; val y0 = h - bh - 28 * u
            paint.color = bannerColors[c.bannerColor.coerceIn(0, 3)]
            cv.drawRoundRect(RectF(x0, y0, x0 + tw + 48 * u, y0 + bh), 10 * u, 10 * u, paint)
            paint.color = Color.WHITE
            cv.save()
            cv.clipRect(x0, y0, x0 + tw + 36 * u, y0 + bh)
            cv.drawText(text, x0 + 24 * u, y0 + bh * 0.68f, paint)
            cv.restore()
        }
        return bmp
    }
}

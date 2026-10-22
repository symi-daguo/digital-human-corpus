package com.symi.digitalhuman.audio

import java.io.File
import java.io.RandomAccessFile

/**
 * 流式 WAV 写入器：先写占位头，边写边追加 PCM，close/flushHeader 时回写真实长度。
 * 支持"补头修复"：进程被杀后文件缺少正确头，可对已有 PCM 数据重新计算并回写。
 */
class WavWriter(private val file: File, private val sampleRate: Int = 16000, private val channels: Short = 1) {
    private var raf: RandomAccessFile? = null
    private var dataBytes = 0L

    val byteSize: Long get() = dataBytes + 44

    fun start() {
        raf = RandomAccessFile(file, "rw").apply {
            setLength(0)
            writeHeader(0)
            seek(44)
        }
        dataBytes = 0
    }

    fun write(pcm: ShortArray, offset: Int = 0, len: Int = pcm.size) {
        val r = raf ?: return
        val buf = ByteArray(len * 2)
        var bi = 0
        for (i in offset until offset + len) {
            val v = pcm[i].toInt()
            buf[bi++] = (v and 0xFF).toByte()
            buf[bi++] = ((v shr 8) and 0xFF).toByte()
        }
        r.write(buf)
        dataBytes += buf.size
    }

    fun close() {
        flushHeader()
        raf?.close()
        raf = null
    }

    /** 回写真实数据长度到 WAV 头 */
    fun flushHeader() {
        val r = raf ?: return
        val mark = r.filePointer
        r.seek(0)
        writeHeader(dataBytes)
        r.seek(mark)
    }

    private fun writeHeader(dataLen: Long) {
        val r = raf ?: return
        val byteRate = sampleRate * channels * 2
        val blockAlign = channels * 2
        r.writeBytes("RIFF")
        r.writeIntLe((36 + dataLen).toInt())
        r.writeBytes("WAVE")
        r.writeBytes("fmt ")
        r.writeIntLe(16)
        r.writeShortLe(1)             // PCM
        r.writeShortLe(channels.toInt())
        r.writeIntLe(sampleRate)
        r.writeIntLe(byteRate)
        r.writeShortLe(blockAlign)
        r.writeShortLe(16)            // bitsPerSample
        r.writeBytes("data")
        r.writeIntLe(dataLen.toInt())
    }

    companion object {
        /** 崩溃恢复：对孤儿 PCM 文件补头（把前 44 字节视为待修头部） */
        fun repair(file: File, sampleRate: Int = 16000, channels: Short = 1): Boolean {
            if (!file.exists() || file.length() <= 44) return false
            val dataLen = file.length() - 44
            val w = WavWriter(file, sampleRate, channels)
            w.start()
            w.dataBytes = dataLen
            w.close()
            return true
        }

        private fun RandomAccessFile.writeIntLe(v: Int) {
            write(v and 0xFF); write((v shr 8) and 0xFF); write((v shr 16) and 0xFF); write((v shr 24) and 0xFF)
        }
        private fun RandomAccessFile.writeShortLe(v: Int) {
            write(v and 0xFF); write((v shr 8) and 0xFF)
        }
    }
}

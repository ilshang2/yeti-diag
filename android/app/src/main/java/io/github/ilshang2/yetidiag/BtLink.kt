package io.github.ilshang2.yetidiag

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothSocket
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.UUID

/**
 * Classic Bluetooth SPP link to an ELM327 (what Windows shows as a COM port).
 *
 * A reader thread collects incoming bytes, so Python's AndroidSerial can read with a
 * timeout like pyserial: read(max, timeoutMs) returns what arrived, possibly nothing.
 */
class BtLink private constructor(private val socket: BluetoothSocket) {
    private val input = socket.inputStream
    private val output = socket.outputStream
    private val buf = ByteArrayOutputStream()
    private val lock = Object()
    @Volatile var closed = false
        private set

    private val reader = Thread {
        val tmp = ByteArray(1024)
        try {
            while (true) {
                val n = input.read(tmp)
                if (n < 0) break
                synchronized(lock) {
                    buf.write(tmp, 0, n)
                    lock.notifyAll()
                }
            }
        } catch (_: IOException) {
        } finally {
            closed = true
            synchronized(lock) { lock.notifyAll() }
        }
    }.apply { isDaemon = true; name = "elm-reader"; start() }

    /** The app's stop button: the running command's next read or write fails. Never used during a write. */
    @Volatile private var aborted = false

    fun abort() {
        aborted = true
        synchronized(lock) { lock.notifyAll() }
    }

    fun resume() {
        aborted = false
    }

    fun read(max: Int, timeoutMs: Int): ByteArray {
        val deadline = System.currentTimeMillis() + timeoutMs
        synchronized(lock) {
            if (aborted) throw IOException("已按停止")
            while (buf.size() == 0 && !closed && !aborted) {
                val left = deadline - System.currentTimeMillis()
                if (left <= 0) break
                lock.wait(left)
            }
            if (buf.size() == 0) {
                if (closed) throw IOException("藍牙連線已中斷")
                return ByteArray(0)
            }
            val all = buf.toByteArray()
            val n = minOf(max, all.size)
            buf.reset()
            if (n < all.size) buf.write(all, n, all.size - n)
            return all.copyOf(n)
        }
    }

    fun write(data: ByteArray) {
        if (closed) throw IOException("藍牙連線已中斷")
        if (aborted) throw IOException("已按停止")
        output.write(data)
        output.flush()
    }

    fun clear() {
        synchronized(lock) { buf.reset() }
    }

    fun close() {
        closed = true
        try { socket.close() } catch (_: IOException) {}
    }

    companion object {
        private val SPP: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")

        /** Try the usual socket kinds; cheap clones sometimes only accept channel 1 directly. */
        @SuppressLint("MissingPermission")
        fun connect(device: BluetoothDevice, log: (String) -> Unit): BtLink {
            val attempts = listOf<Pair<String, () -> BluetoothSocket>>(
                "SPP (不加密)" to { device.createInsecureRfcommSocketToServiceRecord(SPP) },
                "SPP" to { device.createRfcommSocketToServiceRecord(SPP) },
                "頻道 1" to {
                    device.javaClass.getMethod("createRfcommSocket", Int::class.javaPrimitiveType)
                        .invoke(device, 1) as BluetoothSocket
                },
            )
            var last: Exception? = null
            for ((name, make) in attempts) {
                var s: BluetoothSocket? = null
                try {
                    log("連線方式：$name …\n")
                    s = make()
                    s.connect()
                    return BtLink(s)
                } catch (e: Exception) {
                    last = e
                    try { s?.close() } catch (_: IOException) {}
                    log("  失敗：${e.message}\n")
                }
            }
            throw IOException("連不上 ${device.name}：${last?.message}")
        }
    }
}

"""ELM327 over an Android Bluetooth socket (`--elm android`, only inside the APK).

The Android app (android/, Kotlin `BtLink`) opens the classic Bluetooth SPP socket and
sets `LINK` before calling the command line. `AndroidSerial` looks like the pyserial
object `Elm327` uses (read / write / reset_input_buffer / close), the same way
`ble.BleSerial` does, so everything above it runs unchanged.

The app owns the socket and keeps it open between commands: reopening a Bluetooth
link right after closing it failed on Windows (COM9, 2026-11-04), so `close` here
does nothing.
"""

LINK = None  # BtLink from the app: read(max, timeout_ms) -> byte[], write(byte[]), clear()


class AndroidSerial:
    def __init__(self, link=None, timeout=0.05):
        self.link = link or LINK
        if self.link is None:
            raise OSError("手機還沒連上接頭 (先在 App 裡按「連線」)")
        self.timeout = timeout

    def _io(self, fn, *args):
        """A dropped link or the app's stop button arrives as a Java exception; the
        command line expects OSError, like pyserial raises (reported as a connection error)."""
        try:
            return fn(*args)
        except OSError:
            raise
        except Exception as e:  # noqa: BLE001 - java.io.IOException from BtLink
            raise OSError(getattr(e, "getMessage", lambda: None)() or str(e)) from e

    def write(self, data):
        self._io(self.link.write, bytes(data))
        return len(data)

    def read(self, n):
        return bytes(self._io(self.link.read, n, max(1, int(self.timeout * 1000))))

    def reset_input_buffer(self):
        self._io(self.link.clear)

    def close(self):
        pass

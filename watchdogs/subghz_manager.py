"""watchdogs/subghz_manager.py — Monster RF (Lab5 C5Monster / JanOS RF) bridge.

Talks to the Monster RF (ESP32-C5 + CC1101, running JanOS RF) over serial and
exposes its sub-GHz console to the game: scan, receive-capture, list stored
signals, replay a saved capture. Receive/replay operate on signals the user
captures from their OWN devices — no manufacturer-key extraction or defeating
of protected keystores.

JanOS RF serial commands used (verified on-device):
  subghz_status                      -> [SUBGHZ_STATUS] mode= freq= keys= rssi=
  subghz_scanner [dwell] [rssi] fast -> continuous edge-activity scan
  subghz_rx [raw] [rssi=-80]         -> listen; raw = save timings
  subghz_list  <mem|sd>              -> list stored signals
  subghz_info  <index> <mem|sd>      -> signal details
  subghz_replay_test <capture.raw.sub> -> replay a saved RAW .sub (TX)
The Monster is a CH340 board (/dev/ttyUSB*); the pentest XIAO is native-USB
(/dev/ttyACM*). We identify the Monster by probing subghz_status.
"""
import glob
import re
import queue
import threading
import time
from typing import Optional

try:
    import serial
except Exception:  # pragma: no cover
    serial = None


class SubGhzManager:
    def __init__(self):
        self.port: Optional[str] = None
        self.connected = False
        self.mode = "idle"          # idle | scan | rx  (from [SUBGHZ_STATUS])
        self.freq = 433.92
        self.last_status = ""
        self.last_signal = ""
        self.rssi = -120            # live RSSI from [SUBGHZ_RSSI] (for a meter)
        self.drops = 0              # [SUBGHZ_RX_DROP] — bursts seen but not decoded
        self.recording = False      # RAW record session active
        self.proto_count = 0
        self.key_count = 0
        self._ser = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._closing = threading.Event()
        self._publish_lock = threading.RLock()
        self._events: "queue.Queue" = queue.Queue()
        self._list_buf: list = []   # buffers [SUBGHZ_LIST] rows until _END/quiet
        self._list_src = "mem"      # source of the list currently buffering
        self._list_ts = 0.0         # time of last buffered list row (for flush)
        self._dir_buf: list = []    # buffers list_dir filenames
        self._in_dir = False        # inside a list_dir listing
        self._collecting_dir = False  # findall .sub during a list_dir window
        self._info_buf: list = []   # buffers subghz_info lines until _END

    # ---------------------------------------------------------------- ports
    @staticmethod
    def available() -> bool:
        return serial is not None

    def find_port(self, exclude=()) -> Optional[str]:
        """Locate the Monster by which port answers subghz_status. The Lab5
        C5Monster is a CH340 board -> only ever /dev/ttyUSB*; we deliberately do
        NOT probe /dev/ttyACM* (the native-USB pentest XIAO, whose open() can
        hang). A busy port (e.g. one the game grabbed) just fails to open and is
        skipped. Port numbers shuffle across reboots, so we identify by the
        subghz_status reply, not the number."""
        if serial is None or self._closing.is_set():
            return None
        cands = [c for c in sorted(glob.glob("/dev/ttyUSB*")) if c not in exclude]
        for dev in cands:
            if self._closing.is_set():
                break
            s = None
            try:
                s = serial.Serial()
                s.port = dev
                s.baudrate = 115200
                s.timeout = 1
                s.dtr = False           # don't reset the ESP on open (CH340)
                s.rts = False
                if self._closing.is_set():
                    break
                s.open()
                try:
                    s.dtr = False
                    s.rts = False
                except Exception:
                    pass
                if self._closing.wait(1.8):  # CH340 boot, interruptible on shutdown
                    break
                s.reset_input_buffer()
                if self._closing.is_set():
                    break
                s.write(b"subghz_status\r\n")
                if self._closing.wait(1.0):
                    break
                resp = s.read(4000).decode(errors="replace")
                if "[SUBGHZ_STATUS]" in resp and not self._closing.is_set():
                    return dev
            except Exception:
                pass
            finally:
                try:
                    if s is not None:
                        s.close()
                except Exception:
                    pass
        return None

    def connect(self, port: Optional[str] = None, exclude=()) -> bool:
        if self._closing.is_set():
            return False
        if serial is None:
            self._events.put(("error", "pyserial not available"))
            return False
        if self.connected:
            return True
        port = port or self.find_port(exclude=exclude)
        if self._closing.is_set():
            return False
        if not port:
            self._events.put(("error", "Monster RF (JanOS RF) not found on any port"))
            return False
        s = None
        published = False
        try:
            s = serial.Serial()
            s.port = port
            s.baudrate = 115200
            s.timeout = 1
            s.dtr = False
            s.rts = False
            if self._closing.is_set():
                return False
            s.open()
            try:
                s.dtr = False
                s.rts = False
            except Exception:
                pass
            with self._publish_lock:
                if self._closing.is_set():
                    return False
                if self.connected:
                    return True  # Another connector won; close our private handle below.
                self._ser = s
                self.port = port
                self.connected = True
                self._stop.clear()
                try:
                    reader = threading.Thread(target=self._read_loop, daemon=True)
                    reader.start()
                except Exception:
                    self._ser = None
                    self._reader = None
                    self.connected = False
                    self._stop.set()
                    raise
                self._reader = reader
                published = True
            self._events.put(("log", f"[SUBGHZ] connected on {port}"))
            # Opening a CH340 port resets the ESP -> wait for JanOS RF to boot,
            # otherwise the first commands are sent into the bootloader and lost.
            if self._stop.wait(3.0) or self._closing.is_set():
                return False
            self.send("subghz_status")
            return self.connected and self._ser is s and not self._closing.is_set()
        except Exception as e:
            self._events.put(("error", f"connect failed: {e}"))
            return False
        finally:
            if s is not None and not published:
                try:
                    s.close()
                except Exception:
                    pass

    # ---------------------------------------------------------------- reader
    def _read_loop(self):
        buf = b""
        while not self._stop.is_set() and self._ser is not None:
            try:
                d = self._ser.read(4096)
            except Exception:
                break
            # END is often dropped on this console -> flush the list once the
            # burst goes quiet (JanOS-style read-until-silence).
            if self._list_buf and (time.time() - self._list_ts) > 2.5:
                self._events.put(("list_result",
                                  (self._list_src, list(self._list_buf))))
                self._list_buf = []
            if not d:
                continue
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode(errors="replace").rstrip("\r")
                if text.strip() in ("", ">"):
                    continue
                self._handle_line(text)

    def _handle_line(self, text: str):
        t = text.strip()
        if t.startswith(">"):               # drop console prompt prefix
            t = t[1:].strip()
        if not t:
            return
        # Continuous RSSI stream during RX/scan -> a meter value, NOT a log line.
        if t.startswith("[SUBGHZ_RSSI]"):
            try:
                self.rssi = int(t.split()[-1])
            except Exception:
                pass
            self._events.put(("rssi", self.rssi))
            return
        # During a list_dir window, harvest .sub filenames with findall — the
        # serial framing merges lines unpredictably, so match names anywhere.
        if self._collecting_dir:
            for nm in re.findall(r"[0-9]{3,}_\S+?\.sub", t):
                if nm not in self._dir_buf:
                    self._dir_buf.append(nm)
            return
        # subghz_info <idx> <src>: the [SUBGHZ_INFO] metadata line is clean on
        # the wire (name/type/freq/id/bits/te) -> emit it immediately. The
        # trailing [SUBGHZ_INFO_RAW]/_END can be dropped/corrupted under the
        # burst, so don't depend on them.
        if t.startswith("[SUBGHZ_INFO_ERR]"):
            return
        if t.startswith("[SUBGHZ_INFO_END]"):
            if self._info_buf:
                self._events.put(("info_result", list(self._info_buf)))
            self._info_buf = []
            return
        if t.startswith("[SUBGHZ_INFO]"):
            self._events.put(("info_meta", t))
            self._info_buf = [t]
            return
        if t.startswith("[SUBGHZ_INFO_RAW]"):
            self._info_buf.append(t)
            return
        # Transmit result (emulate). The serial link drops/merges bytes, so the
        # clean "[SUBGHZ_TX]" prefix is often mangled (e.g. "...433.z] TX raw
        # frame=14/14 reps=3"). Detect by the transmit signature too: a decoded
        # key ends with "done"; a RAW replay reports "frame=N/N" + "reps=M".
        if ("[SUBGHZ_TX]" in t or ("frame=" in t and "reps=" in t)
                or "Force raw replay" in t):
            self._events.put(("tx", t))
            return
        if t.startswith("[SUBGHZ_RENAME]") or t.startswith("[SUBGHZ_DELETE]"):
            self._events.put(("renamed", t))
            return
        if t.startswith("[SUBGHZ_RX_DROP]"):
            self.drops += 1
            self._events.put(("drop", t))
            return
        if t.startswith("[SUBGHZ_STATUS]"):
            self.last_status = t
            for tok in t.split():
                if tok.startswith("mode="):
                    self.mode = tok[5:]
                elif tok.startswith("freq="):
                    try:
                        self.freq = float(tok[5:])
                    except ValueError:
                        pass
                elif tok.startswith("keys="):   # count of signals held in mem
                    try:
                        self.key_count = int(tok[5:])
                    except ValueError:
                        pass
            self._events.put(("status", t))
            return
        if t.startswith("[SUBGHZ_PROTOS_BEGIN]"):
            try:
                self.proto_count = int(t.split("count=")[1])
            except Exception:
                pass
            self._events.put(("info", t))
            return
        if t.startswith("[SUBGHZ_KEYS_BEGIN]"):
            try:
                self.key_count = int(t.split("count=")[1])
            except Exception:
                pass
            self._events.put(("info", t))
            return
        if t.startswith("[SUBGHZ_LIST_END]"):
            src = self._list_src
            for tok in t.split():
                if tok.startswith("source="):
                    src = tok.split("=", 1)[1]
            self._events.put(("list_result", (src, list(self._list_buf))))
            self._list_buf = []
            return
        if t.startswith("[SUBGHZ_LIST]"):
            self._list_buf.append(t)     # a real row (echo never has the [tag])
            self._list_ts = time.time()
            return
        if t.startswith("[SUBGHZ_INFO"):
            self._events.put(("info", t))
            return
        # A successful decode / recognized signal (strict — drops handled above).
        low = t.lower()
        if (t.startswith("[SUBGHZ_RX]") or t.startswith("[SUBGHZ_SIGNAL]")
                or t.startswith("[SUBGHZ_DECODE]") or "proto=" in low
                or "protocol" in low or "serial=" in low
                or ("key=" in low and "0x" in low)):
            self.last_signal = t
            self._events.put(("signal", t))
            return
        # Everything else = firmware chatter / debug / task notices — info only.
        self._events.put(("info", t))

    def send(self, cmd: str):
        if not self._closing.is_set() and self._ser is not None and self.connected:
            try:
                self._ser.write((cmd + "\r\n").encode())
            except Exception as e:
                self._events.put(("error", f"write failed: {e}"))

    def poll_events(self):
        out = []
        try:
            while True:
                out.append(self._events.get_nowait())
        except queue.Empty:
            pass
        return out

    # ---------------------------------------------------------- high level
    def cmd_status(self):
        self.send("subghz_status")

    def cmd_scan(self, rssi=-75):
        self.mode = "scan"
        self.recording = False
        self.send(f"subghz_scanner rssi={rssi} fast")

    def cmd_freq(self, freq):
        """Tune the CC1101 before RX (a remote 30 kHz off the default 433.92
        decodes wrong, e.g. the real 433.89 signal read as garbage on 433.92)."""
        self.send(f"subghz_freq {float(freq):.2f}")

    def cmd_rx(self, raw=False, rssi=-80):
        self.mode = "rx"
        self.recording = bool(raw)
        self.send(f"subghz_rx raw rssi={rssi}" if raw else f"subghz_rx rssi={rssi}")

    def cmd_analyzer(self, trigger=-90):
        """Frequency analyzer (hunt): scans bands, locks the strongest signal,
        reports its REAL frequency + decodes it + captures it to mem."""
        self.mode = "analyzer"
        self.recording = True
        self.send(f"subghz_freq_analyzer {int(trigger)} hunt timeout=1500")

    def cmd_stop(self):
        # JanOS RF: the RX/scan/record loop is stopped with 'subghz_stop'
        # (the firmware itself prints "Use 'subghz_stop'").
        self.send("subghz_stop")
        self.mode = "idle"
        self.recording = False
        time.sleep(0.1)
        self.send("subghz_status")

    def cmd_list(self, where="mem"):
        self._list_src = where
        self._list_buf = []
        self._list_ts = time.time()
        self.send(f"subghz_list {where}")

    def cmd_clear(self):
        """Wipe the volatile mem cache (so a capture starts clean)."""
        self.send("subghz_clear")

    def cmd_save(self, what="all"):
        # Copy captured mem signal(s) to SD so they persist / can be replayed.
        self.send(f"subghz_save {what}")

    def cmd_list_files(self, path="/sdcard/lab/subghz"):
        # Reliable Saved listing: subghz_list sd is broken; list_dir works but
        # its output merges into a pending status/RSSI reply. So stop + drain
        # first, harvest .sub names via findall, and emit after a fixed window.
        def _work():
            self.send("subghz_stop")
            time.sleep(1.5)
            try:
                self._ser.reset_input_buffer()   # flush pending status/echo
            except Exception:
                pass
            self._dir_buf = []
            self._collecting_dir = True
            self.send(f"list_dir {path}")
            time.sleep(4.5)
            self._collecting_dir = False
            self._events.put(("files_result", list(self._dir_buf)))
        threading.Thread(target=_work, daemon=True).start()

    def cmd_info(self, index, where="sd"):
        self.send(f"subghz_info {index} {where}")

    def cmd_tx(self, index, source="sd", raw=False):
        """Transmit (emulate) a stored signal the user captured from their OWN
        device. JanOS RF: subghz_tx <index> <mem|sd> [raw]. RAW-type captures
        need the 'raw' flag; decoded protocols re-encode from the key."""
        cmd = f"subghz_tx {int(index)} {source}"
        if raw:
            cmd += " raw"
        self.send(cmd)

    def cmd_rename(self, index, name):
        """Rename a saved SD signal. JanOS RF: subghz_rename <index> <new_name>.
        new_name is sanitised to [A-Za-z0-9_.-], 1..63 chars, .sub auto-added."""
        import re as _re
        nm = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(name)).strip("_") or "signal"
        nm = nm[:63]
        self.send(f"subghz_rename {int(index)} {nm}")

    def cmd_delete(self, index):
        """Delete a saved SD signal: subghz_delete <index>."""
        self.send(f"subghz_delete {int(index)}")

    def cmd_replay(self, path: str):
        # Deprecated: subghz_replay_test is a firmware self-test that never keys
        # the transmitter. Kept only so old callers don't crash; real TX is
        # cmd_tx() -> subghz_tx <index> <mem|sd> [raw].
        self.send(f"subghz_replay_test {path}")

    def cmd_protocols(self, filt=""):
        self.send(f"subghz_protocols {filt}".strip())

    def shutdown(self) -> bool:
        """Permanently reject new connections/commands, then release our reader.

        Port probes/open calls already in progress own private handles. They
        close those handles instead of publishing them if shutdown wins.
        Ordinary stop() remains usable before a later explicit reconnect.
        """
        self._closing.set()
        with self._publish_lock:
            pass  # A publication that won the race must finish before stop's snapshot.
        return self.stop()

    def stop(self):
        """Stop our firmware receiver and release the serial reader before power-off."""
        connection = self._ser
        ok = True
        if connection is not None and self.connected:
            try:
                payload = b"subghz_stop\r\n"
                ok = connection.write(payload) == len(payload)
            except Exception as exc:
                self._events.put(("error", f"stop command failed: {exc}"))
                ok = False
        self.connected = False
        self._stop.set()
        if connection is not None:
            try:
                if hasattr(connection, "cancel_read"):
                    connection.cancel_read()
            except Exception:
                pass
            try:
                connection.close()
            except Exception as exc:
                self._events.put(("error", f"serial close failed: {exc}"))
                ok = False
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2)
        if reader is not None and reader.is_alive():
            return False
        self._reader = None
        if ok:
            self._ser = None
        self.mode = "idle"
        return ok

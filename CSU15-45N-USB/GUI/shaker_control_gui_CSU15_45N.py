from __future__ import annotations

import csv
from array import array
import importlib.util
import math
import os
import queue
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path


def _relaunch_with_project_python_if_needed() -> None:
    """Use the known Python 3.12 installation automatically when needed.

    This lets the user start the GUI by double-clicking the launcher/script
    instead of typing the full Python 3.12 command manually.
    """
    required_modules = ("PySide6", "pyqtgraph", "pyvisa", "serial")
    missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
    if not missing:
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if local_app_data:
        base = Path(local_app_data) / "Programs" / "Python" / "Python312"
        candidates.extend([base / "pythonw.exe", base / "python.exe"])

    try:
        current = Path(sys.executable).resolve()
    except Exception:
        current = Path(sys.executable)

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            if candidate.resolve() == current:
                continue
        except Exception:
            pass
        os.execv(
            str(candidate),
            [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]],
        )


_relaunch_with_project_python_if_needed()

import pyqtgraph as pg
import pyvisa
import serial
from PySide6.QtCore import QThread, QTimer, Qt, Signal, QRectF
from PySide6.QtGui import QFont, QPixmap, QPainter
from PySide6.QtSerialPort import QSerialPortInfo
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


APP_TITLE = "Shaker Control & DAQ - Stable Zoom + Exact MSO4104B Screen"

MOTOR_BAUD = 115200
FORCE_BAUD = 115200

# SingleTact CSU15-45N calibration
FORCE_RATING_N = 45.0
FORCE_COUNTS_FULL_SCALE = 512.0
FORCE_FACTORY_BASELINE = 255.0
FORCE_I2C_ADDRESS = 0x04
FORCE_READ_LOCATION = 128
FORCE_READ_LENGTH = 6
FORCE_POLL_INTERVAL_S = 0.010
FORCE_REPLY_TIMEOUT_S = 0.15


class MotorWorker(QThread):
    """Dedicated serial thread for Arduino motor + encoder traffic."""

    connection_changed = Signal(bool, str)
    line_received = Signal(str)
    error = Signal(str)

    def __init__(self, port: str, parent=None):
        super().__init__(parent)
        self.port = port
        self._stop_event = threading.Event()
        self._commands: queue.Queue[str] = queue.Queue()
        self._brake_on_exit = True

    def send_command(self, command: str) -> None:
        if not self._stop_event.is_set():
            self._commands.put(command.strip())

    def request_stop(self, brake_on_exit: bool = True) -> None:
        self._brake_on_exit = brake_on_exit
        self._stop_event.set()

    def run(self) -> None:
        ser = None
        rx = bytearray()
        try:
            ser = serial.Serial(
                self.port,
                MOTOR_BAUD,
                timeout=0.02,
                write_timeout=0.3,
            )
            # Opening an Arduino USB serial port normally resets the board.
            if self._stop_event.wait(2.0):
                return
            try:
                ser.reset_input_buffer()
            except Exception:
                pass

            self.connection_changed.emit(True, f"Motor: connected to {self.port} @ {MOTOR_BAUD}")

            while not self._stop_event.is_set():
                # Drain a bounded number of outgoing commands each cycle.
                for _ in range(20):
                    try:
                        command = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    payload = (command + "\n").encode("ascii", errors="ignore")
                    ser.write(payload)
                    ser.flush()

                waiting = ser.in_waiting
                data = ser.read(waiting if waiting > 0 else 1)
                if data:
                    rx.extend(data)
                    while b"\n" in rx:
                        raw, _, remainder = rx.partition(b"\n")
                        rx = bytearray(remainder)
                        line = raw.decode("ascii", errors="ignore").strip()
                        if line:
                            self.line_received.emit(line)

        except Exception as exc:
            self.error.emit(f"Motor serial error: {exc}")
        finally:
            if ser is not None:
                try:
                    if self._brake_on_exit:
                        ser.write(b"MOTOR_BRAKE\n")
                        ser.flush()
                        time.sleep(0.03)
                except Exception:
                    pass
                try:
                    ser.close()
                except Exception:
                    pass
            self.connection_changed.emit(False, "Motor: disconnected")


class ForceWorker(QThread):
    """Dedicated SingleTact CSU15-45N USB acquisition thread."""

    connection_changed = Signal(bool, str)
    sample_received = Signal(int, int, float)
    timeout_changed = Signal(int)
    error = Signal(str)

    def __init__(self, port: str, parent=None):
        super().__init__(parent)
        self.port = port
        self._stop_event = threading.Event()
        self._rx = bytearray()
        self._command_id = 0
        self._last_iteration: int | None = None
        self._timeouts = 0

    def request_stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _build_read_command(command_id: int) -> bytes:
        packet = bytearray(16)
        packet[0:4] = b"\xFF\xFF\xFF\xFF"
        packet[4] = FORCE_I2C_ADDRESS
        packet[5] = 100
        packet[6] = command_id & 0xFF
        packet[7] = 0x01
        packet[8] = FORCE_READ_LOCATION
        packet[9] = FORCE_READ_LENGTH
        packet[10] = 0xFF
        packet[11:15] = b"\xFE\xFE\xFE\xFE"
        packet[15] = 0x00
        return bytes(packet)

    @staticmethod
    def _header_at(buf: bytearray, index: int) -> bool:
        if index + 4 > len(buf):
            return False
        return all(buf[index + i] in (0xAA, 0xFF) for i in range(4))

    def _take_packets(self):
        packets = []
        while True:
            if len(self._rx) < 16:
                break

            start = None
            for i in range(len(self._rx) - 3):
                if self._header_at(self._rx, i):
                    start = i
                    break
            if start is None:
                if len(self._rx) > 3:
                    del self._rx[:-3]
                break
            if start > 0:
                del self._rx[:start]
            if len(self._rx) < 12:
                break

            n_bytes = self._rx[11]
            packet_length = 16 + n_bytes
            if len(self._rx) < packet_length:
                break

            packet = bytes(self._rx[:packet_length])
            if packet[-4:] != b"\xFE\xFE\xFE\xFE":
                del self._rx[0]
                continue
            del self._rx[:packet_length]
            packets.append(packet)
        return packets

    def _wait_for_reply(self, ser: serial.Serial, expected_id: int):
        deadline = time.perf_counter() + FORCE_REPLY_TIMEOUT_S
        while time.perf_counter() < deadline and not self._stop_event.is_set():
            waiting = ser.in_waiting
            chunk = ser.read(waiting if waiting > 0 else 1)
            if chunk:
                self._rx.extend(chunk)
                for packet in self._take_packets():
                    if len(packet) >= 22 and packet[6] == expected_id:
                        return packet
        return None

    def run(self) -> None:
        ser = None
        try:
            ser = serial.Serial(
                self.port,
                FORCE_BAUD,
                timeout=0.01,
                write_timeout=0.3,
            )

            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                ser.dtr = True
                time.sleep(0.010)
                ser.dtr = False
            except Exception:
                pass

            # Match the reset delay used by the official SingleTact driver.
            if self._stop_event.wait(2.0):
                return

            try:
                ser.rts = True
                ser.reset_input_buffer()
                ser.reset_output_buffer()
            except Exception:
                pass

            self.connection_changed.emit(
                True,
                f"Force: connected to {self.port} @ {FORCE_BAUD}; receiving in dedicated thread",
            )

            while not self._stop_event.is_set():
                cycle_start = time.perf_counter()
                command_id = self._command_id & 0xFF
                self._command_id = (self._command_id + 1) & 0xFF

                ser.write(self._build_read_command(command_id))
                ser.flush()
                packet = self._wait_for_reply(ser, command_id)

                if packet is None:
                    self._timeouts += 1
                    self.timeout_changed.emit(self._timeouts)
                else:
                    n_bytes = packet[11]
                    if packet[5] == 0 and n_bytes >= FORCE_READ_LENGTH:
                        data = packet[12 : 12 + n_bytes]
                        if len(data) >= 6:
                            iteration = (data[0] << 8) | data[1]
                            raw_counts = (data[4] << 8) | data[5]
                            if iteration != self._last_iteration:
                                self._last_iteration = iteration
                                self.sample_received.emit(raw_counts, iteration, time.perf_counter())

                elapsed = time.perf_counter() - cycle_start
                remaining = FORCE_POLL_INTERVAL_S - elapsed
                if remaining > 0:
                    self._stop_event.wait(remaining)

        except Exception as exc:
            self.error.emit(f"Force sensor error: {exc}")
        finally:
            if ser is not None:
                try:
                    ser.rts = False
                    ser.dtr = False
                except Exception:
                    pass
                try:
                    ser.close()
                except Exception:
                    pass
            self.connection_changed.emit(False, "Force: disconnected")


class ScopeWorker(QThread):
    """Dedicated VISA thread for all MSO4104B USB communication and sampling."""

    resources_ready = Signal(object)
    connected = Signal(str, str)
    disconnected = Signal()
    status = Signal(str)
    settings_ready = Signal(object)
    waveform_ready = Signal(object, object, object)
    screen_ready = Signal(bytes, float)
    sampling_state = Signal(bool, str)
    error = Signal(str)

    COALESCED_TASKS = {"screen", "waveform", "sync", "refresh_resources"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._tasks: queue.Queue[tuple[str, object]] = queue.Queue()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._rm = None
        self._scope = None
        self._resource_name: str | None = None

        # Session sampling state. All instrument I/O remains in this one thread.
        self._sampling_active = False  # live waveform polling while connected
        self._session_active = False   # whether captured frames belong to a recording
        self._sampling_t0 = 0.0
        self._sampling_channel = "CH1"
        self._sampling_interval_s = 0.5
        self._next_sample_due = 0.0
        self._sampling_capture_index = 0

    def request(self, kind: str, payload=None) -> None:
        if self._stop_event.is_set():
            return
        if kind in self.COALESCED_TASKS:
            with self._pending_lock:
                if kind in self._pending:
                    return
                self._pending.add(kind)
        self._tasks.put((kind, payload))

    def request_stop(self) -> None:
        self._stop_event.set()
        self._tasks.put(("stop", None))

    def _clear_pending(self, kind: str) -> None:
        if kind in self.COALESCED_TASKS:
            with self._pending_lock:
                self._pending.discard(kind)

    def _resource_manager(self):
        if self._rm is None:
            self._rm = pyvisa.ResourceManager()
        return self._rm

    def _require_scope(self):
        if self._scope is None:
            raise RuntimeError("Oscilloscope is not connected")
        return self._scope

    @staticmethod
    def _extract_png(data: bytes) -> bytes:
        signature = b"\x89PNG\r\n\x1a\n"
        start = data.find(signature)
        if start < 0:
            raise RuntimeError("The oscilloscope response did not contain a PNG image")
        png = data[start:]
        marker = b"\x00\x00\x00\x00IEND\xaeB`\x82"
        end = png.find(marker)
        if end >= 0:
            png = png[: end + len(marker)]
        return png

    def _refresh_resources(self) -> None:
        rm = self._resource_manager()
        resources = list(rm.list_resources())
        usb = [str(r) for r in resources if str(r).upper().startswith("USB")]
        shown = usb if usb else [str(r) for r in resources]
        self.resources_ready.emit(shown)

    def _connect(self, resource: str) -> None:
        if self._scope is not None:
            try:
                self._scope.close()
            except Exception:
                pass
            self._scope = None

        rm = self._resource_manager()
        inst = rm.open_resource(resource)
        inst.timeout = 5000
        inst.chunk_size = 1024 * 1024
        inst.write_termination = "\n"
        inst.read_termination = "\n"
        idn = inst.query("*IDN?").strip()
        inst.write("HEADER 0")
        self._scope = inst
        self._resource_name = resource
        # Free-running acquisitions start immediately at Connect, even before START.
        inst.write("ACQUIRE:STOPAFTER RUNSTOP")
        inst.write("ACQUIRE:STATE RUN")
        self._sampling_active = True
        self._session_active = False
        self._next_sample_due = time.perf_counter() + 0.05
        self.connected.emit(idn, resource)

    def _disconnect(self) -> None:
        self._sampling_active = False
        self._session_active = False
        if self._scope is not None:
            # Disconnecting the GUI should not STOP the front-panel acquisition.
            try:
                self._scope.close()
            except Exception:
                pass
        self._scope = None
        self._resource_name = None
        self.disconnected.emit()

    def _sync_settings(self, channel: str) -> None:
        scope = self._require_scope()
        result = {
            "channel": channel,
            "vertical_scale": float(str(scope.query(f"{channel}:SCALE?")).strip()),
            "vertical_position": float(str(scope.query(f"{channel}:POSITION?")).strip()),
            "time_scale": float(str(scope.query("HORIZONTAL:MAIN:SCALE?")).strip()),
            "coupling": str(scope.query(f"{channel}:COUPLING?")).strip().upper(),
        }
        try:
            source = str(scope.query("TRIGGER:A:EDGE:SOURCE?")).strip().upper()
            slope = str(scope.query("TRIGGER:A:EDGE:SLOPE?")).strip().upper()
            result["trigger_source"] = source
            result["trigger_slope"] = slope
            if source in {"CH1", "CH2", "CH3", "CH4"}:
                result["trigger_level"] = float(
                    str(scope.query(f"TRIGGER:A:LEVEL:{source}?")).strip()
                )
        except Exception:
            pass
        self.settings_ready.emit(result)

    def _write_commands(self, payload: dict) -> None:
        scope = self._require_scope()
        for command in payload.get("commands", []):
            scope.write(command)
        label = payload.get("label")
        if label:
            self.status.emit(str(label))
        if payload.get("sync"):
            self._sync_settings(payload.get("channel", "CH1"))
        # Background numerical capture continues independently. Do not
        # force an extra waveform query for every user interface change.
        if payload.get("screen"):
            self._read_screen()

    def _read_screen(self) -> None:
        scope = self._require_scope()
        old_term = scope.read_termination
        try:
            scope.write("SAVE:IMAGE:FILEFORMAT PNG")
            scope.write("HARDCOPY:INKSAVER OFF")
            scope.write("HARDCOPY:LAYOUT LANDSCAPE")
            scope.read_termination = None
            # Tag image at acquisition request time to reject a stale frame
            # that was already in flight when the user pressed START or STOP.
            capture_started = time.perf_counter()
            scope.write("HARDCOPY START")
            raw = scope.read_raw()
            png = self._extract_png(bytes(raw))
            self.screen_ready.emit(png, capture_started)
            self.status.emit(
                f"Oscilloscope: exact screen image received over USB ({len(png) / 1024:.0f} kB)"
            )
        finally:
            scope.read_termination = old_term

    def _read_waveform(
        self,
        channel: str,
        session_elapsed_s: float | None = None,
        capture_index: int | None = None,
    ) -> None:
        scope = self._require_scope()
        capture_monotonic_s = time.perf_counter()
        scope.write("HEADER 0")
        # Reading waveform data must not turn a channel on or change the front-panel display.
        scope.write(f"DATA:SOURCE {channel}")
        scope.write("DATA:START 1")
        try:
            record_length = int(float(str(scope.query("HORIZONTAL:RECORDLENGTH?")).strip()))
        except Exception:
            record_length = 10000
        points = max(1, min(record_length, 10000))
        scope.write(f"DATA:STOP {points}")
        scope.write("WFMOUTPRE:ENCDG BINARY")
        scope.write("WFMOUTPRE:BN_FMT RI")
        scope.write("WFMOUTPRE:BYT_NR 1")
        scope.write("WFMOUTPRE:BYT_OR MSB")

        def qf(command: str) -> float:
            return float(str(scope.query(command)).strip())

        x_incr = qf("WFMOUTPRE:XINCR?")
        x_zero = qf("WFMOUTPRE:XZERO?")
        try:
            pt_off = qf("WFMOUTPRE:PT_OFF?")
        except Exception:
            pt_off = 0.0
        y_mult = qf("WFMOUTPRE:YMULT?")
        y_off = qf("WFMOUTPRE:YOFF?")
        y_zero = qf("WFMOUTPRE:YZERO?")

        raw = scope.query_binary_values(
            "CURVE?",
            datatype="b",
            is_big_endian=True,
            container=list,
            expect_termination=True,
        )
        if not raw:
            raise RuntimeError("CURVE? returned no samples")

        x_values = [x_zero + (i - pt_off) * x_incr for i in range(len(raw))]
        y_values = [(float(v) - y_off) * y_mult + y_zero for v in raw]
        minimum = min(y_values)
        maximum = max(y_values)
        mean = sum(y_values) / len(y_values)
        rms = math.sqrt(sum(v * v for v in y_values) / len(y_values))
        metrics = {
            "capture_monotonic_s": capture_monotonic_s,
            "channel": channel,
            "points": len(y_values),
            "min": minimum,
            "max": maximum,
            "vpp": maximum - minimum,
            "mean": mean,
            "rms": rms,
            "x_increment_s": x_incr,
        }
        if session_elapsed_s is not None:
            metrics["session_elapsed_s"] = float(session_elapsed_s)
        if capture_index is not None:
            metrics["capture_index"] = int(capture_index)
        self.waveform_ready.emit(x_values, y_values, metrics)

    def _start_sampling(self, payload: dict) -> None:
        scope = self._require_scope()
        self._sampling_t0 = float(payload.get("t0", time.perf_counter()))
        self._sampling_channel = str(payload.get("channel", "CH1"))
        self._sampling_interval_s = max(0.1, float(payload.get("interval_s", 0.5)))
        self._sampling_capture_index = 0
        scope.write("ACQUIRE:STOPAFTER RUNSTOP")
        scope.write("ACQUIRE:STATE RUN")
        self._session_active = True
        self._sampling_active = True
        self._next_sample_due = time.perf_counter() + 0.01
        self.sampling_state.emit(True, "Oscilloscope: live acquisition + session recording")

    def _stop_sampling(self) -> None:
        # STOP ends only the in-memory recording session. The scope continues
        # acquiring and our live transfer loop remains active.
        self._session_active = False
        self.sampling_state.emit(False, "Oscilloscope: recording stopped; live waveform continues")

    def _set_live_interval(self, interval_s: float) -> None:
        self._sampling_interval_s = max(0.1, float(interval_s))
        self._next_sample_due = time.perf_counter() + 0.01

    def _sampling_due(self) -> bool:
        return (
            self._sampling_active
            and self._scope is not None
            and time.perf_counter() >= self._next_sample_due
        )

    def _perform_sampling_capture(self) -> None:
        if not self._sampling_due():
            return
        now = time.perf_counter()
        self._sampling_capture_index += 1
        elapsed = max(0.0, now - self._sampling_t0) if self._session_active else None
        index = self._sampling_capture_index if self._session_active else None
        try:
            self._read_waveform(
                self._sampling_channel,
                session_elapsed_s=elapsed,
                capture_index=index,
            )
        except Exception as exc:
            self.error.emit(f"Oscilloscope live waveform read failed: {exc}")
        finally:
            # A slow USB transfer must never generate a backlog of captures.
            self._next_sample_due = time.perf_counter() + self._sampling_interval_s

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                timeout = 0.05
                if self._sampling_active:
                    timeout = max(0.0, min(0.05, self._next_sample_due - time.perf_counter()))
                try:
                    kind, payload = self._tasks.get(timeout=timeout)
                except queue.Empty:
                    self._perform_sampling_capture()
                    continue

                try:
                    if kind == "stop":
                        break
                    if kind == "refresh_resources":
                        self._refresh_resources()
                    elif kind == "connect":
                        self._connect(str(payload))
                    elif kind == "disconnect":
                        self._disconnect()
                    elif kind == "sync":
                        self._sync_settings(str(payload or "CH1"))
                    elif kind == "commands":
                        self._write_commands(dict(payload or {}))
                    elif kind == "screen":
                        self._read_screen()
                    elif kind == "waveform":
                        self._read_waveform(str(payload or "CH1"))
                    elif kind == "start_sampling":
                        self._start_sampling(dict(payload or {}))
                    elif kind == "stop_sampling":
                        self._stop_sampling()
                    elif kind == "live_interval":
                        self._set_live_interval(float(payload))
                except Exception as exc:
                    self.error.emit(f"Oscilloscope task '{kind}' failed: {exc}")
                finally:
                    self._clear_pending(kind)

                self._perform_sampling_capture()
        finally:
            try:
                self._sampling_active = False
                self._stop_sampling()
            except Exception:
                pass
            try:
                self._disconnect()
            except Exception:
                pass
            if self._rm is not None:
                try:
                    self._rm.close()
                except Exception:
                    pass
                self._rm = None


class CSVWriterWorker(QThread):
    """Writes completed, RAM-buffered recordings on explicit Save As only."""

    session_saved = Signal(str, str, str)
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: queue.Queue[tuple[str, object]] = queue.Queue()

    def save_session(self, folder: str, stamp: str, force_rows: list, scope_records: list) -> None:
        # Lists are not modified while saving; the worker reads them directly.
        self._tasks.put(("save", (folder, stamp, force_rows, scope_records)))

    def request_stop(self) -> None:
        self._tasks.put(("shutdown", None))

    def _save(self, payload: tuple) -> None:
        folder, stamp, forces, scopes = payload
        directory = Path(folder).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        session_dir = directory / f"session_{stamp}"
        suffix = 1
        while session_dir.exists():
            session_dir = directory / f"session_{stamp}_{suffix:02d}"
            suffix += 1
        session_dir.mkdir(parents=True)
        force_path = session_dir / "force_sensor.csv"
        scope_path = session_dir / "oscilloscope.csv"
        try:
            with force_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Time (s)", "Raw Counts", "Force (N)",
                                 "Motor Position (counts)", "Motor State"])
                writer.writerows(forces)

            with scope_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Capture Time (s)", "Channel", "Sample Index",
                                 "Waveform Time (s)", "Voltage (V)"])
                for elapsed, channel, x0, step, ys in scopes:
                    elapsed_text = f"{elapsed:.6f}"
                    for i, v in enumerate(ys):
                        writer.writerow([elapsed_text, channel, i,
                                         f"{x0 + i * step:.12g}", f"{v:.12g}"])
            self.session_saved.emit(str(session_dir), str(force_path), str(scope_path))
        except Exception:
            # Preserve captured data in RAM after any disk-write failure.
            try:
                for partial in (force_path, scope_path):
                    if partial.exists():
                        partial.unlink()
                session_dir.rmdir()
            except OSError:
                pass
            raise

    def run(self) -> None:
        while True:
            kind, payload = self._tasks.get()
            if kind == "shutdown":
                return
            if kind == "save":
                try:
                    self._save(payload)
                except Exception as exc:
                    self.error.emit(f"Save As failed; recording is still in memory: {exc}")


class ScopeScreenView(QGraphicsView):
    """A native PNG screen viewer with stable, lightweight wheel zoom.

    Image updates replace only the pixmap; user zoom and pan are not reset.
    All QGraphicsItem operations take place on the Qt GUI thread.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self._has_image = False
        self._manual_zoom = False
        self._scale_factor = 1.0
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.setToolTip("Mouse wheel: zoom | Drag: pan | FIT SCREEN: restore fit")

    def show_png(self, png_bytes: bytes) -> tuple[int, int]:
        pixmap = QPixmap()
        if not pixmap.loadFromData(png_bytes, "PNG"):
            raise ValueError("The PNG screenshot could not be decoded")
        width, height = pixmap.width(), pixmap.height()
        old_size = self._item.pixmap().size()
        self._item.setPixmap(pixmap)
        if not self._has_image or old_size != pixmap.size():
            self._scene.setSceneRect(QRectF(0, 0, width, height))
            if not self._manual_zoom:
                self.fit_screen()
        self._has_image = True
        return width, height

    def fit_screen(self) -> None:
        if self._item.pixmap().isNull():
            return
        self._manual_zoom = False
        self._scale_factor = 1.0
        self.fitInView(self._item.boundingRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event) -> None:
        if not self._has_image:
            event.ignore()
            return
        # Avoid accumulated huge transforms, excessive resampling and crashes.
        zoom = 1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15
        if event.angleDelta().y() == 0:
            event.ignore()
            return
        next_scale = self._scale_factor * zoom
        if 0.20 <= next_scale <= 8.0:
            self._manual_zoom = True
            self._scale_factor = next_scale
            self.scale(zoom, zoom)
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._has_image and not self._manual_zoom:
            self.fit_screen()

    def clear_screen(self, text: str = "Waiting for a new scope screenshot...") -> None:
        self._item.setPixmap(QPixmap())
        self._scene.setSceneRect(QRectF())
        self._has_image = False
        self._manual_zoom = False
        self._scale_factor = 1.0
        self.resetTransform()
        self.setToolTip(text + "  |  Mouse wheel: zoom, drag: pan")


class MetricLabels(QWidget):
    def __init__(self, unit: str, parent=None):
        super().__init__(parent)
        self.unit = unit
        self.labels: dict[str, QLabel] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        for key, title in [
            ("current", "Current"),
            ("max", "Max"),
            ("min", "Min"),
            ("mean", "Mean"),
            ("rms", "RMS"),
        ]:
            label = QLabel(f"{title}: -- {unit}")
            label.setMinimumWidth(125)
            self.labels[key] = label
            layout.addWidget(label)

        layout.addStretch(1)

    def update_values(self, values) -> None:
        if not values:
            for key, label in self.labels.items():
                title = "RMS" if key == "rms" else key.capitalize()
                label.setText(f"{title}: -- {self.unit}")
            return

        arr = list(values)
        current = arr[-1]
        maximum = max(arr)
        minimum = min(arr)
        mean = sum(arr) / len(arr)
        rms = math.sqrt(sum(v * v for v in arr) / len(arr))

        self.labels["current"].setText(f"Current: {current:.4f} {self.unit}")
        self.labels["max"].setText(f"Max: {maximum:.4f} {self.unit}")
        self.labels["min"].setText(f"Min: {minimum:.4f} {self.unit}")
        self.labels["mean"].setText(f"Mean: {mean:.4f} {self.unit}")
        self.labels["rms"].setText(f"RMS: {rms:.4f} {self.unit}")


class ShakerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1450, 900)

        # ------------------------------------------------------------
        # Dedicated worker threads
        # ------------------------------------------------------------
        self.motor_worker: MotorWorker | None = None
        self.force_worker: ForceWorker | None = None

        self.scope_worker = ScopeWorker(self)
        self.scope_worker.resources_ready.connect(self.on_scope_resources_ready)
        self.scope_worker.connected.connect(self.on_scope_connected)
        self.scope_worker.disconnected.connect(self.on_scope_disconnected)
        self.scope_worker.status.connect(self.on_scope_status)
        self.scope_worker.settings_ready.connect(self.on_scope_settings_ready)
        self.scope_worker.waveform_ready.connect(self.on_scope_waveform_ready)
        self.scope_worker.screen_ready.connect(self.on_scope_screen_ready)
        self.scope_worker.sampling_state.connect(self.on_scope_sampling_state)
        self.scope_worker.error.connect(self.on_scope_error)
        self.scope_worker.start()

        self.csv_worker = CSVWriterWorker(self)
        self.csv_worker.session_saved.connect(self.on_csv_session_saved)
        self.csv_worker.error.connect(self.on_csv_error)
        self.csv_worker.start()

        self.motor_connected = False
        self.motor_position = 0
        self.motor_state = "UNKNOWN"
        self.estop_active = False
        self.position_brake_active = False
        self.position_brake_target: int | None = None

        self.force_connected = False
        self.force_baseline_counts = FORCE_FACTORY_BASELINE
        self.force_raw_recent = deque(maxlen=50)
        self.latest_force_raw: int | None = None
        self.latest_force_n = 0.0
        self.force_sample_counter = 0
        self.force_timeout_counter = 0

        self.scope_resource_name: str | None = None
        self.scope_connected = False
        self.scope_sampling_active = False
        self.scope_last_x: list[float] = []
        self.scope_last_y: list[float] = []

        # ------------------------------------------------------------
        # Sampling session state
        # ------------------------------------------------------------
        self.acquiring = False
        self.acquisition_t0 = 0.0
        self.window_seconds = 60
        self.force_follow_live = True
        self._force_plot_dirty = True
        self._metrics_updated_at = 0.0
        self.time_data = deque()
        self.force_data = deque()
        self.session_force_samples = 0
        self.session_scope_captures = 0
        self.recording_unsaved = False
        self.saving_in_progress = False
        self.session_force_rows: list[tuple] = []
        # Each scope capture stores (time, channel, waveform_x0, waveform_dt, array('d', volts)).
        self.session_scope_records: list[tuple] = []
        self.session_stamp = ""
        self.plot_origin_t0 = time.perf_counter()

        # ------------------------------------------------------------
        # Build UI: live DAQ and motor operation in one Dashboard.
        # Device connection widgets live in a separate Settings tab.
        # ------------------------------------------------------------
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.daq_tab = self.build_daq_tab()
        self.settings_tab = self.build_settings_tab()
        self.tabs.addTab(self.daq_tab, "Dashboard - DAQ + Motor")
        self.tabs.addTab(self.settings_tab, "Settings")

        # ------------------------------------------------------------
        # GUI-only timers. Hardware I/O remains inside its dedicated thread.
        # ------------------------------------------------------------
        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(100)
        self.plot_timer.timeout.connect(self.refresh_plot)
        self.plot_timer.start()

        # Fastest offered screenshot request interval. Actual refresh rate is
        # limited by the MSO4104B PNG rendering/USB transfer and the scope
        # worker coalesces requests, so slow transfers cannot queue up frames.
        self.scope_live_timer = QTimer(self)
        self.scope_live_timer.setInterval(50)
        self.scope_live_timer.timeout.connect(self.refresh_scope_screen)

        self.refresh_ports()
        self.refresh_scope_resources()
        self.apply_style()

    # ==================================================================
    # UI
    # ==================================================================
    def build_daq_tab(self) -> QWidget:
        """Live force/scope visualization, acquisition, and motor controls together."""
        tab = QWidget()
        outer = QVBoxLayout(tab)

        navigation = QHBoxLayout()
        info = QLabel(
            "Live force + oscilloscope + position control | "
            "Connect devices in Settings first"
        )
        info.setWordWrap(True)
        navigation.addWidget(info, 1)
        settings_button = QPushButton("OPEN CONNECTION SETTINGS")
        settings_button.setToolTip(
            "Choose and connect the force sensor, oscilloscope and Arduino Mega"
        )
        settings_button.clicked.connect(
            lambda: self.tabs.setCurrentWidget(self.settings_tab)
        )
        navigation.addWidget(settings_button)
        outer.addLayout(navigation)

        # The controls plus two full-size plots are taller than many laptop
        # screens. A native Qt scroll area prevents shrinking/clipping and
        # keeps every motor and scope command accessible.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        dashboard = QWidget()
        root = QVBoxLayout(dashboard)
        root.setSpacing(10)

        root.addWidget(self.build_sampling_box())
        root.addWidget(self.build_motor_box())
        root.addWidget(self.build_scope_controls_box())

        figures = QGridLayout()
        force_box = self.build_force_box()
        scope_box = self.build_scope_display_box()
        force_box.setMinimumHeight(390)
        scope_box.setMinimumHeight(390)
        figures.addWidget(force_box, 0, 0)
        figures.addWidget(scope_box, 0, 1)
        figures.setColumnStretch(0, 1)
        figures.setColumnStretch(1, 1)
        root.addLayout(figures, 1)
        scroll.setWidget(dashboard)
        outer.addWidget(scroll, 1)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("status")
        outer.addWidget(self.status_label)
        return tab

    def build_settings_tab(self) -> QWidget:
        """All connections in Settings; no motor movement controls here."""
        tab = QWidget()
        root = QVBoxLayout(tab)

        description = QLabel(
            "Connect each device here. The DAQ and motor worker threads "
            "continue running when you return to Dashboard."
        )
        description.setWordWrap(True)
        root.addWidget(description)

        root.addWidget(self.build_daq_connections_box())
        root.addWidget(self.build_motor_connection_box())
        root.addStretch(1)
        return tab

    def build_daq_connections_box(self) -> QGroupBox:
        box = QGroupBox("DAQ connections")
        grid = QGridLayout(box)

        grid.addWidget(QLabel("Force sensor (SingleTact CSU15-45N USB):"), 0, 0)
        self.force_port_combo = QComboBox()
        self.force_port_combo.setMinimumWidth(260)
        grid.addWidget(self.force_port_combo, 0, 1)

        self.force_connect_btn = QPushButton("Connect force sensor")
        self.force_connect_btn.clicked.connect(self.toggle_force_connection)
        grid.addWidget(self.force_connect_btn, 0, 2)

        self.refresh_ports_btn = QPushButton("Refresh COM ports")
        self.refresh_ports_btn.clicked.connect(self.refresh_ports)
        grid.addWidget(self.refresh_ports_btn, 0, 3)

        self.force_connection_label = QLabel("Force: disconnected")
        grid.addWidget(self.force_connection_label, 1, 0, 1, 4)

        grid.addWidget(QLabel("Oscilloscope VISA resource:"), 2, 0)
        self.scope_resource_combo = QComboBox()
        self.scope_resource_combo.setMinimumWidth(430)
        grid.addWidget(self.scope_resource_combo, 2, 1)

        self.scope_refresh_resources_btn = QPushButton("Refresh VISA devices")
        self.scope_refresh_resources_btn.clicked.connect(self.refresh_scope_resources)
        grid.addWidget(self.scope_refresh_resources_btn, 2, 2)

        self.scope_connect_btn = QPushButton("Connect oscilloscope")
        self.scope_connect_btn.clicked.connect(self.toggle_scope_connection)
        grid.addWidget(self.scope_connect_btn, 2, 3)

        self.scope_idn_label = QLabel("Instrument: disconnected")
        self.scope_idn_label.setWordWrap(True)
        grid.addWidget(self.scope_idn_label, 3, 0, 1, 4)

        self.scope_status_label = QLabel("Oscilloscope: disconnected")
        self.scope_status_label.setWordWrap(True)
        grid.addWidget(self.scope_status_label, 4, 0, 1, 4)
        return box

    def build_motor_connection_box(self) -> QGroupBox:
        box = QGroupBox("Motor controller connection")
        grid = QGridLayout(box)
        grid.addWidget(QLabel("Arduino Mega:"), 0, 0)
        self.motor_port_combo = QComboBox()
        self.motor_port_combo.setMinimumWidth(300)
        grid.addWidget(self.motor_port_combo, 0, 1)

        self.motor_connect_btn = QPushButton("Connect motor")
        self.motor_connect_btn.clicked.connect(self.toggle_motor_connection)
        grid.addWidget(self.motor_connect_btn, 0, 2)

        refresh = QPushButton("Refresh COM ports")
        refresh.clicked.connect(self.refresh_ports)
        grid.addWidget(refresh, 0, 3)

        self.motor_connection_label = QLabel("Motor: disconnected")
        grid.addWidget(self.motor_connection_label, 1, 0, 1, 4)
        return box

    def build_force_box(self) -> QGroupBox:
        box = QGroupBox("Force - SingleTact CSU15-45N")
        layout = QVBoxLayout(box)

        self.force_plot = pg.PlotWidget()
        self.force_plot.setBackground(None)
        self.force_plot.showGrid(x=True, y=True, alpha=0.25)
        self.force_plot.setLabel("bottom", "Live time since last reset", units="s")
        self.force_plot.setLabel("left", "Force", units="N")
        self.force_curve = self.force_plot.plot([], [], pen=pg.mkPen(width=1.5))
        # PlotItem downsamples for the screen ONLY; full-force samples stay
        # in memory for recording and CSV export.
        self.force_curve.setClipToView(True)
        self.force_curve.setDownsampling(auto=True, method="peak")
        self.force_plot.setMouseEnabled(x=True, y=True)
        self.force_plot.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=False)
        self.force_plot.setYRange(-1.0, 45.0, padding=0.02)
        self.force_plot.getViewBox().sigRangeChangedManually.connect(
            self._on_force_plot_manually_changed
        )
        layout.addWidget(self.force_plot, 1)

        follow_row = QHBoxLayout()
        self.force_follow_button = QPushButton("FOLLOW LIVE")
        self.force_follow_button.setToolTip(
            "Resume scrolling with incoming data after manual mouse zoom/pan"
        )
        self.force_follow_button.clicked.connect(self.resume_force_follow)
        follow_row.addWidget(self.force_follow_button)
        self.force_zoom_status = QLabel("Auto follow: ON | Wheel: zoom | Drag: pan")
        follow_row.addWidget(self.force_zoom_status, 1)
        layout.addLayout(follow_row)

        self.force_metrics = MetricLabels("N")
        layout.addWidget(self.force_metrics)

        row = QHBoxLayout()
        self.force_current_label = QLabel("Live force: -- N")
        self.force_current_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        row.addWidget(self.force_current_label)

        self.force_raw_label = QLabel("Raw: -- counts")
        row.addWidget(self.force_raw_label)

        self.force_baseline_label = QLabel(f"Baseline: {FORCE_FACTORY_BASELINE:.1f} counts")
        row.addWidget(self.force_baseline_label)
        row.addStretch(1)

        self.force_tare_btn = QPushButton("TARE FORCE")
        self.force_tare_btn.clicked.connect(self.tare_force)
        row.addWidget(self.force_tare_btn)

        self.force_reset_zero_btn = QPushButton("RESET FACTORY ZERO")
        self.force_reset_zero_btn.clicked.connect(self.reset_force_zero)
        row.addWidget(self.force_reset_zero_btn)
        layout.addLayout(row)

        note = QLabel(
            "Force graph remains live at all times while connected. START and STOP both "
            "reset the plot; only samples between START and STOP are stored in RAM."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        return box

    def build_motor_box(self) -> QGroupBox:
        box = QGroupBox("Motor control - FAULHABER + L298N + IE2-512")
        grid = QGridLayout(box)

        grid.addWidget(QLabel("Speed command (max 5%):"), 0, 0)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(0, 5)
        self.speed_slider.setValue(5)
        self.speed_slider.valueChanged.connect(self.on_speed_changed)
        grid.addWidget(self.speed_slider, 0, 1, 1, 3)

        self.speed_label = QLabel("5 %")
        grid.addWidget(self.speed_label, 0, 4)

        self.up_btn = QPushButton("UP")
        self.down_btn = QPushButton("DOWN")
        self.up_btn.setMinimumHeight(46)
        self.down_btn.setMinimumHeight(46)
        self.up_btn.pressed.connect(lambda: self.start_motor_motion("UP"))
        self.up_btn.released.connect(self.finish_motor_motion)
        self.down_btn.pressed.connect(lambda: self.start_motor_motion("DOWN"))
        self.down_btn.released.connect(self.finish_motor_motion)
        grid.addWidget(self.up_btn, 1, 0, 1, 2)
        grid.addWidget(self.down_btn, 1, 2, 1, 2)

        self.brake_btn = QPushButton("BRAKE MOTOR")
        self.brake_btn.clicked.connect(self.brake_motor)
        grid.addWidget(self.brake_btn, 1, 4)

        self.position_label = QLabel("Relative position: 0 counts")
        self.position_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        grid.addWidget(self.position_label, 2, 0, 1, 2)

        self.motor_state_label = QLabel("Motor state: UNKNOWN")
        self.motor_state_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        grid.addWidget(self.motor_state_label, 2, 2, 1, 2)

        self.position_brake_btn = QPushButton("POSITION BRAKE")
        self.position_brake_btn.clicked.connect(self.toggle_position_brake)
        grid.addWidget(self.position_brake_btn, 2, 4)

        self.position_brake_target_label = QLabel("Position brake target: --")
        grid.addWidget(self.position_brake_target_label, 3, 0, 1, 2)

        self.lock_tolerance_spin = QSpinBox()
        self.lock_tolerance_spin.setRange(0, 1000)
        self.lock_tolerance_spin.setValue(2)
        self.lock_tolerance_spin.setSuffix(" counts")
        self.lock_tolerance_spin.valueChanged.connect(self.send_lock_tolerance)
        form = QFormLayout()
        form.addRow("Position brake tolerance:", self.lock_tolerance_spin)
        grid.addLayout(form, 3, 2, 1, 2)

        self.estop_btn = QPushButton("EMERGENCY STOP")
        self.estop_btn.setObjectName("estop")
        self.estop_btn.clicked.connect(self.emergency_stop)
        grid.addWidget(self.estop_btn, 4, 0, 1, 2)

        self.reset_estop_btn = QPushButton("RESET E-STOP")
        self.reset_estop_btn.clicked.connect(self.reset_estop)
        grid.addWidget(self.reset_estop_btn, 4, 2, 1, 2)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        return box


    def build_sampling_box(self) -> QGroupBox:
        box = QGroupBox("Continuous live plots / Record in RAM / Save As")
        grid = QGridLayout(box)

        self.start_btn = QPushButton("START SAMPLING")
        self.start_btn.setMinimumHeight(42)
        self.start_btn.clicked.connect(self.start_acquisition)
        grid.addWidget(self.start_btn, 0, 0)

        self.stop_btn = QPushButton("STOP SAMPLING")
        self.stop_btn.setMinimumHeight(42)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_acquisition)
        grid.addWidget(self.stop_btn, 0, 1)

        self.save_as_btn = QPushButton("SAVE AS...")
        self.save_as_btn.setMinimumHeight(42)
        self.save_as_btn.setEnabled(False)
        self.save_as_btn.clicked.connect(self.save_as_session)
        grid.addWidget(self.save_as_btn, 0, 2)

        self.window_spin = QSpinBox()
        self.window_spin.setRange(5, 600)
        self.window_spin.setValue(60)
        self.window_spin.setSuffix(" s")
        self.window_spin.valueChanged.connect(self.on_window_changed)
        grid.addWidget(QLabel("Force plot window:"), 0, 3)
        grid.addWidget(self.window_spin, 0, 4)

        self.scope_sampling_interval_spin = QSpinBox()
        self.scope_sampling_interval_spin.setRange(100, 5000)
        self.scope_sampling_interval_spin.setValue(1000)
        self.scope_sampling_interval_spin.setSuffix(" ms")
        self.scope_sampling_interval_spin.valueChanged.connect(self.change_scope_waveform_interval)
        grid.addWidget(QLabel("Scope waveform interval:"), 0, 5)
        grid.addWidget(self.scope_sampling_interval_spin, 0, 6)

        self.csv_status_label = QLabel("No recording stored. Live plots run while devices are connected.")
        self.csv_status_label.setWordWrap(True)
        self.sample_status_label = QLabel("Ready | Recorded force: 0 | Recorded scope: 0")
        grid.addWidget(self.csv_status_label, 1, 0, 1, 4)
        grid.addWidget(self.sample_status_label, 1, 4, 1, 3)
        return box

    def build_scope_controls_box(self) -> QGroupBox:
        control_box = QGroupBox("Oscilloscope controls - Tektronix MSO4104B")
        control_grid = QGridLayout(control_box)

        self.scope_channel_combo = QComboBox()
        self.scope_channel_combo.addItems(["CH1", "CH2", "CH3", "CH4"])
        control_grid.addWidget(QLabel("Channel:"), 0, 0)
        control_grid.addWidget(self.scope_channel_combo, 0, 1)

        self.scope_coupling_combo = QComboBox()
        self.scope_coupling_combo.addItems(["DC", "AC", "GND"])
        control_grid.addWidget(QLabel("Coupling:"), 0, 2)
        control_grid.addWidget(self.scope_coupling_combo, 0, 3)

        self.scope_vertical_scale_combo = QComboBox()
        for label, value in [
            ("1 mV/div", 0.001), ("2 mV/div", 0.002), ("5 mV/div", 0.005),
            ("10 mV/div", 0.010), ("20 mV/div", 0.020), ("50 mV/div", 0.050),
            ("100 mV/div", 0.100), ("200 mV/div", 0.200), ("500 mV/div", 0.500),
            ("1 V/div", 1.0), ("2 V/div", 2.0), ("5 V/div", 5.0), ("10 V/div", 10.0),
        ]:
            self.scope_vertical_scale_combo.addItem(label, value)
        self.scope_vertical_scale_combo.setCurrentIndex(6)
        control_grid.addWidget(QLabel("V/div:"), 0, 4)
        control_grid.addWidget(self.scope_vertical_scale_combo, 0, 5)

        self.scope_vertical_position_spin = QDoubleSpinBox()
        self.scope_vertical_position_spin.setRange(-8.0, 8.0)
        self.scope_vertical_position_spin.setDecimals(2)
        self.scope_vertical_position_spin.setSingleStep(0.1)
        self.scope_vertical_position_spin.setSuffix(" div")
        control_grid.addWidget(QLabel("V position:"), 1, 0)
        control_grid.addWidget(self.scope_vertical_position_spin, 1, 1)

        self.scope_apply_vertical_btn = QPushButton("APPLY VERTICAL")
        self.scope_apply_vertical_btn.clicked.connect(self.apply_scope_vertical)
        control_grid.addWidget(self.scope_apply_vertical_btn, 1, 2)

        self.scope_time_scale_combo = QComboBox()
        for label, value in [
            ("1 ns/div", 1e-9), ("2 ns/div", 2e-9), ("5 ns/div", 5e-9),
            ("10 ns/div", 10e-9), ("20 ns/div", 20e-9), ("50 ns/div", 50e-9),
            ("100 ns/div", 100e-9), ("200 ns/div", 200e-9), ("500 ns/div", 500e-9),
            ("1 us/div", 1e-6), ("2 us/div", 2e-6), ("5 us/div", 5e-6),
            ("10 us/div", 10e-6), ("20 us/div", 20e-6), ("50 us/div", 50e-6),
            ("100 us/div", 100e-6), ("200 us/div", 200e-6), ("500 us/div", 500e-6),
            ("1 ms/div", 1e-3), ("2 ms/div", 2e-3), ("5 ms/div", 5e-3),
            ("10 ms/div", 10e-3), ("20 ms/div", 20e-3), ("50 ms/div", 50e-3),
            ("100 ms/div", 100e-3), ("200 ms/div", 200e-3), ("500 ms/div", 500e-3),
            ("1 s/div", 1.0), ("2 s/div", 2.0), ("5 s/div", 5.0),
            ("10 s/div", 10.0), ("15 s/div", 15.0), ("20 s/div", 20.0),
            ("30 s/div", 30.0), ("40 s/div", 40.0), ("45 s/div", 45.0),
            ("50 s/div", 50.0), ("1 min/div", 60.0),
            ("1.5 min/div", 90.0), ("2 min/div", 120.0),
            ("2.5 min/div", 150.0), ("3 min/div", 180.0),
            ("3.5 min/div", 210.0), ("4 min/div", 240.0),
        ]:
            self.scope_time_scale_combo.addItem(label, value)
        self.scope_time_scale_combo.setCurrentIndex(20)
        control_grid.addWidget(QLabel("Time/div:"), 1, 3)
        control_grid.addWidget(self.scope_time_scale_combo, 1, 4)
        self.scope_apply_horizontal_btn = QPushButton("APPLY TIMEBASE")
        self.scope_apply_horizontal_btn.clicked.connect(self.apply_scope_horizontal)
        control_grid.addWidget(self.scope_apply_horizontal_btn, 1, 5)

        self.scope_trigger_source_combo = QComboBox()
        self.scope_trigger_source_combo.addItems(["CH1", "CH2", "CH3", "CH4"])
        self.scope_trigger_slope_combo = QComboBox()
        self.scope_trigger_slope_combo.addItems(["RISE", "FALL"])
        self.scope_trigger_level_spin = QDoubleSpinBox()
        self.scope_trigger_level_spin.setRange(-100.0, 100.0)
        self.scope_trigger_level_spin.setDecimals(4)
        self.scope_trigger_level_spin.setSingleStep(0.01)
        self.scope_trigger_level_spin.setSuffix(" V")
        control_grid.addWidget(QLabel("Trigger source:"), 2, 0)
        control_grid.addWidget(self.scope_trigger_source_combo, 2, 1)
        control_grid.addWidget(QLabel("Slope:"), 2, 2)
        control_grid.addWidget(self.scope_trigger_slope_combo, 2, 3)
        control_grid.addWidget(QLabel("Level:"), 2, 4)
        control_grid.addWidget(self.scope_trigger_level_spin, 2, 5)

        command_row = QHBoxLayout()
        self.scope_apply_trigger_btn = QPushButton("APPLY TRIGGER")
        self.scope_apply_trigger_btn.clicked.connect(self.apply_scope_trigger)
        self.scope_run_btn = QPushButton("RUN")
        self.scope_run_btn.clicked.connect(self.scope_run)
        self.scope_stop_btn = QPushButton("STOP")
        self.scope_stop_btn.clicked.connect(self.scope_stop)
        self.scope_single_btn = QPushButton("SINGLE")
        self.scope_single_btn.clicked.connect(self.scope_single)
        self.scope_reset_btn = QPushButton("RESET SCOPE")
        self.scope_reset_btn.clicked.connect(self.scope_reset)
        self.scope_autoset_btn = QPushButton("AUTOSET")
        self.scope_autoset_btn.clicked.connect(self.scope_autoset)
        self.scope_refresh_settings_btn = QPushButton("READ SETTINGS")
        self.scope_refresh_settings_btn.clicked.connect(self.sync_scope_controls_from_instrument)
        self.scope_refresh_screen_btn = QPushButton("REFRESH SCREEN")
        self.scope_refresh_screen_btn.clicked.connect(self.refresh_scope_screen)
        for button in [
            self.scope_apply_trigger_btn, self.scope_run_btn, self.scope_stop_btn,
            self.scope_single_btn, self.scope_reset_btn, self.scope_autoset_btn,
            self.scope_refresh_settings_btn, self.scope_refresh_screen_btn,
        ]:
            command_row.addWidget(button)
        control_grid.addLayout(command_row, 3, 0, 1, 6)

        live_row = QHBoxLayout()
        self.scope_live_check = QCheckBox("AUTO REFRESH SCOPE SCREEN")
        self.scope_live_check.setChecked(True)
        self.scope_live_check.toggled.connect(self.toggle_scope_live)
        live_row.addWidget(self.scope_live_check)
        self.scope_live_interval_spin = QSpinBox()
        # 50 ms is only the fastest request cadence, not a guaranteed 20 FPS.
        self.scope_live_interval_spin.setRange(50, 5000)
        self.scope_live_interval_spin.setValue(50)
        self.scope_live_interval_spin.setSuffix(" ms")
        self.scope_live_interval_spin.valueChanged.connect(self.change_scope_live_interval)
        live_row.addWidget(QLabel("Screen refresh:"))
        live_row.addWidget(self.scope_live_interval_spin)
        live_row.addStretch(1)
        control_grid.addLayout(live_row, 4, 0, 1, 6)
        return control_box


    def build_scope_display_box(self) -> QGroupBox:
        box = QGroupBox("Tektronix MSO4104B - Exact LCD screen (USB)")
        root = QVBoxLayout(box)

        self.scope_screen_view = ScopeScreenView()
        self.scope_screen_view.setMinimumSize(400, 260)
        root.addWidget(self.scope_screen_view, 1)

        controls = QHBoxLayout()
        self.scope_fit_btn = QPushButton("FIT SCREEN")
        self.scope_fit_btn.clicked.connect(self.scope_screen_view.fit_screen)
        controls.addWidget(self.scope_fit_btn)
        self.scope_screen_info = QLabel(
            "Waiting for scope image | Wheel: zoom | Drag: pan"
        )
        self.scope_screen_info.setWordWrap(True)
        controls.addWidget(self.scope_screen_info, 1)
        root.addLayout(controls)

        # Waveform data are still read for live measurements and saved to their
        # own oscilloscope CSV. No second, reconstructed waveform figure is shown.
        self.scope_measurement_label = QLabel("Numerical waveform: waiting for USB data...")
        self.scope_measurement_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        self.scope_measurement_label.setWordWrap(True)
        root.addWidget(self.scope_measurement_label)
        return box

    # ==================================================================
    # Styling
    # ==================================================================
    def apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #1e1f22;
                color: #e8e8e8;
            }
            QGroupBox {
                border: 1px solid #555;
                border-radius: 6px;
                margin-top: 10px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QPushButton {
                background: #34363b;
                border: 1px solid #666;
                border-radius: 5px;
                padding: 7px 12px;
            }
            QPushButton:hover { background: #454850; }
            QPushButton:pressed { background: #2a2c30; }
            QPushButton#estop {
                background: #8a1f1f;
                border: 1px solid #cc5555;
                font-weight: bold;
            }
            QLabel#status {
                padding: 5px;
                font-weight: bold;
            }
            QTabWidget::pane {
                border: 1px solid #555;
            }
            QTabBar::tab {
                background: #2b2d31;
                padding: 9px 18px;
            }
            QTabBar::tab:selected {
                background: #3a3d43;
            }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #2a2c30;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 4px;
            }
            """
        )

    # ==================================================================
    # COM ports
    # ==================================================================
    def refresh_ports(self) -> None:
        motor_selected = self.motor_port_combo.currentData() if self.motor_port_combo.count() else None
        force_selected = self.force_port_combo.currentData() if self.force_port_combo.count() else None

        infos = QSerialPortInfo.availablePorts()
        entries: list[tuple[str, str]] = []
        for info in infos:
            label = info.portName()
            if info.description():
                label += f" - {info.description()}"
            entries.append((label, info.portName()))

        for combo, previous in [
            (self.motor_port_combo, motor_selected),
            (self.force_port_combo, force_selected),
        ]:
            combo.clear()
            for label, port_name in entries:
                combo.addItem(label, port_name)
            if previous:
                idx = combo.findData(previous)
                if idx >= 0:
                    combo.setCurrentIndex(idx)

    # ==================================================================
    # Motor connection / protocol - dedicated worker thread
    # ==================================================================
    def toggle_motor_connection(self) -> None:
        if self.motor_worker is not None and self.motor_worker.isRunning():
            self.motor_worker.request_stop(brake_on_exit=not self.estop_active)
            self.motor_connect_btn.setEnabled(False)
            return

        port = self.motor_port_combo.currentData()
        if not port:
            QMessageBox.warning(self, APP_TITLE, "Select the Arduino COM port.")
            return

        worker = MotorWorker(str(port), self)
        worker.connection_changed.connect(self.on_motor_connection_changed)
        worker.line_received.connect(self.parse_motor_line)
        worker.error.connect(self.on_motor_error)
        worker.finished.connect(self.on_motor_worker_finished)
        self.motor_worker = worker
        self.motor_connect_btn.setEnabled(False)
        self.motor_connection_label.setText(f"Motor: connecting to {port} ...")
        worker.start()

    def on_motor_connection_changed(self, connected: bool, message: str) -> None:
        self.motor_connected = connected
        self.motor_connection_label.setText(message)
        self.motor_connect_btn.setEnabled(True)
        self.motor_connect_btn.setText("Disconnect motor" if connected else "Connect motor")
        if connected:
            self.send_motor_command(f"SET_LOCK_TOL,{self.lock_tolerance_spin.value()}")
            self.send_motor_command("GET_STATUS")

    def on_motor_worker_finished(self) -> None:
        self.motor_connected = False
        self.motor_connect_btn.setEnabled(True)
        self.motor_connect_btn.setText("Connect motor")
        self.motor_connection_label.setText("Motor: disconnected")
        if self.motor_worker is not None and not self.motor_worker.isRunning():
            self.motor_worker.deleteLater()
            self.motor_worker = None

    def on_motor_error(self, message: str) -> None:
        self.status_message(message)

    def send_motor_command(self, command: str) -> None:
        if self.motor_worker is None or not self.motor_worker.isRunning() or not self.motor_connected:
            self.status_message("Motor command not sent: Arduino is not connected")
            return
        self.motor_worker.send_command(command)

    def parse_motor_line(self, line: str) -> None:
        parts = [p.strip() for p in line.split(",")]
        if not parts:
            return

        if parts[0] == "MOTOR_DATA" and len(parts) >= 4:
            try:
                self.motor_position = int(parts[2])
            except ValueError:
                return
            self.motor_state = parts[3]
            self.estop_active = self.motor_state == "ESTOP"
            self.position_label.setText(f"Relative position: {self.motor_position} counts")
            self.motor_state_label.setText(f"Motor state: {self.motor_state}")
            return

        if parts[0] == "STATUS":
            code = parts[1] if len(parts) > 1 else ""

            if code == "POSITION_BRAKE_ON":
                self.position_brake_active = True
                self.position_brake_btn.setText("RELEASE POSITION BRAKE")
                if len(parts) > 2:
                    try:
                        self.position_brake_target = int(parts[2])
                    except ValueError:
                        self.position_brake_target = None
                self.update_position_brake_target_label()

            elif code in {"POSITION_BRAKE_OFF", "MOTOR_BRAKED"}:
                self.position_brake_active = False
                self.position_brake_target = None
                self.position_brake_btn.setText("POSITION BRAKE")
                self.update_position_brake_target_label()

            elif code == "EMERGENCY_STOP":
                self.estop_active = True
                self.position_brake_active = False
                self.position_brake_btn.setText("POSITION BRAKE")
                self.position_brake_target = None
                self.update_position_brake_target_label()

            elif code == "ESTOP_RESET":
                self.estop_active = False
                self.position_brake_active = False
                self.position_brake_target = None
                self.position_brake_btn.setText("POSITION BRAKE")
                self.update_position_brake_target_label()

            self.status_message("Motor: " + ", ".join(parts[1:]))

        elif parts[0] == "ERROR":
            self.status_message("Motor error: " + ", ".join(parts[1:]))

    # ==================================================================
    # Direct SingleTact USB acquisition - dedicated worker thread
    # ==================================================================
    def toggle_force_connection(self) -> None:
        if self.force_worker is not None and self.force_worker.isRunning():
            self.force_worker.request_stop()
            self.force_connect_btn.setEnabled(False)
            return

        port = self.force_port_combo.currentData()
        if not port:
            QMessageBox.warning(self, APP_TITLE, "Select the SingleTact USB COM port.")
            return

        self.force_sample_counter = 0
        self.force_timeout_counter = 0
        self.time_data.clear()
        self.force_data.clear()
        self.force_curve.setData([], [])
        self.force_metrics.update_values([])

        worker = ForceWorker(str(port), self)
        worker.connection_changed.connect(self.on_force_connection_changed)
        worker.sample_received.connect(self.on_force_sample)
        worker.timeout_changed.connect(self.on_force_timeout)
        worker.error.connect(self.on_force_error)
        worker.finished.connect(self.on_force_worker_finished)
        self.force_worker = worker
        self.force_connect_btn.setEnabled(False)
        self.force_connection_label.setText(f"Force: initializing {port} ...")
        self.status_message("Initializing SingleTact USB in dedicated acquisition thread...")
        worker.start()

    def on_force_connection_changed(self, connected: bool, message: str) -> None:
        self.force_connected = connected
        self.force_connection_label.setText(message)
        self.force_connect_btn.setEnabled(True)
        self.force_connect_btn.setText("Disconnect force sensor" if connected else "Connect force sensor")
        if connected:
            self.status_message("SingleTact USB initialized; threaded acquisition started")

    def on_force_worker_finished(self) -> None:
        self.force_connected = False
        self.force_connect_btn.setEnabled(True)
        self.force_connect_btn.setText("Connect force sensor")
        self.force_connection_label.setText("Force: disconnected")
        if self.force_worker is not None and not self.force_worker.isRunning():
            self.force_worker.deleteLater()
            self.force_worker = None

    def on_force_error(self, message: str) -> None:
        self.status_message(message)
        self.force_connection_label.setText(message)

    def on_force_timeout(self, count: int) -> None:
        self.force_timeout_counter = count
        self.update_sample_status()
        if count == 10:
            self.status_message(
                "SingleTact is not replying. Check the selected COM port and close the official SingleTact app."
            )


    def on_force_sample(self, raw_counts: int, iteration: int, sample_time: float) -> None:
        force_n = (raw_counts - self.force_baseline_counts) * FORCE_RATING_N / FORCE_COUNTS_FULL_SCALE
        self.latest_force_raw = raw_counts
        self.latest_force_n = force_n
        self.force_raw_recent.append(raw_counts)
        self.force_sample_counter += 1
        self.force_current_label.setText(f"Live force: {force_n:.4f} N")
        self.force_raw_label.setText(f"Raw: {raw_counts} counts")

        # Live view is always independent of the START/STOP recording state.
        if sample_time >= self.plot_origin_t0:
            self._force_plot_dirty = True
            t_plot = sample_time - self.plot_origin_t0
            self.time_data.append(t_plot)
            self.force_data.append(force_n)
            cutoff = t_plot - self.window_seconds
            while self.time_data and self.time_data[0] < cutoff:
                self.time_data.popleft()
                self.force_data.popleft()

        if self.acquiring and sample_time >= self.acquisition_t0:
            t = sample_time - self.acquisition_t0
            self.session_force_rows.append((f"{t:.6f}", raw_counts, f"{force_n:.8g}",
                                            self.motor_position, self.motor_state))
            self.session_force_samples += 1
            self.recording_unsaved = True
            self.update_sample_status()

    def _reset_live_figures(self) -> None:
        # Each button press starts a new visible live window, but does not
        # touch the separate RAM recording buffers.
        self.plot_origin_t0 = time.perf_counter()
        self.time_data.clear()
        self.force_data.clear()
        self.force_curve.setData([], [])
        self.force_metrics.update_values([])
        # START/STOP is an intentional reset, including zoom/follow state.
        self.resume_force_follow()
        self.force_plot.setXRange(0, self.window_seconds, padding=0)
        self._force_plot_dirty = True
        self.scope_last_x = []
        self.scope_last_y = []
        self.scope_measurement_label.setText("Numeric data: waiting for next live capture...")
        self.scope_screen_view.clear_screen()
        self.scope_screen_info.setText("Waiting for a fresh hardware screenshot...")
        if self.scope_connected:
            self.refresh_scope_screen()

    def update_sample_status(self) -> None:
        state = "RECORDING" if self.acquiring else "LIVE (not recording)"
        self.sample_status_label.setText(
            f"{state} | Recorded force: {self.session_force_samples} | "
            f"Recorded scope: {self.session_scope_captures}"
        )

    def tare_force(self) -> None:
        if self.acquiring:
            QMessageBox.warning(self, APP_TITLE, "STOP SAMPLING before changing the force baseline.")
            return
        if len(self.force_raw_recent) < 10:
            QMessageBox.warning(self, APP_TITLE, "Wait for ten valid unloaded force samples, then retry TARE FORCE.")
            return
        recent = list(self.force_raw_recent)[-20:]
        self.force_baseline_counts = sum(recent) / len(recent)
        self.force_baseline_label.setText(f"Baseline: {self.force_baseline_counts:.2f} counts")
        self._reset_live_figures()
        self.status_message("Force tare applied; live plots restarted. RAM recording remains unchanged.")

    def reset_force_zero(self) -> None:
        if self.acquiring:
            QMessageBox.warning(self, APP_TITLE, "STOP SAMPLING before resetting force zero.")
            return
        self.force_baseline_counts = FORCE_FACTORY_BASELINE
        self.force_baseline_label.setText(f"Baseline: {self.force_baseline_counts:.1f} counts")
        self._reset_live_figures()
        self.status_message("Factory baseline restored; live plots restarted.")

    # ==================================================================
    # Motor controls
    # ==================================================================
    def on_speed_changed(self, value: int) -> None:
        value = max(0, min(5, value))
        self.speed_label.setText(f"{value} %")

    def start_motor_motion(self, direction: str) -> None:
        if not self.motor_connected:
            QMessageBox.warning(self, APP_TITLE, "Connect the Arduino motor controller first.")
            return

        if self.estop_active:
            self.status_message("Reset E-STOP before moving the motor")
            return

        speed = max(0, min(5, self.speed_slider.value()))
        if speed <= 0:
            self.status_message("Motor speed is 0%")
            return

        # Manual motion always cancels POSITION BRAKE.
        self.position_brake_active = False
        self.position_brake_target = None
        self.position_brake_btn.setText("POSITION BRAKE")
        self.update_position_brake_target_label()

        self.send_motor_command(f"MOTOR_{direction},{speed}")

    def finish_motor_motion(self) -> None:
        if not self.motor_connected or self.estop_active:
            return

        # Release means dynamic brake only. POSITION BRAKE is not re-enabled.
        self.send_motor_command("MOTOR_BRAKE")

    def brake_motor(self) -> None:
        if not self.motor_connected:
            return
        self.position_brake_active = False
        self.position_brake_target = None
        self.position_brake_btn.setText("POSITION BRAKE")
        self.update_position_brake_target_label()
        self.send_motor_command("MOTOR_BRAKE")

    def toggle_position_brake(self) -> None:
        if not self.motor_connected:
            QMessageBox.warning(self, APP_TITLE, "Connect the Arduino motor controller first.")
            return

        if self.estop_active:
            self.status_message("Reset E-STOP before enabling POSITION BRAKE")
            return

        if self.position_brake_active:
            self.send_motor_command("MOTOR_POSITION_RELEASE")
        else:
            tol = self.lock_tolerance_spin.value()
            self.send_motor_command(f"MOTOR_POSITION_BRAKE,{tol}")

    def send_lock_tolerance(self, value: int) -> None:
        if self.motor_connected:
            self.send_motor_command(f"SET_LOCK_TOL,{value}")

    def emergency_stop(self) -> None:
        if self.motor_connected:
            self.send_motor_command("ESTOP")
        self.estop_active = True
        self.position_brake_active = False
        self.position_brake_target = None
        self.position_brake_btn.setText("POSITION BRAKE")
        self.update_position_brake_target_label()
        self.status_message("EMERGENCY STOP activated")

    def reset_estop(self) -> None:
        if not self.motor_connected:
            QMessageBox.warning(self, APP_TITLE, "Connect the Arduino motor controller first.")
            return
        self.send_motor_command("RESET_ESTOP")
        self.estop_active = False

    def update_position_brake_target_label(self) -> None:
        if self.position_brake_target is None:
            self.position_brake_target_label.setText("Position brake target: --")
        else:
            self.position_brake_target_label.setText(
                f"Position brake target: {self.position_brake_target} counts"
            )

    # ==================================================================
    # Tektronix MSO4104B USB / VISA - dedicated worker thread
    # ==================================================================
    def refresh_scope_resources(self) -> None:
        self.scope_resource_combo.clear()
        self.scope_resource_combo.addItem("Searching VISA instruments...", None)
        self.scope_status_label.setText("Oscilloscope: searching VISA resources in scope thread...")
        self.scope_worker.request("refresh_resources")

    def on_scope_resources_ready(self, resources) -> None:
        previous = self.scope_resource_name
        self.scope_resource_combo.clear()
        if not resources:
            self.scope_resource_combo.addItem("No VISA instruments found", None)
            self.scope_status_label.setText(
                "Oscilloscope: no VISA instruments found. Check USB Device connection and VISA driver."
            )
            return
        for resource in resources:
            self.scope_resource_combo.addItem(str(resource), str(resource))
        if previous:
            idx = self.scope_resource_combo.findData(previous)
            if idx >= 0:
                self.scope_resource_combo.setCurrentIndex(idx)
        self.scope_status_label.setText(
            f"Oscilloscope: found {len(resources)} VISA resource(s); select the MSO4104B USB resource."
        )

    def toggle_scope_connection(self) -> None:
        if self.scope_connected:
            self.scope_live_timer.stop()
            self.scope_connect_btn.setEnabled(False)
            self.scope_worker.request("disconnect")
            return

        resource = self.scope_resource_combo.currentData()
        if not resource:
            QMessageBox.warning(self, APP_TITLE, "Select the oscilloscope VISA USB resource first.")
            return
        self.scope_connect_btn.setEnabled(False)
        self.scope_status_label.setText(f"Oscilloscope: connecting to {resource} in dedicated thread...")
        self.scope_worker.request("connect", str(resource))

    def on_scope_connected(self, idn: str, resource: str) -> None:
        self.scope_connected = True
        self.scope_resource_name = resource
        self.scope_connect_btn.setEnabled(True)
        self.scope_connect_btn.setText("Disconnect oscilloscope")
        self.scope_idn_label.setText(f"Instrument: {idn}")
        self.scope_status_label.setText(f"Oscilloscope: connected through USB VISA ({resource})")
        self.scope_worker.request("live_interval", self.scope_sampling_interval_spin.value() / 1000.0)
        self.sync_scope_controls_from_instrument()
        self.refresh_scope_screen()
        if self.scope_live_check.isChecked():
            self.scope_live_timer.start()

    def on_scope_disconnected(self) -> None:
        self.scope_connected = False
        self.scope_resource_name = None
        self.scope_connect_btn.setEnabled(True)
        self.scope_connect_btn.setText("Connect oscilloscope")
        self.scope_idn_label.setText("Instrument: disconnected")
        self.scope_status_label.setText("Oscilloscope: disconnected")
        self.scope_live_timer.stop()

    def on_scope_status(self, message: str) -> None:
        self.scope_status_label.setText(message)

    def on_scope_error(self, message: str) -> None:
        self.scope_status_label.setText(message)

    def require_scope(self) -> bool:
        if not self.scope_connected:
            QMessageBox.warning(self, APP_TITLE, "Connect the MSO4104B in the Settings tab first.")
            return False
        return True

    @staticmethod
    def _set_combo_to_nearest_data(combo: QComboBox, value: float) -> None:
        if combo.count() == 0:
            return
        best_index = 0
        best_error = float("inf")
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data is None:
                continue
            error = abs(float(data) - value)
            if error < best_error:
                best_error = error
                best_index = i
        combo.setCurrentIndex(best_index)

    def sync_scope_controls_from_instrument(self) -> None:
        if not self.scope_connected:
            return
        self.scope_worker.request("sync", self.scope_channel_combo.currentText())

    def on_scope_settings_ready(self, data: dict) -> None:
        try:
            self._set_combo_to_nearest_data(self.scope_vertical_scale_combo, float(data["vertical_scale"]))
            self.scope_vertical_position_spin.setValue(float(data["vertical_position"]))
            self._set_combo_to_nearest_data(self.scope_time_scale_combo, float(data["time_scale"]))

            coupling = str(data.get("coupling", "")).upper()
            idx = self.scope_coupling_combo.findText(coupling)
            if idx >= 0:
                self.scope_coupling_combo.setCurrentIndex(idx)

            source = str(data.get("trigger_source", "")).upper()
            slope = str(data.get("trigger_slope", "")).upper()
            idx = self.scope_trigger_source_combo.findText(source)
            if idx >= 0:
                self.scope_trigger_source_combo.setCurrentIndex(idx)
            idx = self.scope_trigger_slope_combo.findText(slope)
            if idx >= 0:
                self.scope_trigger_slope_combo.setCurrentIndex(idx)
            if "trigger_level" in data:
                self.scope_trigger_level_spin.setValue(float(data["trigger_level"]))
            self.scope_status_label.setText("Oscilloscope: front-panel settings read successfully")
        except Exception as exc:
            self.scope_status_label.setText(f"Oscilloscope: could not apply returned settings: {exc}")

    def _queue_scope_commands(
        self,
        commands: list[str],
        label: str,
        *,
        waveform: bool = False,
        screen: bool = False,
        sync: bool = False,
    ) -> None:
        if not self.require_scope():
            return
        self.scope_worker.request(
            "commands",
            {
                "commands": commands,
                "label": label,
                "channel": self.scope_channel_combo.currentText(),
                "waveform": waveform,
                "screen": screen,
                "sync": sync,
            },
        )

    def apply_scope_vertical(self) -> None:
        channel = self.scope_channel_combo.currentText()
        scale = float(self.scope_vertical_scale_combo.currentData())
        coupling = self.scope_coupling_combo.currentText()
        position = self.scope_vertical_position_spin.value()
        self._queue_scope_commands(
            [
                f"SELECT:{channel} ON",
                f"{channel}:SCALE {scale}",
                f"{channel}:COUPLING {coupling}",
                f"{channel}:POSITION {position}",
            ],
            f"Oscilloscope: {channel} vertical settings applied",
            waveform=True,
            screen=True,
        )

    def apply_scope_horizontal(self) -> None:
        scale = float(self.scope_time_scale_combo.currentData())
        self._queue_scope_commands(
            [f"HORIZONTAL:MAIN:SCALE {scale}"],
            "Oscilloscope: timebase command sent (readback follows)",
            waveform=False,
            screen=True,
            sync=True,
        )

    def apply_scope_trigger(self) -> None:
        source = self.scope_trigger_source_combo.currentText()
        slope = self.scope_trigger_slope_combo.currentText()
        level = self.scope_trigger_level_spin.value()
        self._queue_scope_commands(
            [
                "TRIGGER:A:TYPE EDGE",
                f"TRIGGER:A:EDGE:SOURCE {source}",
                f"TRIGGER:A:EDGE:SLOPE {slope}",
                f"TRIGGER:A:LEVEL:{source} {level}",
            ],
            "Oscilloscope: edge-trigger settings applied",
            screen=True,
        )

    def scope_run(self) -> None:
        self._queue_scope_commands(
            ["ACQUIRE:STOPAFTER RUNSTOP", "ACQUIRE:STATE RUN"],
            "Oscilloscope: RUN",
        )

    def scope_reset(self) -> None:
        if not self.require_scope():
            return
        self.scope_worker.request(
            "commands",
            {"commands": ["ACQUIRE:STATE STOP", "*RST", "*CLS"], "label": "Oscilloscope: reset requested"},
        )
        QTimer.singleShot(1200, self.sync_scope_controls_from_instrument)
        QTimer.singleShot(1800, self.refresh_scope_screen)

    def scope_stop(self) -> None:
        self._queue_scope_commands(
            ["ACQUIRE:STATE STOP"],
            "Oscilloscope: STOP",
            waveform=True,
            screen=True,
        )

    def scope_single(self) -> None:
        self._queue_scope_commands(
            ["ACQUIRE:STOPAFTER SEQUENCE", "ACQUIRE:STATE RUN"],
            "Oscilloscope: SINGLE acquisition armed",
        )

    def scope_autoset(self) -> None:
        if not self.require_scope():
            return
        self.scope_worker.request(
            "commands",
            {"commands": ["AUTOSET EXECUTE"], "label": "Oscilloscope: AUTOSET requested"},
        )
        QTimer.singleShot(1700, self.sync_scope_controls_from_instrument)
        QTimer.singleShot(2200, self.refresh_scope_screen)

    def change_scope_live_interval(self, value: int) -> None:
        self.scope_live_timer.setInterval(max(50, int(value)))

    def toggle_scope_live(self, enabled: bool) -> None:
        if enabled:
            if not self.scope_connected:
                self.scope_live_check.blockSignals(True)
                self.scope_live_check.setChecked(False)
                self.scope_live_check.blockSignals(False)
                QMessageBox.warning(self, APP_TITLE, "Connect the oscilloscope before enabling the live screen mirror.")
                return
            self.scope_live_timer.setInterval(max(50, self.scope_live_interval_spin.value()))
            self.scope_live_timer.start()
            self.scope_status_label.setText(
                "Oscilloscope: live USB screen mirroring enabled in dedicated scope thread"
            )
            self.refresh_scope_screen()
        else:
            self.scope_live_timer.stop()
            if self.scope_connected:
                self.scope_status_label.setText("Oscilloscope: live screen mirroring stopped")


    def _display_scope_screen_png(self, png_data: bytes) -> None:
        try:
            width, height = self.scope_screen_view.show_png(png_data)
        except ValueError as exc:
            self.scope_status_label.setText(f"Screen PNG error: {exc}")
            return
        self.scope_screen_info.setText(
            f"Real MSO4104B PNG: {width} x {height} px | "
            f"Received at {datetime.now().strftime('%H:%M:%S')} | "
            "Wheel: zoom; drag: pan; FIT SCREEN: reset"
        )

    def on_scope_screen_ready(self, png_data: bytes, capture_started: float) -> None:
        if capture_started < self.plot_origin_t0:
            return
        self._display_scope_screen_png(png_data)

    def refresh_scope_screen(self) -> None:
        if self.scope_connected:
            self.scope_worker.request("screen")

    def refresh_scope_waveform(self) -> None:
        if self.scope_connected:
            self.scope_worker.request("waveform", self.scope_channel_combo.currentText())


    def on_scope_waveform_ready(self, x_values, y_values, metrics: dict) -> None:
        captured_at = float(metrics.get("capture_monotonic_s", time.perf_counter()))
        # Ignore a response that was in flight before START or STOP cleared the figures.
        if captured_at < self.plot_origin_t0:
            return
        # Numeric samples feed measurements + CSV only. The image on the
        # screen is always the actual hardcopy PNG from the oscilloscope.
        self.scope_last_x = x_values if isinstance(x_values, list) else list(x_values)
        self.scope_last_y = y_values if isinstance(y_values, list) else list(y_values)

        # Use the timestamp taken in the VISA thread, not the time when
        # a queued Qt signal reaches the UI. This also retains captures that
        # began immediately after START while the SCPI RUN command is queued.
        recording_frame = self.acquiring and captured_at >= self.acquisition_t0
        time_text = (f"{captured_at - self.acquisition_t0:.3f}" if recording_frame
                     else f"{captured_at - self.plot_origin_t0:.3f} live")
        self.scope_measurement_label.setText(
            f"Waveform: {metrics['channel']}, {metrics['points']} points | "
            f"Capture t: {time_text} s | Min: {metrics['min']:.6g} V | "
            f"Max: {metrics['max']:.6g} V | Vpp: {metrics['vpp']:.6g} V | "
            f"Mean: {metrics['mean']:.6g} V | RMS: {metrics['rms']:.6g} V"
        )
        if recording_frame:
            # Store voltage values compactly; waveform times are reconstructed
            # from scope-provided XZERO, PT_OFF and XINCR (uniform sampling).
            x0 = float(self.scope_last_x[0]) if self.scope_last_x else 0.0
            dx = (float(self.scope_last_x[1]) - x0) if len(self.scope_last_x) > 1 else 0.0
            self.session_scope_records.append((
                captured_at - self.acquisition_t0,
                str(metrics['channel']), x0, dx, array('d', self.scope_last_y),
            ))
            self.session_scope_captures += 1
            self.recording_unsaved = True
            self.update_sample_status()

    def on_scope_sampling_state(self, active: bool, message: str) -> None:
        self.scope_sampling_active = active
        self.scope_status_label.setText(message)

    def on_csv_session_saved(self, session_dir: str, force_path: str, scope_path: str) -> None:
        self.saving_in_progress = False
        self.recording_unsaved = False
        self.start_btn.setEnabled(True)
        self.save_as_btn.setEnabled(bool(self.session_force_rows or self.session_scope_records))
        self.csv_status_label.setText(f"Saved separate force + oscilloscope CSV files to: {session_dir}")
        self.status_message(f"Save As completed: {force_path} and {scope_path}")

    def on_csv_error(self, message: str) -> None:
        self.saving_in_progress = False
        self.start_btn.setEnabled(True)
        self.save_as_btn.setEnabled(bool(self.session_force_rows or self.session_scope_records))
        self.csv_status_label.setText("Save As failed; all samples retained in memory. Please retry.")
        self.status_message(message)

    # ==================================================================
    # Synchronized acquisition / plotting / CSV
    # ==================================================================

    def start_acquisition(self) -> None:
        if self.acquiring or self.saving_in_progress:
            return
        if self.recording_unsaved:
            QMessageBox.warning(self, APP_TITLE,
                                "Previous recording is still in memory. Click SAVE AS before starting another one.")
            return
        if not self.force_connected or not self.scope_connected:
            QMessageBox.warning(self, APP_TITLE,
                                "Connect BOTH the SingleTact force sensor and MSO4104B first.")
            return

        self.acquisition_t0 = time.perf_counter()
        self.session_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.acquiring = True
        self.session_force_rows.clear()
        self.session_scope_records.clear()
        self.session_force_samples = 0
        self.session_scope_captures = 0
        self._reset_live_figures()
        # Align GUI plot origin exactly with recording zero.
        self.plot_origin_t0 = self.acquisition_t0

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.save_as_btn.setEnabled(False)
        self.csv_status_label.setText("Recording into RAM. No files written yet.")
        self.update_sample_status()
        self.scope_worker.request("start_sampling", {
            "t0": self.acquisition_t0,
            "channel": self.scope_channel_combo.currentText(),
            "interval_s": self.scope_sampling_interval_spin.value() / 1000.0,
        })
        self.status_message("START: plots reset, both sensors live, saving incoming data to RAM.")

    def stop_acquisition(self) -> None:
        if not self.acquiring:
            return
        self.acquiring = False
        self.scope_worker.request("stop_sampling")
        self._reset_live_figures()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        count = bool(self.session_force_rows or self.session_scope_records)
        self.recording_unsaved = count
        self.save_as_btn.setEnabled(count)
        self.update_sample_status()
        if count:
            self.csv_status_label.setText("Recording retained in RAM. Click SAVE AS to choose the destination.")
        else:
            self.csv_status_label.setText("No data captured. Live figures are still updating.")
        self.status_message("STOP: RAM recording preserved; both plots reset and continue live.")

    def save_as_session(self) -> None:
        if self.acquiring:
            QMessageBox.warning(self, APP_TITLE, "Press STOP SAMPLING before Save As.")
            return
        if self.saving_in_progress or not (self.session_force_rows or self.session_scope_records):
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose folder for separate Force and Oscilloscope CSV files", str(Path.home()))
        if not folder:
            return
        self.saving_in_progress = True
        self.start_btn.setEnabled(False)
        self.save_as_btn.setEnabled(False)
        self.csv_status_label.setText("Saving both CSV files in background; captured data remains in RAM...")
        self.csv_worker.save_session(folder, self.session_stamp,
                                     self.session_force_rows, self.session_scope_records)


    def _on_force_plot_manually_changed(self, _axes) -> None:
        # Never change a user's X/Y range while they are zooming or panning.
        self.force_follow_live = False
        self.force_zoom_status.setText("Auto follow: OFF (manual zoom/pan)")

    def resume_force_follow(self) -> None:
        self.force_follow_live = True
        self.force_zoom_status.setText("Auto follow: ON | Wheel: zoom | Drag: pan")
        if self.time_data:
            right = self.time_data[-1]
            self.force_plot.setXRange(
                max(0, right - self.window_seconds),
                max(self.window_seconds, right), padding=0
            )

    def refresh_plot(self) -> None:
        if not self.time_data or not self._force_plot_dirty:
            return
        # The 10Hz UI refresh is decoupled from the ~100Hz force serial samples.
        # The plot data can be decimated on screen, but all raw samples still
        # reach the recording RAM arrays without being skipped.
        self._force_plot_dirty = False
        x = list(self.time_data)
        y = list(self.force_data)
        self.force_curve.setData(x, y, skipFiniteCheck=True)
        now = time.perf_counter()
        if now - self._metrics_updated_at > 0.3:
            self.force_metrics.update_values(y)
            self._metrics_updated_at = now
        if self.force_follow_live:
            right = x[-1]
            self.force_plot.setXRange(
                max(0.0, right - self.window_seconds),
                max(self.window_seconds, right), padding=0
            )

    def on_window_changed(self, value: int) -> None:
        self.window_seconds = value
        if self.force_follow_live:
            self.resume_force_follow()

    def change_scope_waveform_interval(self, value: int) -> None:
        self.scope_worker.request("live_interval", max(0.1, value / 1000.0))

    # ==================================================================
    # General
    # ==================================================================
    def status_message(self, message: str) -> None:
        self.status_label.setText(message)


    def closeEvent(self, event) -> None:
        if self.saving_in_progress:
            QMessageBox.information(self, APP_TITLE,
                                    "Save As is still writing files. Please wait until it finishes.")
            event.ignore()
            return
        if self.acquiring:
            self.stop_acquisition()
        if self.recording_unsaved:
            confirm = QMessageBox.question(
                self, APP_TITLE,
                "The last recording is still in RAM and has not been saved. Exit and discard it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.scope_live_timer.stop()
        if self.motor_worker is not None and self.motor_worker.isRunning():
            self.motor_worker.request_stop(brake_on_exit=not self.estop_active)
        if self.force_worker is not None and self.force_worker.isRunning():
            self.force_worker.request_stop()
        if self.scope_worker is not None and self.scope_worker.isRunning():
            self.scope_worker.request_stop()
        if self.csv_worker is not None and self.csv_worker.isRunning():
            self.csv_worker.request_stop()

        # QObject/QThread cannot be destroyed while the associated native
        # thread is running. A VISA call may take up to its 5-second timeout.
        for worker in (self.motor_worker, self.force_worker, self.scope_worker, self.csv_worker):
            if worker is not None and worker.isRunning():
                if not worker.wait(7000):
                    QMessageBox.warning(self, APP_TITLE,
                        "A hardware thread is still finishing an I/O request. Please close again shortly.")
                    event.ignore()
                    return
        event.accept()



def main() -> None:
    app = QApplication(sys.argv)

    def exception_hook(exc_type, exc_value, exc_tb):
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            log_path = Path(__file__).resolve().with_name("shaker_gui_error.log")
            log_path.write_text(details, encoding="utf-8")
        except Exception:
            pass
        QMessageBox.critical(
            None,
            APP_TITLE,
            "An unexpected GUI error occurred. The details were saved to "
            "shaker_gui_error.log next to the GUI file.\n\n" + str(exc_value),
        )

    sys.excepthook = exception_hook
    pg.setConfigOptions(antialias=True)
    window = ShakerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

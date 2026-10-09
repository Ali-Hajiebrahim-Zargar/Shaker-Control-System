from __future__ import annotations

import csv
import math
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QIODevice, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


APP_TITLE = "Shaker Control & DAQ - CSU15-45N USB"

MOTOR_BAUD = 115200
FORCE_BAUD = 115200

# SingleTact CSU15-45N calibration
FORCE_RATING_N = 45.0
FORCE_COUNTS_FULL_SCALE = 512.0
FORCE_FACTORY_BASELINE = 255.0  # 0xFF, as used by the official NETInterface
FORCE_I2C_ADDRESS = 0x04
FORCE_READ_LOCATION = 128
FORCE_READ_LENGTH = 6
FORCE_POLL_INTERVAL_MS = 10  # target ~100 Hz
FORCE_REPLY_TIMEOUT_S = 0.15


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
        self.resize(1200, 820)

        # ------------------------------------------------------------
        # Motor serial port (ASCII, Arduino Mega)
        # ------------------------------------------------------------
        self.motor_serial = QSerialPort(self)
        self.motor_serial.readyRead.connect(self.on_motor_ready_read)
        self.motor_rx_buffer = bytearray()
        self.motor_connected = False
        self.motor_position = 0
        self.motor_state = "UNKNOWN"
        self.estop_active = False
        self.position_brake_active = False
        self.position_brake_target: int | None = None

        # ------------------------------------------------------------
        # Force serial port (binary, direct SingleTact USB)
        # ------------------------------------------------------------
        self.force_serial = QSerialPort(self)
        self.force_serial.readyRead.connect(self.on_force_ready_read)
        self.force_rx_buffer = bytearray()
        self.force_connected = False
        self.force_command_id = 0
        self.force_pending_id: int | None = None
        self.force_pending_since = 0.0
        self.last_force_iteration: int | None = None

        self.force_baseline_counts = FORCE_FACTORY_BASELINE
        self.force_raw_recent = deque(maxlen=50)
        self.latest_force_raw: int | None = None
        self.latest_force_n = 0.0
        self.force_sample_counter = 0
        self.force_timeout_counter = 0
        self.force_stream_t0 = time.perf_counter()

        # ------------------------------------------------------------
        # Acquisition / plot / CSV
        # ------------------------------------------------------------
        self.acquiring = False
        self.acquisition_t0 = 0.0
        self.window_seconds = 60
        self.time_data = deque()
        self.force_data = deque()

        self.csv_file = None
        self.csv_writer = None
        self.pending_csv_rows: list[list[object]] = []

        # ------------------------------------------------------------
        # Build UI
        # ------------------------------------------------------------
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        root.addWidget(self.build_connections_box())
        root.addWidget(self.build_force_box(), 1)
        root.addWidget(self.build_motor_box())
        root.addWidget(self.build_acquisition_box())

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("status")
        root.addWidget(self.status_label)

        # ------------------------------------------------------------
        # Timers
        # ------------------------------------------------------------
        self.force_poll_timer = QTimer(self)
        self.force_poll_timer.setInterval(FORCE_POLL_INTERVAL_MS)
        self.force_poll_timer.timeout.connect(self.poll_force_sensor)

        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(33)
        self.plot_timer.timeout.connect(self.refresh_plot)
        self.plot_timer.start()

        self.csv_flush_timer = QTimer(self)
        self.csv_flush_timer.setInterval(500)
        self.csv_flush_timer.timeout.connect(self.flush_csv)
        self.csv_flush_timer.start()

        self.refresh_ports()
        self.apply_style()

    # ==================================================================
    # UI
    # ==================================================================
    def build_connections_box(self) -> QGroupBox:
        box = QGroupBox("Connections")
        grid = QGridLayout(box)

        grid.addWidget(QLabel("Motor controller (Arduino Mega):"), 0, 0)
        self.motor_port_combo = QComboBox()
        self.motor_port_combo.setMinimumWidth(220)
        grid.addWidget(self.motor_port_combo, 0, 1)

        self.motor_connect_btn = QPushButton("Connect motor")
        self.motor_connect_btn.clicked.connect(self.toggle_motor_connection)
        grid.addWidget(self.motor_connect_btn, 0, 2)

        grid.addWidget(QLabel("Force sensor (SingleTact CSU15-45N USB):"), 1, 0)
        self.force_port_combo = QComboBox()
        self.force_port_combo.setMinimumWidth(220)
        grid.addWidget(self.force_port_combo, 1, 1)

        self.force_connect_btn = QPushButton("Connect force sensor")
        self.force_connect_btn.clicked.connect(self.toggle_force_connection)
        grid.addWidget(self.force_connect_btn, 1, 2)

        self.refresh_ports_btn = QPushButton("Refresh COM ports")
        self.refresh_ports_btn.clicked.connect(self.refresh_ports)
        grid.addWidget(self.refresh_ports_btn, 0, 3, 2, 1)

        self.motor_connection_label = QLabel("Motor: disconnected")
        self.force_connection_label = QLabel("Force: disconnected")
        grid.addWidget(self.motor_connection_label, 2, 0, 1, 2)
        grid.addWidget(self.force_connection_label, 2, 2, 1, 2)

        return box

    def build_force_box(self) -> QGroupBox:
        box = QGroupBox("Force - SingleTact CSU15-45N USB")
        layout = QVBoxLayout(box)

        self.force_plot = pg.PlotWidget()
        self.force_plot.setBackground(None)
        self.force_plot.showGrid(x=True, y=True, alpha=0.25)
        self.force_plot.setLabel("bottom", "Time", units="s")
        self.force_plot.setLabel("left", "Force", units="N")
        self.force_curve = self.force_plot.plot([], [], pen=pg.mkPen(width=1.5))
        self.force_plot.setYRange(-1.0, 45.0, padding=0.02)
        layout.addWidget(self.force_plot, 1)

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
            "Force is read directly from the USB sensor. The graph always shows the same live force "
            "value shown above. START/STOP ACQUISITION only controls recording to CSV. "
            "TARE FORCE uses recent unloaded USB readings as the software zero."
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

    def build_acquisition_box(self) -> QGroupBox:
        box = QGroupBox("Acquisition / CSV")
        grid = QGridLayout(box)

        self.start_btn = QPushButton("START ACQUISITION")
        self.start_btn.clicked.connect(self.start_acquisition)
        grid.addWidget(self.start_btn, 0, 0)

        self.stop_btn = QPushButton("STOP ACQUISITION")
        self.stop_btn.clicked.connect(self.stop_acquisition)
        self.stop_btn.setEnabled(False)
        grid.addWidget(self.stop_btn, 0, 1)

        self.window_spin = QSpinBox()
        self.window_spin.setRange(5, 600)
        self.window_spin.setValue(60)
        self.window_spin.setSuffix(" s")
        self.window_spin.valueChanged.connect(self.on_window_changed)
        grid.addWidget(QLabel("Plot window:"), 0, 2)
        grid.addWidget(self.window_spin, 0, 3)

        self.save_check = QCheckBox("Save acquisition to CSV")
        self.save_check.setChecked(True)
        grid.addWidget(self.save_check, 1, 0, 1, 2)

        self.save_folder_edit = QLineEdit(str(Path.home() / "Documents" / "ShakerData"))
        grid.addWidget(QLabel("Folder:"), 1, 2)
        grid.addWidget(self.save_folder_edit, 1, 3)

        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self.choose_save_folder)
        grid.addWidget(self.browse_btn, 1, 4)

        self.csv_status_label = QLabel("CSV saving: OFF")
        self.sample_status_label = QLabel("Force samples: 0")
        grid.addWidget(self.csv_status_label, 2, 0, 1, 3)
        grid.addWidget(self.sample_status_label, 2, 3, 1, 2)

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
            QLineEdit, QComboBox, QSpinBox {
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
    # Motor connection / protocol
    # ==================================================================
    def toggle_motor_connection(self) -> None:
        if self.motor_serial.isOpen():
            self.motor_serial.close()
            self.motor_connected = False
            self.motor_connect_btn.setText("Connect motor")
            self.motor_connection_label.setText("Motor: disconnected")
            return

        port = self.motor_port_combo.currentData()
        if not port:
            QMessageBox.warning(self, APP_TITLE, "Select the Arduino COM port.")
            return

        self.motor_serial.setPortName(port)
        self.motor_serial.setBaudRate(MOTOR_BAUD)
        self.motor_serial.setDataBits(QSerialPort.DataBits.Data8)
        self.motor_serial.setParity(QSerialPort.Parity.NoParity)
        self.motor_serial.setStopBits(QSerialPort.StopBits.OneStop)
        self.motor_serial.setFlowControl(QSerialPort.FlowControl.NoFlowControl)

        if not self.motor_serial.open(QIODevice.OpenModeFlag.ReadWrite):
            QMessageBox.critical(self, APP_TITLE, f"Cannot open {port}:\n{self.motor_serial.errorString()}")
            return

        self.motor_connected = True
        self.motor_rx_buffer.clear()
        self.motor_connect_btn.setText("Disconnect motor")
        self.motor_connection_label.setText(f"Motor: connected to {port} @ {MOTOR_BAUD}")
        self.send_motor_command(f"SET_LOCK_TOL,{self.lock_tolerance_spin.value()}")
        self.send_motor_command("GET_STATUS")

    def send_motor_command(self, command: str) -> None:
        if not self.motor_serial.isOpen():
            self.status_message("Motor command not sent: Arduino is not connected")
            return

        payload = (command.strip() + "\n").encode("ascii")
        self.motor_serial.write(payload)
        self.motor_serial.flush()
        print(f"MOTOR TX -> {command}", flush=True)

    def on_motor_ready_read(self) -> None:
        self.motor_rx_buffer.extend(bytes(self.motor_serial.readAll()))

        while b"\n" in self.motor_rx_buffer:
            raw_line, _, remainder = self.motor_rx_buffer.partition(b"\n")
            self.motor_rx_buffer = bytearray(remainder)
            line = raw_line.decode("ascii", errors="ignore").strip()
            if line:
                self.parse_motor_line(line)

    def parse_motor_line(self, line: str) -> None:
        print(f"MOTOR RX <- {line}", flush=True)
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
    # Direct SingleTact USB connection / protocol
    # ==================================================================
    def toggle_force_connection(self) -> None:
        if self.force_serial.isOpen():
            self.force_poll_timer.stop()
            try:
                self.force_serial.setRequestToSend(False)
                self.force_serial.setDataTerminalReady(False)
            except Exception:
                pass
            self.force_serial.close()
            self.force_connected = False
            self.force_pending_id = None
            self.force_connect_btn.setText("Connect force sensor")
            self.force_connection_label.setText("Force: disconnected")
            self.status_message("SingleTact USB disconnected")
            return

        port = self.force_port_combo.currentData()
        if not port:
            QMessageBox.warning(self, APP_TITLE, "Select the SingleTact USB COM port.")
            return

        self.force_serial.setPortName(port)
        self.force_serial.setBaudRate(FORCE_BAUD)
        self.force_serial.setDataBits(QSerialPort.DataBits.Data8)
        self.force_serial.setParity(QSerialPort.Parity.NoParity)
        self.force_serial.setStopBits(QSerialPort.StopBits.OneStop)
        self.force_serial.setFlowControl(QSerialPort.FlowControl.NoFlowControl)

        if not self.force_serial.open(QIODevice.OpenModeFlag.ReadWrite):
            QMessageBox.critical(self, APP_TITLE, f"Cannot open {port}:\n{self.force_serial.errorString()}")
            return

        # Match the initialization sequence used by the official SingleTact
        # NETInterface driver. The USB electronics expect the serial control
        # lines to be toggled before normal command/response traffic begins.
        self.force_connection_label.setText(
            f"Force: initializing {port} @ {FORCE_BAUD} ..."
        )
        self.status_message("Initializing SingleTact USB electronics...")
        QApplication.processEvents()

        self.force_serial.clear(QSerialPort.Direction.AllDirections)
        self.force_serial.setDataTerminalReady(True)
        time.sleep(0.010)
        self.force_serial.setDataTerminalReady(False)

        # The official driver waits about two seconds after the DTR reset.
        time.sleep(2.0)
        self.force_serial.setRequestToSend(True)
        self.force_serial.clear(QSerialPort.Direction.AllDirections)

        self.force_rx_buffer.clear()
        self.force_pending_id = None
        self.force_pending_since = 0.0
        self.last_force_iteration = None
        self.force_timeout_counter = 0
        self.force_sample_counter = 0
        self.force_command_id = 0
        self.force_stream_t0 = time.perf_counter()
        self.time_data.clear()
        self.force_data.clear()
        self.force_curve.setData([], [])
        self.force_metrics.update_values([])
        self.force_connected = True

        self.force_connect_btn.setText("Disconnect force sensor")
        self.force_connection_label.setText(
            f"Force: connected to {port} @ {FORCE_BAUD}; waiting for data"
        )
        self.sample_status_label.setText("Force samples: 0 | timeouts: 0")

        # Start polling only after the USB electronics have completed the same
        # reset/initialization sequence used by the official application.
        self.force_poll_timer.start()
        self.status_message("SingleTact USB initialized; direct force polling started")

    @staticmethod
    def build_singletact_read_command(command_id: int) -> bytes:
        # Exact packet layout used by SingleTact NETInterface GenerateReadCommand.
        # The official .NET implementation allocates 16 bytes; byte 15 remains 0.
        packet = bytearray(16)
        packet[0:4] = b"\xFF\xFF\xFF\xFF"
        packet[4] = FORCE_I2C_ADDRESS
        packet[5] = 100  # timeout field used by the official protocol
        packet[6] = command_id & 0xFF
        packet[7] = 0x01  # CMD_READ
        packet[8] = FORCE_READ_LOCATION
        packet[9] = FORCE_READ_LENGTH
        packet[10] = 0xFF
        packet[11:15] = b"\xFE\xFE\xFE\xFE"
        packet[15] = 0x00
        return bytes(packet)

    def poll_force_sensor(self) -> None:
        if not self.force_serial.isOpen():
            return

        now = time.perf_counter()

        if self.force_pending_id is not None:
            if now - self.force_pending_since <= FORCE_REPLY_TIMEOUT_S:
                return
            self.force_timeout_counter += 1
            self.force_pending_id = None
            if self.force_timeout_counter == 10:
                self.force_connection_label.setText(
                    "Force: connected, but no replies received from sensor"
                )
                self.status_message(
                    "SingleTact is not replying. Make sure the official SingleTact app is closed and the correct COM port is selected."
                )

        command_id = self.force_command_id & 0xFF
        self.force_command_id = (self.force_command_id + 1) & 0xFF
        packet = self.build_singletact_read_command(command_id)

        self.force_serial.write(packet)
        self.force_serial.flush()
        self.force_pending_id = command_id
        self.force_pending_since = now

    def on_force_ready_read(self) -> None:
        self.force_rx_buffer.extend(bytes(self.force_serial.readAll()))
        self.parse_force_packets()

    @staticmethod
    def _is_header4(buf: bytearray, index: int) -> bool:
        if index + 4 > len(buf):
            return False
        return all(buf[index + k] in (0xAA, 0xFF) for k in range(4))

    def parse_force_packets(self) -> None:
        # Response format used by SingleTact NETInterface:
        # 0..3  header (USB usually AA AA AA AA; Arduino interface uses FF)
        # 4     address
        # 5     timeout flag
        # 6     command ID
        # 7..10 timestamp
        # 11    N data bytes
        # 12..  data
        # final 4 bytes FE FE FE FE
        while True:
            if len(self.force_rx_buffer) < 16:
                return

            header_index = None
            for i in range(0, len(self.force_rx_buffer) - 3):
                if self._is_header4(self.force_rx_buffer, i):
                    header_index = i
                    break

            if header_index is None:
                # Keep only a few trailing bytes in case a header is split across reads.
                if len(self.force_rx_buffer) > 3:
                    del self.force_rx_buffer[:-3]
                return

            if header_index > 0:
                del self.force_rx_buffer[:header_index]

            if len(self.force_rx_buffer) < 12:
                return

            n_bytes = self.force_rx_buffer[11]
            packet_length = 16 + n_bytes

            if len(self.force_rx_buffer) < packet_length:
                return

            packet = bytes(self.force_rx_buffer[:packet_length])

            if packet[-4:] != b"\xFE\xFE\xFE\xFE":
                del self.force_rx_buffer[0]
                continue

            del self.force_rx_buffer[:packet_length]
            self.handle_force_packet(packet)

    def handle_force_packet(self, packet: bytes) -> None:
        if len(packet) < 22:
            return

        command_id = packet[6]
        n_bytes = packet[11]
        timeout_flag = packet[5]
        if timeout_flag != 0 or n_bytes < FORCE_READ_LENGTH:
            return

        if self.force_pending_id is not None and command_id == self.force_pending_id:
            self.force_pending_id = None

        data = packet[12 : 12 + n_bytes]
        if len(data) < 6:
            return

        iteration = (data[0] << 8) | data[1]
        raw_counts = (data[4] << 8) | data[5]

        # The sensor can return the same frame more than once if polled faster
        # than its internal update rate. Do not duplicate those samples.
        if self.last_force_iteration == iteration:
            return
        self.last_force_iteration = iteration

        force_n = (raw_counts - self.force_baseline_counts) * FORCE_RATING_N / FORCE_COUNTS_FULL_SCALE

        self.latest_force_raw = raw_counts
        self.latest_force_n = force_n
        self.force_raw_recent.append(raw_counts)
        self.force_sample_counter += 1

        if self.force_sample_counter == 1:
            port = self.force_serial.portName()
            self.force_connection_label.setText(
                f"Force: receiving data from {port} @ {FORCE_BAUD}"
            )
            self.status_message("SingleTact USB data received successfully")

        self.force_current_label.setText(f"Live force: {force_n:.4f} N")
        self.force_raw_label.setText(f"Raw: {raw_counts} counts")
        self.sample_status_label.setText(
            f"Force samples: {self.force_sample_counter} | timeouts: {self.force_timeout_counter}"
        )

        # The plot is a true live view: every valid USB force sample is
        # appended to the graph, independent of CSV acquisition state.
        live_t = time.perf_counter() - self.force_stream_t0
        self.time_data.append(live_t)
        self.force_data.append(force_n)

        cutoff = live_t - self.window_seconds
        while self.time_data and self.time_data[0] < cutoff:
            self.time_data.popleft()
            self.force_data.popleft()

        # START/STOP ACQUISITION controls only data recording. The plotted
        # value and the numeric Live force label always use this same force_n.
        if self.acquiring and self.csv_writer is not None:
            acquisition_t = time.perf_counter() - self.acquisition_t0
            self.pending_csv_rows.append(
                [
                    f"{acquisition_t:.6f}",
                    raw_counts,
                    f"{force_n:.6f}",
                    self.motor_position,
                    self.motor_state,
                ]
            )

    # ==================================================================
    # Force controls
    # ==================================================================
    def tare_force(self) -> None:
        if len(self.force_raw_recent) < 10:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Not enough recent force samples yet. Keep the sensor unloaded for a moment and press TARE FORCE again.",
            )
            return

        recent = list(self.force_raw_recent)[-20:]
        self.force_baseline_counts = sum(recent) / len(recent)
        self.force_baseline_label.setText(f"Baseline: {self.force_baseline_counts:.2f} counts")

        # Old plotted points were calculated with the previous baseline, so
        # clear them to ensure the graph and Live force always use one zero.
        self.force_stream_t0 = time.perf_counter()
        self.time_data.clear()
        self.force_data.clear()
        self.force_curve.setData([], [])
        self.force_metrics.update_values([])
        self.status_message("Force tare applied; live graph restarted with the new zero")

    def reset_force_zero(self) -> None:
        self.force_baseline_counts = FORCE_FACTORY_BASELINE
        self.force_baseline_label.setText(f"Baseline: {self.force_baseline_counts:.1f} counts")

        # Restart the live graph because the force conversion baseline changed.
        self.force_stream_t0 = time.perf_counter()
        self.time_data.clear()
        self.force_data.clear()
        self.force_curve.setData([], [])
        self.force_metrics.update_values([])
        self.status_message("Force baseline reset to 255; live graph restarted")

    # ==================================================================
    # Motor controls
    # ==================================================================
    def on_speed_changed(self, value: int) -> None:
        value = max(0, min(5, value))
        self.speed_label.setText(f"{value} %")

    def start_motor_motion(self, direction: str) -> None:
        if not self.motor_serial.isOpen():
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
        if not self.motor_serial.isOpen() or self.estop_active:
            return

        # Release means dynamic brake only. POSITION BRAKE is not re-enabled.
        self.send_motor_command("MOTOR_BRAKE")

    def brake_motor(self) -> None:
        if not self.motor_serial.isOpen():
            return
        self.position_brake_active = False
        self.position_brake_target = None
        self.position_brake_btn.setText("POSITION BRAKE")
        self.update_position_brake_target_label()
        self.send_motor_command("MOTOR_BRAKE")

    def toggle_position_brake(self) -> None:
        if not self.motor_serial.isOpen():
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
        if self.motor_serial.isOpen():
            self.send_motor_command(f"SET_LOCK_TOL,{value}")

    def emergency_stop(self) -> None:
        if self.motor_serial.isOpen():
            self.send_motor_command("ESTOP")
        self.estop_active = True
        self.position_brake_active = False
        self.position_brake_target = None
        self.position_brake_btn.setText("POSITION BRAKE")
        self.update_position_brake_target_label()
        self.status_message("EMERGENCY STOP activated")

    def reset_estop(self) -> None:
        if not self.motor_serial.isOpen():
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
    # Acquisition / plotting / CSV
    # ==================================================================
    def start_acquisition(self) -> None:
        if not self.force_serial.isOpen():
            QMessageBox.warning(self, APP_TITLE, "Connect the SingleTact USB force sensor first.")
            return

        # Do not clear or restart the live graph here. The graph already follows
        # the sensor continuously; acquisition only starts a new recording time.
        self.acquisition_t0 = time.perf_counter()
        self.acquiring = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        if self.save_check.isChecked():
            self.open_csv_file()
        else:
            self.csv_status_label.setText("CSV saving: OFF")

        self.status_message("Acquisition recording started; live graph continues continuously")

    def stop_acquisition(self) -> None:
        if not self.acquiring:
            return

        self.acquiring = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.flush_csv()
        self.close_csv_file()
        self.status_message("Acquisition recording stopped; live graph is still running")

    def refresh_plot(self) -> None:
        if not self.time_data:
            return

        x = list(self.time_data)
        y = list(self.force_data)
        self.force_curve.setData(x, y)
        self.force_metrics.update_values(y)

        right = x[-1]
        if right >= self.window_seconds:
            self.force_plot.setXRange(right - self.window_seconds, right, padding=0)
        else:
            self.force_plot.setXRange(0, self.window_seconds, padding=0)

    def on_window_changed(self, value: int) -> None:
        self.window_seconds = value

    def choose_save_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select data folder",
            self.save_folder_edit.text() or str(Path.home()),
        )
        if folder:
            self.save_folder_edit.setText(folder)

    def open_csv_file(self) -> None:
        folder = Path(self.save_folder_edit.text()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = folder / f"shaker_csu15_45n_{stamp}.csv"
            self.csv_file = path.open("w", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(
                [
                    "time_s",
                    "force_raw_counts",
                    "force_N",
                    "motor_position_counts",
                    "motor_state",
                ]
            )
            self.csv_status_label.setText(f"CSV saving: {path.name}")
        except OSError as exc:
            self.csv_file = None
            self.csv_writer = None
            QMessageBox.warning(self, APP_TITLE, f"Could not create CSV file:\n{exc}")
            self.csv_status_label.setText("CSV saving: ERROR")

    def flush_csv(self) -> None:
        if self.csv_writer is None or self.csv_file is None or not self.pending_csv_rows:
            return
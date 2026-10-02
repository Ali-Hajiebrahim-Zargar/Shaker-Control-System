from __future__ import annotations

import csv
import math
import random
import sys
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QIODevice, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
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
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import pyqtgraph as pg


APP_TITLE = "Shaker Control & DAQ"
DEFAULT_BAUD = 115200

# -----------------------------------------------------------------------------
# Serial protocol expected from Arduino
# -----------------------------------------------------------------------------
# Arduino -> GUI, one sample per line:
# DATA,<time_s>,<piezo_v>,<force_n>,<position_counts>,<motor_state>
# Example:
# DATA,1.234000,0.8421,12.55,1532,LOCKED
#
# GUI -> Arduino commands:
# START,<sample_rate_hz>
# STOP
# TARE_FORCE
# MOTOR_SPEED,<0..5>   # GUI safety cap
# MOTOR_UP,<0..5>      # GUI safety cap
# MOTOR_DOWN,<0..5>    # GUI safety cap
# MOTOR_LOCK,<tolerance_counts>
# MOTOR_STOP
# ESTOP
# SET_LOCK_TOL,<tolerance_counts>
#
# Notes:
# - piezo_v must be the RAW ADC-derived voltage, with no digital filtering.
# - force_n must already be converted to Newtons by the Arduino firmware.
# - position_counts is the relative encoder count reported by the Arduino.
# -----------------------------------------------------------------------------


class MetricLabels(QWidget):
    """Compact metric display for one signal."""

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
            label.setMinimumWidth(120)
            self.labels[key] = label
            layout.addWidget(label)

        layout.addStretch(1)

    def set_visibility(self, enabled: dict[str, bool]) -> None:
        for key, label in self.labels.items():
            label.setVisible(enabled.get(key, False))

    def update_values(self, values: list[float] | deque[float]) -> None:
        if not values:
            for key, label in self.labels.items():
                title = key.upper() if key == "rms" else key.capitalize()
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


class SignalPanel(QGroupBox):
    def __init__(self, title: str, y_label: str, unit: str, parent=None):
        super().__init__(title, parent)
        self.unit = unit

        root = QVBoxLayout(self)

        self.plot = pg.PlotWidget()
        self.plot.setBackground(None)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setLabel("left", y_label, units=unit)
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.getPlotItem().setClipToView(True)
        self.plot.getPlotItem().setDownsampling(auto=True, mode="peak")
        self.curve = self.plot.plot([], [], pen=pg.mkPen(width=1.5))

        toolbar = QHBoxLayout()
        self.auto_scale_btn = QPushButton("Auto scale")
        self.reset_view_btn = QPushButton("Reset view")
        self.auto_scale_btn.clicked.connect(self.enable_auto_range)
        self.reset_view_btn.clicked.connect(self.reset_view)
        toolbar.addWidget(self.auto_scale_btn)
        toolbar.addWidget(self.reset_view_btn)
        toolbar.addStretch(1)

        self.metrics = MetricLabels(unit)

        root.addWidget(self.plot, 1)
        root.addLayout(toolbar)
        root.addWidget(self.metrics)

    def enable_auto_range(self) -> None:
        self.plot.enableAutoRange(axis="y", enable=True)

    def reset_view(self) -> None:
        self.plot.enableAutoRange(axis="x", enable=True)
        self.plot.enableAutoRange(axis="y", enable=True)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 980)

        # -------------------- runtime state --------------------
        self.serial = QSerialPort(self)
        self.serial.readyRead.connect(self.on_serial_ready_read)
        self.rx_buffer = bytearray()

        self.connected = False
        self.acquiring = False
        self.demo_mode = False
        self.estop_active = False
        self.motor_state = "STOPPED"

        self.window_seconds = 60
        self.sample_rate_hz = 1000
        self.lock_tolerance_counts = 2

        self.time_data = deque()
        self.piezo_data = deque()
        self.force_data = deque()
        self.position_data = deque()

        self.csv_file = None
        self.csv_writer = None
        self.pending_csv_rows: list[list[object]] = []

        self.demo_t = 0.0
        self.demo_position = 0
        self.demo_direction = 0
        self.demo_locked = False

        # -------------------- UI --------------------
        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        self.dashboard_tab = QWidget()
        self.settings_tab = QWidget()
        tabs.addTab(self.dashboard_tab, "Dashboard")
        tabs.addTab(self.settings_tab, "Settings")

        self.build_dashboard()
        self.build_settings()

        # -------------------- timers --------------------
        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(33)  # ~30 FPS GUI refresh
        self.plot_timer.timeout.connect(self.refresh_plots)
        self.plot_timer.start()

        self.csv_flush_timer = QTimer(self)
        self.csv_flush_timer.setInterval(500)
        self.csv_flush_timer.timeout.connect(self.flush_csv)
        self.csv_flush_timer.start()

        self.demo_timer = QTimer(self)
        self.demo_timer.timeout.connect(self.generate_demo_sample)

        # Delay timer used after releasing UP/DOWN.
        # The motor is stopped immediately on release, then the current
        # encoder position is captured and locked after a short settling time.
        self.lock_delay_timer = QTimer(self)
        self.lock_delay_timer.setSingleShot(True)
        self.lock_delay_timer.setInterval(150)
        self.lock_delay_timer.timeout.connect(self.lock_current_position)

        self.refresh_ports()
        self.apply_settings_to_runtime()
        self.apply_style()

    # =====================================================================
    # UI construction
    # =====================================================================
    def build_dashboard(self) -> None:
        root = QVBoxLayout(self.dashboard_tab)

        # ---------- connection / acquisition bar ----------
        top = QHBoxLayout()
        top.setSpacing(8)

        top.addWidget(QLabel("COM port:"))
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(160)
        top.addWidget(self.port_combo)

        self.refresh_ports_btn = QPushButton("Refresh")
        self.refresh_ports_btn.clicked.connect(self.refresh_ports)
        top.addWidget(self.refresh_ports_btn)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connection)
        top.addWidget(self.connect_btn)

        top.addSpacing(20)

        self.start_btn = QPushButton("START ACQUISITION")
        self.start_btn.clicked.connect(self.start_acquisition)
        top.addWidget(self.start_btn)

        self.stop_btn = QPushButton("STOP ACQUISITION")
        self.stop_btn.clicked.connect(self.stop_acquisition)
        self.stop_btn.setEnabled(False)
        top.addWidget(self.stop_btn)

        top.addStretch(1)

        self.estop_btn = QPushButton("EMERGENCY STOP")
        self.estop_btn.setObjectName("estop")
        self.estop_btn.clicked.connect(self.emergency_stop)
        top.addWidget(self.estop_btn)

        root.addLayout(top)

        self.status_label = QLabel("Disconnected")
        self.status_label.setObjectName("status")
        root.addWidget(self.status_label)

        # ---------- plots ----------
        splitter = QSplitter(Qt.Orientation.Vertical)

        self.piezo_panel = SignalPanel(
            "1) Piezoelectric sensor - raw ADC voltage",
            "Piezo voltage",
            "V",
        )
        self.force_panel = SignalPanel(
            "2) Force sensor - SingleTact CS15-450N",
            "Force",
            "N",
        )

        splitter.addWidget(self.piezo_panel)
        splitter.addWidget(self.force_panel)
        splitter.setSizes([330, 330])

        root.addWidget(splitter, 1)

        # ---------- motor panel ----------
        motor_box = QGroupBox("3) Motor control - FAULHABER + L298N + IE2-512")
        motor_layout = QGridLayout(motor_box)

        motor_layout.addWidget(QLabel("Speed command (max 5%):"), 0, 0)
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(0, 5)
        self.speed_slider.setValue(5)
        self.speed_slider.valueChanged.connect(self.on_speed_changed)
        motor_layout.addWidget(self.speed_slider, 0, 1, 1, 3)

        self.speed_label = QLabel("5 %")
        self.speed_label.setMinimumWidth(70)
        motor_layout.addWidget(self.speed_label, 0, 4)

        self.up_btn = QPushButton("▲  UP")
        self.down_btn = QPushButton("▼  DOWN")
        self.up_btn.setMinimumHeight(48)
        self.down_btn.setMinimumHeight(48)
        self.up_btn.pressed.connect(lambda: self.start_motor_motion("UP"))
        self.up_btn.released.connect(self.finish_motor_motion)
        self.down_btn.pressed.connect(lambda: self.start_motor_motion("DOWN"))
        self.down_btn.released.connect(self.finish_motor_motion)
        motor_layout.addWidget(self.up_btn, 1, 0, 1, 2)
        motor_layout.addWidget(self.down_btn, 1, 2, 1, 2)

        self.motor_stop_btn = QPushButton("STOP MOTOR")
        self.motor_stop_btn.clicked.connect(self.stop_motor)
        motor_layout.addWidget(self.motor_stop_btn, 1, 4)

        self.position_label = QLabel("Relative position: 0 counts")
        self.position_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        motor_layout.addWidget(self.position_label, 2, 0, 1, 2)

        self.motor_state_label = QLabel("Motor state: STOPPED")
        self.motor_state_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        motor_layout.addWidget(self.motor_state_label, 2, 2, 1, 2)

        self.lock_btn = QPushButton("LOCK CURRENT POSITION")
        self.lock_btn.clicked.connect(self.lock_current_position)
        motor_layout.addWidget(self.lock_btn, 2, 4)

        self.force_tare_btn = QPushButton("TARE FORCE")
        self.force_tare_btn.clicked.connect(self.tare_force)
        motor_layout.addWidget(self.force_tare_btn, 3, 0)

        self.last_sample_label = QLabel("Last sample: --")
        motor_layout.addWidget(self.last_sample_label, 3, 1, 1, 2)

        self.csv_status_label = QLabel("CSV saving: OFF")
        motor_layout.addWidget(self.csv_status_label, 3, 3, 1, 2)

        motor_layout.setColumnStretch(1, 1)
        motor_layout.setColumnStretch(3, 1)
        root.addWidget(motor_box)

    def build_settings(self) -> None:
        root = QVBoxLayout(self.settings_tab)

        # ---------- Acquisition ----------
        acquisition_box = QGroupBox("Acquisition")
        acquisition_form = QFormLayout(acquisition_box)

        self.sample_rate_spin = QSpinBox()
        self.sample_rate_spin.setRange(1, 1000)
        self.sample_rate_spin.setValue(1000)
        self.sample_rate_spin.setSuffix(" Hz")
        acquisition_form.addRow("Piezo sample rate:", self.sample_rate_spin)

        self.window_spin = QSpinBox()
        self.window_spin.setRange(5, 600)
        self.window_spin.setValue(60)
        self.window_spin.setSuffix(" s")
        acquisition_form.addRow("Moving plot window:", self.window_spin)

        self.baud_combo = QComboBox()
        for baud in [115200, 230400, 460800, 500000, 921600]:
            self.baud_combo.addItem(str(baud), baud)
        idx = self.baud_combo.findData(DEFAULT_BAUD)
        if idx >= 0:
            self.baud_combo.setCurrentIndex(idx)
        acquisition_form.addRow("Serial baud rate:", self.baud_combo)

        self.demo_check = QCheckBox("Demo mode (run GUI without Arduino)")
        acquisition_form.addRow(self.demo_check)

        root.addWidget(acquisition_box)

        # ---------- Metrics ----------
        metrics_box = QGroupBox("Displayed metrics")
        metrics_grid = QGridLayout(metrics_box)

        metrics_grid.addWidget(QLabel("Metric"), 0, 0)
        metrics_grid.addWidget(QLabel("Piezo"), 0, 1)
        metrics_grid.addWidget(QLabel("Force"), 0, 2)

        self.piezo_metric_checks: dict[str, QCheckBox] = {}
        self.force_metric_checks: dict[str, QCheckBox] = {}

        for row, (key, title) in enumerate(
            [
                ("current", "Current"),
                ("max", "Maximum"),
                ("min", "Minimum"),
                ("mean", "Mean"),
                ("rms", "RMS"),
            ],
            start=1,
        ):
            metrics_grid.addWidget(QLabel(title), row, 0)
            p = QCheckBox()
            f = QCheckBox()
            p.setChecked(key in {"current", "max", "min"})
            f.setChecked(key in {"current", "max", "min"})
            self.piezo_metric_checks[key] = p
            self.force_metric_checks[key] = f
            metrics_grid.addWidget(p, row, 1)
            metrics_grid.addWidget(f, row, 2)

        root.addWidget(metrics_box)

        # ---------- Force ----------
        force_box = QGroupBox("Force sensor")
        force_form = QFormLayout(force_box)

        self.auto_tare_check = QCheckBox("Automatically tare force when START is pressed")
        self.auto_tare_check.setChecked(True)
        force_form.addRow(self.auto_tare_check)

        root.addWidget(force_box)

        # ---------- Saving ----------
        save_box = QGroupBox("Data saving")
        save_layout = QGridLayout(save_box)

        self.save_check = QCheckBox("Save acquisition to CSV")
        self.save_check.setChecked(True)
        save_layout.addWidget(self.save_check, 0, 0, 1, 2)

        self.save_folder_edit = QLineEdit(str(Path.home() / "Documents" / "ShakerData"))
        save_layout.addWidget(QLabel("Folder:"), 1, 0)
        save_layout.addWidget(self.save_folder_edit, 1, 1)

        self.browse_save_btn = QPushButton("Browse...")
        self.browse_save_btn.clicked.connect(self.choose_save_folder)
        save_layout.addWidget(self.browse_save_btn, 1, 2)

        root.addWidget(save_box)

        # ---------- Motor ----------
        motor_box = QGroupBox("Motor / position lock")
        motor_form = QFormLayout(motor_box)

        self.lock_tolerance_spin = QSpinBox()
        self.lock_tolerance_spin.setRange(0, 1000)
        self.lock_tolerance_spin.setValue(2)
        self.lock_tolerance_spin.setSuffix(" encoder counts")
        motor_form.addRow("Position-lock tolerance:", self.lock_tolerance_spin)

        self.limit_switch_check = QCheckBox("Use upper and lower limit switches")
        self.limit_switch_check.setChecked(True)
        motor_form.addRow(self.limit_switch_check)

        self.invert_direction_check = QCheckBox("Invert UP/DOWN direction")
        motor_form.addRow(self.invert_direction_check)

        root.addWidget(motor_box)

        # ---------- Plot scale ----------
        scale_box = QGroupBox("Plot Y-axis scale")
        scale_grid = QGridLayout(scale_box)

        self.piezo_auto_y_check = QCheckBox("Auto")
        self.piezo_auto_y_check.setChecked(True)
        self.piezo_ymin = QDoubleSpinBox()
        self.piezo_ymax = QDoubleSpinBox()
        self.piezo_ymin.setRange(-1000, 1000)
        self.piezo_ymax.setRange(-1000, 1000)
        self.piezo_ymin.setDecimals(4)
        self.piezo_ymax.setDecimals(4)
        self.piezo_ymin.setValue(0.0)
        self.piezo_ymax.setValue(5.0)

        self.force_auto_y_check = QCheckBox("Auto")
        self.force_auto_y_check.setChecked(True)
        self.force_ymin = QDoubleSpinBox()
        self.force_ymax = QDoubleSpinBox()
        self.force_ymin.setRange(-10000, 10000)
        self.force_ymax.setRange(-10000, 10000)
        self.force_ymin.setDecimals(2)
        self.force_ymax.setDecimals(2)
        self.force_ymin.setValue(0.0)
        self.force_ymax.setValue(450.0)

        scale_grid.addWidget(QLabel("Signal"), 0, 0)
        scale_grid.addWidget(QLabel("Auto"), 0, 1)
        scale_grid.addWidget(QLabel("Y min"), 0, 2)
        scale_grid.addWidget(QLabel("Y max"), 0, 3)

        scale_grid.addWidget(QLabel("Piezo (V)"), 1, 0)
        scale_grid.addWidget(self.piezo_auto_y_check, 1, 1)
        scale_grid.addWidget(self.piezo_ymin, 1, 2)
        scale_grid.addWidget(self.piezo_ymax, 1, 3)

        scale_grid.addWidget(QLabel("Force (N)"), 2, 0)
        scale_grid.addWidget(self.force_auto_y_check, 2, 1)
        scale_grid.addWidget(self.force_ymin, 2, 2)
        scale_grid.addWidget(self.force_ymax, 2, 3)

        root.addWidget(scale_box)

        # ---------- apply ----------
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.apply_settings_btn = QPushButton("APPLY SETTINGS")
        self.apply_settings_btn.clicked.connect(self.apply_settings_to_runtime)
        buttons.addWidget(self.apply_settings_btn)
        root.addLayout(buttons)
        root.addStretch(1)

    # =====================================================================
    # Styling
    # =====================================================================
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
            QPushButton:hover {
                background: #454850;
            }
            QPushButton:pressed {
                background: #2a2c30;
            }
            QPushButton#estop {
                background: #8a1f1f;
                border: 1px solid #cc5555;
                font-weight: bold;
            }
            QPushButton#estop:hover {
                background: #a52b2b;
            }
            QLabel#status {
                padding: 4px;
                font-weight: bold;
            }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #2a2c30;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 4px;
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
            """
        )

    # =====================================================================
    # Settings
    # =====================================================================
    def apply_settings_to_runtime(self) -> None:
        if hasattr(self, "sample_rate_spin"):
            self.sample_rate_hz = self.sample_rate_spin.value()
            self.window_seconds = self.window_spin.value()
            self.lock_tolerance_counts = self.lock_tolerance_spin.value()
            self.demo_mode = self.demo_check.isChecked()

            self.piezo_panel.metrics.set_visibility(
                {k: cb.isChecked() for k, cb in self.piezo_metric_checks.items()}
            )
            self.force_panel.metrics.set_visibility(
                {k: cb.isChecked() for k, cb in self.force_metric_checks.items()}
            )

            self.apply_y_axis_settings()

            if self.connected and not self.demo_mode:
                self.send_command(f"SET_LOCK_TOL,{self.lock_tolerance_counts}")

        self.status_message("Settings applied")

    def apply_y_axis_settings(self) -> None:
        if self.piezo_auto_y_check.isChecked():
            self.piezo_panel.plot.enableAutoRange(axis="y", enable=True)
        else:
            self.piezo_panel.plot.enableAutoRange(axis="y", enable=False)
            lo, hi = self.piezo_ymin.value(), self.piezo_ymax.value()
            if hi > lo:
                self.piezo_panel.plot.setYRange(lo, hi, padding=0)

        if self.force_auto_y_check.isChecked():
            self.force_panel.plot.enableAutoRange(axis="y", enable=True)
        else:
            self.force_panel.plot.enableAutoRange(axis="y", enable=False)
            lo, hi = self.force_ymin.value(), self.force_ymax.value()
            if hi > lo:
                self.force_panel.plot.setYRange(lo, hi, padding=0)

    def choose_save_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select data folder",
            self.save_folder_edit.text() or str(Path.home()),
        )
        if folder:
            self.save_folder_edit.setText(folder)

    # =====================================================================
    # Serial connection
    # =====================================================================
    def refresh_ports(self) -> None:
        current = self.port_combo.currentText() if hasattr(self, "port_combo") else ""
        self.port_combo.clear()
        ports = QSerialPortInfo.availablePorts()
        for info in ports:
            label = info.portName()
            if info.description():
                label += f" - {info.description()}"
            self.port_combo.addItem(label, info.portName())

        if current:
            for i in range(self.port_combo.count()):
                if self.port_combo.itemText(i) == current:
                    self.port_combo.setCurrentIndex(i)
                    break

    def toggle_connection(self) -> None:
        self.apply_settings_to_runtime()

        if self.demo_mode:
            self.connected = not self.connected
            self.connect_btn.setText("Disconnect" if self.connected else "Connect")
            self.status_message("Demo mode connected" if self.connected else "Disconnected")
            return

        if self.serial.isOpen():
            self.serial.close()
            self.connected = False
            self.connect_btn.setText("Connect")
            self.status_message("Disconnected")
            return

        if self.port_combo.currentIndex() < 0:
            QMessageBox.warning(self, APP_TITLE, "No serial port is selected.")
            return

        port_name = self.port_combo.currentData()
        baud = int(self.baud_combo.currentData())
        self.serial.setPortName(port_name)
        self.serial.setBaudRate(baud)
        self.serial.setDataBits(QSerialPort.DataBits.Data8)
        self.serial.setParity(QSerialPort.Parity.NoParity)
        self.serial.setStopBits(QSerialPort.StopBits.OneStop)
        self.serial.setFlowControl(QSerialPort.FlowControl.NoFlowControl)

        if not self.serial.open(QIODevice.OpenModeFlag.ReadWrite):
            QMessageBox.critical(
                self,
                APP_TITLE,
                f"Could not open {port_name}.\n{self.serial.errorString()}",
            )
            return

        self.connected = True
        self.connect_btn.setText("Disconnect")
        self.status_message(f"Connected to {port_name} at {baud} baud")
        self.send_command(f"SET_LOCK_TOL,{self.lock_tolerance_counts}")

    def send_command(self, command: str) -> None:
        """Send one newline-terminated ASCII command to Arduino and log it."""
        if self.demo_mode:
            print(f"DEMO MODE - command not sent: {command}", flush=True)
            return

        if not self.serial.isOpen():
            print(f"SERIAL CLOSED - command not sent: {command}", flush=True)
            self.status_message(f"Serial closed - command not sent: {command}")
            return

        payload = (command.strip() + "\n").encode("ascii", errors="ignore")

        print(f"TX -> ARDUINO: {command}", flush=True)

        bytes_queued = self.serial.write(payload)

        # Force Qt to push the command to the operating system immediately.
        self.serial.flush()
        sent_ok = self.serial.waitForBytesWritten(300)

        if bytes_queued < 0 or not sent_ok:
            print(
                f"SERIAL WRITE WARNING: command={command}, "
                f"bytes_queued={bytes_queued}, error={self.serial.errorString()}",
                flush=True,
            )

    def on_serial_ready_read(self) -> None:
        self.rx_buffer.extend(bytes(self.serial.readAll()))

        while b"\n" in self.rx_buffer:
            raw_line, _, remainder = self.rx_buffer.partition(b"\n")
            self.rx_buffer = bytearray(remainder)
            line = raw_line.decode("ascii", errors="ignore").strip()
            if line:
                self.parse_serial_line(line)

    def parse_serial_line(self, line: str) -> None:
        print(f"RX <- ARDUINO: {line}", flush=True)

        parts = [p.strip() for p in line.split(",")]
        if not parts:
            return

        if parts[0] == "DATA" and len(parts) >= 6:
            try:
                t = float(parts[1])
                piezo_v = float(parts[2])
                force_n = float(parts[3])
                position_counts = int(float(parts[4]))
                motor_state = parts[5]
            except ValueError:
                return

            self.add_sample(t, piezo_v, force_n, position_counts, motor_state)

        elif parts[0] == "STATUS":
            self.status_message(", ".join(parts[1:]))

        elif parts[0] == "ERROR":
            self.status_message("Arduino error: " + ", ".join(parts[1:]))

    # =====================================================================
    # Acquisition
    # =====================================================================
    def start_acquisition(self) -> None:
        self.apply_settings_to_runtime()

        if not self.connected:
            QMessageBox.warning(
                self,
                APP_TITLE,
                "Connect to the Arduino first, or enable Demo mode in Settings.",
            )
            return

        self.clear_plot_buffers()
        self.estop_active = False
        self.acquiring = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        if self.save_check.isChecked():
            self.open_csv_file()
        else:
            self.csv_status_label.setText("CSV saving: OFF")

        if self.auto_tare_check.isChecked():
            self.tare_force()

        if self.demo_mode:
            self.demo_t = 0.0
            self.demo_timer.start(max(1, int(1000 / max(1, self.sample_rate_hz))))
        else:
            self.send_command(f"START,{self.sample_rate_hz}")

        self.status_message(f"Acquisition running at {self.sample_rate_hz} Hz")

    def stop_acquisition(self) -> None:
        if not self.acquiring:
            return

        if self.demo_mode:
            self.demo_timer.stop()
        else:
            self.send_command("STOP")

        self.acquiring = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.flush_csv()
        self.close_csv_file()
        self.status_message("Acquisition stopped")

    def clear_plot_buffers(self) -> None:
        self.time_data.clear()
        self.piezo_data.clear()
        self.force_data.clear()
        self.position_data.clear()
        self.piezo_panel.curve.setData([], [])
        self.force_panel.curve.setData([], [])
        self.piezo_panel.metrics.update_values([])
        self.force_panel.metrics.update_values([])

    def add_sample(
        self,
        t: float,
        piezo_v: float,
        force_n: float,
        position_counts: int,
        motor_state: str,
    ) -> None:
        if not self.acquiring:
            return

        self.time_data.append(t)
        self.piezo_data.append(piezo_v)
        self.force_data.append(force_n)
        self.position_data.append(position_counts)

        self.motor_state = motor_state
        self.position_label.setText(f"Relative position: {position_counts} counts")
        self.motor_state_label.setText(f"Motor state: {motor_state}")
        self.last_sample_label.setText(f"Last sample: {t:.3f} s")

        # Keep only the moving window in GUI memory.
        cutoff = t - self.window_seconds
        while self.time_data and self.time_data[0] < cutoff:
            self.time_data.popleft()
            self.piezo_data.popleft()
            self.force_data.popleft()
            self.position_data.popleft()

        if self.csv_writer is not None:
            self.pending_csv_rows.append(
                [f"{t:.6f}", f"{piezo_v:.6f}", f"{force_n:.6f}", position_counts, motor_state]
            )

    def refresh_plots(self) -> None:
        if not self.time_data:
            return

        x = list(self.time_data)
        p = list(self.piezo_data)
        f = list(self.force_data)

        self.piezo_panel.curve.setData(x, p)
        self.force_panel.curve.setData(x, f)

        if len(x) >= 2:
            right = x[-1]
            if right >= self.window_seconds:
                left = right - self.window_seconds
                self.piezo_panel.plot.setXRange(left, right, padding=0)
                self.force_panel.plot.setXRange(left, right, padding=0)
            else:
                self.piezo_panel.plot.setXRange(0, self.window_seconds, padding=0)
                self.force_panel.plot.setXRange(0, self.window_seconds, padding=0)

        self.piezo_panel.metrics.update_values(p)
        self.force_panel.metrics.update_values(f)

    # =====================================================================
    # CSV saving
    # =====================================================================
    def open_csv_file(self) -> None:
        folder = Path(self.save_folder_edit.text()).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = folder / f"shaker_acquisition_{stamp}.csv"
            self.csv_file = path.open("w", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            self.csv_writer.writerow(
                ["time_s", "piezo_voltage_V", "force_N", "position_counts", "motor_state"]
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

        self.csv_writer.writerows(self.pending_csv_rows)
        self.pending_csv_rows.clear()
        self.csv_file.flush()

    def close_csv_file(self) -> None:
        if self.csv_file is not None:
            self.csv_file.close()
        self.csv_file = None
        self.csv_writer = None
        self.pending_csv_rows.clear()

    # =====================================================================
    # Force sensor
    # =====================================================================
    def tare_force(self) -> None:
        if self.demo_mode:
            self.status_message("Demo force tare applied")
        else:
            self.send_command("TARE_FORCE")
            self.status_message("Force tare command sent")

    # =====================================================================
    # Motor control
    # =====================================================================
    def on_speed_changed(self, value: int) -> None:
        # GUI hard limit: motor command can never exceed 5%.
        value = max(0, min(5, value))

        if self.speed_slider.value() != value:
            self.speed_slider.blockSignals(True)
            self.speed_slider.setValue(value)
            self.speed_slider.blockSignals(False)

        self.speed_label.setText(f"{value} %")

        if self.demo_mode:
            return

        self.send_command(f"MOTOR_SPEED,{value}")

    def start_motor_motion(self, direction: str) -> None:
        if self.estop_active:
            return

        # If the user presses UP/DOWN again before a delayed lock occurs,
        # cancel that pending lock so it cannot interrupt the new movement.
        self.lock_delay_timer.stop()

        if not self.connected:
            QMessageBox.warning(self, APP_TITLE, "Connect to the controller first.")
            return

        # GUI hard limit: UP/DOWN commands can never exceed 5%.
        speed = max(0, min(5, self.speed_slider.value()))

        if speed <= 0:
            self.status_message("Motor speed is 0%. Increase the speed command first.")
            return

        if self.invert_direction_check.isChecked():
            direction = "DOWN" if direction == "UP" else "UP"

        if self.demo_mode:
            self.demo_locked = False
            self.demo_direction = 1 if direction == "UP" else -1
        else:
            self.send_command(f"MOTOR_{direction},{speed}")

        self.motor_state = direction
        self.motor_state_label.setText(f"Motor state: {direction}")

    def finish_motor_motion(self) -> None:
        if self.estop_active:
            return

        # Releasing UP/DOWN stops the motor immediately.
        # Automatic position lock is intentionally disabled.
        self.lock_delay_timer.stop()

        if self.demo_mode:
            self.demo_direction = 0
            self.demo_locked = False
        else:
            self.send_command("MOTOR_STOP")

        self.motor_state = "STOPPED"
        self.motor_state_label.setText("Motor state: STOPPED")
        self.status_message("UP/DOWN released: MOTOR_STOP sent")

    def lock_current_position(self) -> None:
        if self.estop_active:
            return

        tolerance = self.lock_tolerance_spin.value()
        self.lock_tolerance_counts = tolerance

        if self.demo_mode:            self.demo_direction = 0
            self.demo_locked = True
        else:
            self.send_command(f"MOTOR_LOCK,{tolerance}")

        self.motor_state = "LOCKED"
        self.motor_state_label.setText("Motor state: LOCKED")
        self.status_message(f"Position lock requested (±{tolerance} counts)")

    def stop_motor(self) -> None:
        # An explicit stop cancels any previously scheduled lock.
        self.lock_delay_timer.stop()

        if self.demo_mode:
            self.demo_direction = 0
            self.demo_locked = False
        else:
            self.send_command("MOTOR_STOP")

        self.motor_state = "STOPPED"
        self.motor_state_label.setText("Motor state: STOPPED")

    def emergency_stop(self) -> None:
        self.estop_active = True
        self.lock_delay_timer.stop()

        if self.demo_mode:
            self.demo_direction = 0
            self.demo_locked = False
        else:
            self.send_command("ESTOP")

        self.motor_state = "E-STOP"
        self.motor_state_label.setText("Motor state: E-STOP")
        self.status_message("EMERGENCY STOP ACTIVE")

        # Stop acquisition too, as requested for the global emergency state.
        if self.acquiring:
            self.stop_acquisition()

    # =====================================================================
    # Demo mode
    # =====================================================================
    def generate_demo_sample(self) -> None:
        if not self.acquiring:
            return

        dt = 1.0 / max(1, self.sample_rate_hz)
        self.demo_t += dt

        # Raw-looking piezo waveform: shaker-like sinusoid + noise + harmonic.
        piezo = (
            1.7
            + 0.55 * math.sin(2 * math.pi * 7.5 * self.demo_t)
            + 0.18 * math.sin(2 * math.pi * 41.0 * self.demo_t)
            + random.gauss(0.0, 0.035)
        )

        # Example force in N.
        force = (
            80.0
            + 20.0 * math.sin(2 * math.pi * 1.4 * self.demo_t)
            + 4.0 * math.sin(2 * math.pi * 8.0 * self.demo_t)
            + random.gauss(0.0, 0.8)
        )
        force = max(0.0, min(450.0, force))

        # Simulated relative motor position.
        if self.demo_direction != 0:
            step = max(1, int(self.speed_slider.value()))
            self.demo_position += self.demo_direction * step
            state = "UP" if self.demo_direction > 0 else "DOWN"
        elif self.demo_locked:
            # Tiny disturbance around locked position.
            disturbance = random.choice([0, 0, 0, 0, 1, -1])
            self.demo_position += disturbance
            state = "LOCKED"
        else:
            state = "STOPPED"

        self.add_sample(self.demo_t, piezo, force, self.demo_position, state)

    # =====================================================================
    # Helpers / close
    # =====================================================================
    def status_message(self, text: str) -> None:
        self.status_label.setText(text)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API name)
        try:
            if self.acquiring:
                self.stop_acquisition()
            if self.serial.isOpen():
                self.send_command("MOTOR_STOP")
                self.serial.close()
            self.flush_csv()
            self.close_csv_file()
        finally:
            event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    pg.setConfigOptions(antialias=False)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
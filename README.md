# Shaker Control System

Windows / Python + Arduino laboratory shaker control and data acquisition.

**Current USB DAQ version (this branch):** [CSU15-45N-USB — full hardware, software and usage guide](CSU15-45N-USB/README.md)

## GUI tabs

**Dashboard - DAQ + Motor:** continuous force and exact oscilloscope screen, START/STOP and SAVE AS, scope controls, plus FAULHABER motor positioning controls (UP/DOWN, BRAKE, POSITION BRAKE, E-STOP).

**Settings:** two connection sections — **DAQ connections** (CSU15-45N force sensor COM and Tektronix MSO4104B VISA) and **Motor controller connection** (Arduino Mega COM).

Open **Settings** to connect hardware, then return to **Dashboard** for normal operation. Connections and worker threads remain active across tabs.

## Hardware in this setup

| Device | Model / purpose |
|---|---|
| Shaker | **TIRA GmbH S504** — mechanical vibration |
| Shaker amplifier | **TIRA BAA 60** — drives the shaker |
| Function generator | Creates the **sinusoidal excitation waveform** fed to the TIRA amplifier; model not specified |
| ICP conditioner | **482A21** — ICP sensor signal conditioning |
| Oscilloscope | **Tektronix MSO4104B** — USB/VISA remote screen and waveform acquisition |
| Calibrated force sensor | **SingleTact CSU15-45N (45 N, USB)** — direct to PC |
| Controller | **Arduino Mega** — position control of auxiliary motor |
| Auxiliary position motor | **FAULHABER 2232 024S** with **IE2-512 encoder** |
| Position motor driver | **L298N H-bridge** |
| DC power source | **Up to 24 V**, for the L298N/FAULHABER motor section |

The **TIRA S504 shaker** and **FAULHABER position motor** are different devices. The 24 V DC supply description refers to the position-control motor, not the TIRA BAA 60 amplifier.

## Run the current application

Open `CSU15-45N-USB/` and double-click `START_GUI.bat` on Windows. Install a VISA backend such as TekVISA or NI-VISA separately. Hardware wiring, oscilloscope control, live viewing, START/STOP RAM recording, and Save As to separate force/scope CSV files are documented in the [full guide](CSU15-45N-USB/README.md).

## Offline CSV viewer

[CSV-Viewer — installation and complete usage guide](CSV-Viewer/README.md) contains the standalone **Shaker CSV Viewer 1.2** for saved force and oscilloscope recordings. It includes raw/filtered plots, frequency-guided maximum/minimum detection, adjustable timing/noise thresholds, zoom-dependent metrics, two-point measurements and vertical Y scrolling. Sparse recordings retain observed extrema with an explicit sampling warning.

Open `CSV-Viewer/`, run `INSTALL_REQUIREMENTS.bat` once, then launch `START_VIEWER.bat`. Browse to `force_sensor.csv` or `oscilloscope.csv` and press **PLOT**. Saved force values in Newtons are preserved, and scope captures remain separate. Analysis runs offline after dependency installation.

## Legacy implementation

The active calibrated force sensor in this branch is **SingleTact CSU15-45N (45 N full scale, USB)**. The root `Arduino/` and `GUI/` files are retained from an earlier Arduino/I2C configuration; for the current **45 N USB force sensor + MSO4104B** setup, use the files in `CSU15-45N-USB/`. The repository `main` branch remains unchanged.


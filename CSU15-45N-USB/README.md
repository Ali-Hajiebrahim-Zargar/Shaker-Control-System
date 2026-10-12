# Shaker Control System — TIRA S504, CSU15-45N and MSO4104B

**Branch:** `csu15-45n-usb` — independent from the legacy `main` implementation.

This project is a Windows/Python GUI for a laboratory **electrodynamic shaker test bench**, combining continuously refreshed force measurements, a Tektronix oscilloscope screen view, acquisition to RAM followed by Save As into separate CSV files, and independent motorized position adjustment via Arduino Mega.

## Hardware inventory

| Equipment / function | Hardware actually used | Role in the experimental setup |
|---|---|---|
| Electrodynamic shaker | **TIRA GmbH S504** | Produces mechanical vibration of the test fixture/sample. |
| Shaker power amplifier | **TIRA BAA 60** | Drives the TIRA S504 with the amplified excitation signal. |
| Signal source | **Function generator** (make/model not specified) | Generates the **sinusoidal waveform** supplied to the shaker power amplifier; sets the excitation waveform/frequency at the instrument. |
| ICP signal conditioner | **482A21** | Conditions the signal from an ICP-compatible transducer (transducer model is not specified) before measurement. |
| Oscilloscope | **Tektronix MSO4104B** | Observes the electrical measurement signals; connects to the PC through its rear **USB Device (USBTMC)** port for PyVISA/SCPI commands, screen PNG images and waveform acquisition. |
| Calibrated force sensor | **SingleTact CSU15-45N**, calibrated **USB** sensor, **45 N** nominal range | Measures the force at the mechanical setup; its SingleTact USB electronics connect **directly to the PC** through a separate COM port. |
| Microcontroller | **Arduino Mega** | Controls the auxiliary motor and reads its encoder. It does **not** acquire CSU15-45N force data in this version. |
| Position-control motor | **FAULHABER 2232 024S** DC motor | Adjusts/holds mechanical position separately from the TIRA vibration shaker. |
| Motor encoder | **FAULHABER IE2-512** | Position feedback for the FAULHABER motor, including the on-demand POSITION BRAKE function. |
| Motor driver | **L298N H-bridge** (Soldered 555048) | Drives the FAULHABER motor under Arduino commands. |
| DC power supply | **Up to 24 V DC** for the motor-driver/motor section | Supplies the **position-control motor** through the L298N; **not** a claim that the TIRA BAA 60 shaker amplifier uses this DC supply. |
| Host computer | **Windows laptop/PC** | Runs the GUI, receives force data, connects to the oscilloscope by USB/VISA and optionally communicates with Arduino via a separate USB COM port. |

The user has not provided the model of the function generator or the ICP transducer itself, so neither is assumed here.

## Experimental signal and control paths

```text
                       VIBRATION / EXCITATION PATH

  Function generator (sinusoidal output)
                |
                v
  TIRA BAA 60 power amplifier
                |
                v
  TIRA GmbH S504 electrodynamic shaker
                |
                v
     Mechanical fixture / device under test
                |
            Force applied
                |
                v
  SingleTact CSU15-45N calibrated USB sensor
                |
       SingleTact USB electronics
                |
             PC (COM)
                |
                v
          Force plot / CSV


                 ICP MEASUREMENT PATH (when fitted)

   ICP-compatible transducer (model not specified)
                |
                v
      ICP signal conditioner 482A21
                |
      Conditioned analog signal
                |
                v
        Tektronix MSO4104B
                |
       Rear USB Device / USBTMC
                |
                v
      PC: PyVISA / scope screen / scope CSV


                MOTOR POSITION CONTROL PATH

              Windows GUI
                  |
            Arduino Mega (USB)
                  |
        L298N motor driver <--- DC supply (up to 24 V)
                  |
          FAULHABER 2232 024S
                  |
         FAULHABER IE2-512 encoder
                  |
       Mega D18 (A), D19 (B)
```

These are **distinct subsystems**: the TIRA S504 is the shaker that generates the sinusoidal mechanical vibration, while the FAULHABER motor is an auxiliary **position-control motor**. Do not confuse the two.

### Oscilloscope and ICP conditioner

- If an ICP output is being measured, connect the ICP sensor to **482A21** and the **conditioned analog output** to a suitable MSO4104B input; use appropriate channel/probe settings.
- Do **not** connect the high-power output of the TIRA BAA 60 directly to a normal oscilloscope input. Use an appropriately rated measurement method/probe when such a measurement is needed.
- The PC-to-scope USB cable goes into the MSO4104B **rear USB Device port**, not a USB Host port.
- Install **TekVISA or NI-VISA** on Windows and Python **PyVISA** for remote acquisition and control.
- The on-screen display is a sequence of original Tektronix PNG **screenshots**, not a zero-latency video stream. A nominal **50 ms request interval** is offered, but actual frame rate is limited by USB/VISA transfer and instrument processing.

### Arduino Mega, L298N and FAULHABER wiring

```text
Arduino Mega D6  -> L298N ENA (remove ENA jumper)
Arduino Mega D7  -> L298N IN1
Arduino Mega D8  -> L298N IN2
Arduino Mega GND -> L298N GND (common ground)

L298N OUT1/OUT2  -> FAULHABER 2232 024S motor
Motor DC supply  -> L298N power input (up to 24 V DC for this setup)

IE2-512 encoder A   -> Mega D18
IE2-512 encoder B   -> Mega D19
IE2-512 encoder +5V -> Mega +5V
IE2-512 encoder GND -> Mega GND
```

**Important:** in the verified L298N configuration powered from the motor supply, **do not connect Arduino 5 V to the L298N 5 V pin**. Common GND is required. The firmware caps motor drive commands at 5%. Verified movement/encoder count mapping:

```text
UP   -> encoder count decreases
DOWN -> encoder count increases
```

Ordinary BRAKE is dynamic electrical braking; **POSITION BRAKE** is an optional encoder-feedback hold (limited motor PWM), and **E-STOP** disables the H-bridge outputs without physically disconnecting the external DC supply.

### Force sensor and synchronization

- **Current sensor: SingleTact CSU15-45N, calibrated USB force sensor, 45 N nominal full scale.** All force readings and CSV force values in this branch use the **45 N** sensor configuration.
- For the **45 N CSU15-45N** sensor, the GUI currently converts digital counts to N as `(raw_counts - baseline_counts) * 45 / 512`; default baseline is 255 counts and TARE sets a new software zero from recent unloaded readings. Verify physical calibration experimentally.
- Force, scope and motor are independent communication tasks; START/STOP define a common **software recording interval**, not a simultaneous hardware trigger.
- Continuous live viewing runs even when RAM recording is stopped. **START SAMPLING** clears both plots and begins a RAM recording; **STOP SAMPLING** resets plots for ongoing live display while keeping the completed recording in RAM until **SAVE AS**.
- SAVE AS creates two files in the selected session directory: `force_sensor.csv` and `oscilloscope.csv`. They are intentionally **separate** and have different column schemas.
- A dedicated motor thread, force thread, scope thread and CSV writer task prevent blocking USB transfers from monopolizing the GUI. Unexported RAM data may still be lost if the application terminates unexpectedly.

## Project files

```text
CSU15-45N-USB/
├── Arduino/
│   └── shaker_controller_CSU15_45N.ino
├── GUI/
│   └── shaker_control_gui_CSU15_45N.py
├── START_GUI.bat
├── requirements.txt
└── README.md
```

Extract the project and double-click **START_GUI.bat** on Windows. It uses the Python 3.12 environment configured in the launcher. Install a VISA backend separately. The legacy root-level `GUI/shaker_control_gui.py` in this repository belongs to an earlier sensor configuration and is **not** the current CSU15-45N application.

## Existing release notes and operating details

### Latest changes: extended timebase and fastest screen refresh

- **Time/div** now includes 10, 15, 20, 30, 40, 45 and 50 seconds/div, followed
  by 1, 1.5, 2, 2.5, 3, 3.5 and **4 minutes/div**. The GUI sends the chosen
  seconds/div value via `HORIZONTAL:MAIN:SCALE` and reads the settings back.
- **AUTO REFRESH SCOPE SCREEN** is enabled by default at the minimum selectable
  **50 ms** refresh-request interval (maximum 5000 ms). This is the fastest
  offered *request interval*, **not a guaranteed 50 ms per screenshot**.
  The actual image refresh rate is limited by the MSO4104B hardcopy rendering,
  USB/VISA transfer, and concurrent waveform transfer for CSV recording.
- The oscilloscope worker continues to handle all scope commands off the GUI
  thread and **coalesces duplicate screen requests**, so using 50 ms cannot
  produce an unlimited backlog or freeze the force/motor workers.
- **MSO4104B limitation:** the available maximum seconds/div can depend on
  record length. Tektronix lists up to 400 s/div for 10 kpoint records and up
  to 1000 s/div with longer records, but only up to 40 s/div for a 1 kpoint
  record. If the scope rejects or adjusts a long timebase, read back the
  instrument settings or adjust record length on the instrument.

### Force zoom and Tektronix LCD image

This standalone Windows version combines:

- SingleTact **CSU15-45N** calibrated USB force sensor
- Tektronix **MSO4104B** through the rear USB Device/USBTMC port and PyVISA
- Arduino Mega + FAULHABER 2232 024S motor / IE2-512 encoder / L298N

It retains the earlier **continuous live monitoring**, START/STOP **RAM recording**, explicit **SAVE AS**, four worker threads, and separate motor controls. This revision removes the reconstructed waveform plot from the display and stabilizes zoom.

## Run

1. Extract the ZIP, keeping the directory structure intact.
2. Install a Windows VISA driver: TekVISA or NI-VISA. The USB cable must go to **USB Device** on the rear of MSO4104B, not a USB Host port.
3. Close the SingleTact manufacturer's app before connecting the CSU15-45N in this GUI.
4. **Double-click `START_GUI.bat`**. It launches the application using the user's Python 3.12 installation, installing missing Python requirements if necessary.
5. Open **Settings**: use **DAQ connections** for force-sensor COM and oscilloscope VISA, and **Motor controller connection** for the Arduino Mega COM port. Return to **Dashboard - DAQ + Motor** for sampling, plots, and motor control.

Requirements are listed in `requirements.txt`: `PySide6`, `pyqtgraph`, `PyVISA`, `pyserial`. A VISA backend is a separate system installation.

## Main UI

### Dashboard - DAQ + Motor

**Force figure**

- The live sensor force curve and current force value appear independently of START/STOP, as long as the force USB device is connected.
- Use mouse wheel to **zoom**, mouse drag to **pan**. The view stops automatic horizontal scrolling when the user manually zooms or pans.
- Click **FOLLOW LIVE** to resume scrolling with new data. START or STOP also intentionally resets the figure and reenables Follow Live.
- Force USB polling is approximately 100 Hz; the **plot is redrawn at 10 Hz**, and peak-preserving downsampling is applied **only to pixels**, not to the RAM recording or exported values.
- Tare averages recent raw unloaded readings. Current force conversion: `(raw - baseline) * 45 / 512 N`, baseline initially `255` raw counts.

**Oscilloscope figure**

- There is **one single view of the exact scope-screen image**; the old *Waveform data* visualization tab has been removed.
- Scope sends the original **PNG screenshot** via `SAVE:IMAGE:FILEFORMAT PNG`, `HARDCOPY:INKSAVER OFF`, `HARDCOPY:LAYOUT LANDSCAPE`, and `HARDCOPY START`. The PC displays the received pixels rather than reconstructing a trace.
- **AUTO REFRESH SCOPE SCREEN** is on by default when a scope connection opens. Set its refresh interval as desired (default 50 ms) or use **REFRESH SCREEN** for a manual snapshot.
- Scroll wheel **zooms** the screenshot, dragging **pans** it; **FIT SCREEN** restores whole-image view. New image frames update the same image item without resetting zoom/pan.
- The panel displays source PNG resolution and the local time it was received.
- A PNG is a snapshot from the instrument, **not a zero-latency video feed**. With time-varying/non-repeatable signals, the trace can differ from what the physical LCD displays at a *later* instant. To compare, freeze the physical scope with its own STOP button and request a fresh screenshot.
- Background `CURVE?` captures continue for numerical measurements and optional CSV recording; these are **not displayed as a second synthetic waveform plot**.
- Ordinary background waveform reads no longer issue `SELECT:CHx ON` and therefore no longer silently change which channels are enabled on the scope screen. Explicit APPLY VERTICAL can still enable the selected channel as intended.

**Scope controls**

- Channel, coupling, V/div, vertical position, timebase, trigger source/slope/level, APPLY, RUN, STOP, SINGLE, RESET SCOPE, AUTOSET, READ SETTINGS, REFRESH SCREEN.
- Scope settings and screenshot/waveform access occur **only inside the dedicated ScopeWorker thread**. Numeric waveform requests and images share the physical VISA session and are serialized.

### Motor control (on Dashboard)

- The first tab contains UP/DOWN, BRAKE, POSITION BRAKE, encoder counts, E-STOP/RESET and speed control. The Arduino Mega COM connection itself is now in **Settings**.
- The Dashboard is vertically scrollable, so all motor controls and both figures remain usable at typical laptop sizes.
- Arduino logic and pin configuration are unchanged; firmware hard-caps motor commands to 5% PWM.

### Settings (connections)

- **DAQ connections:** select/connect/disconnect SingleTact CSU15-45N and MSO4104B.
- **Motor controller connection:** select/connect/disconnect Arduino Mega.
- Live acquisition continues across tab changes.

## Sampling / RAM / SAVE AS

| Action | Live force figure | Live scope PNG view | In-memory recording |
|---|---|---|---|
| Connect devices | Runs | Runs | Not recording |
| START SAMPLING | Clears, restarts live | Clears, requests fresh PNG | Begins force + scope waveform storage |
| During sampling | Runs | Runs | Adds samples in RAM |
| STOP SAMPLING | Clears, restarts live | Clears, requests fresh PNG | Stops but retains completed capture |
| SAVE AS... | Continues live | Continues live | Writes recorded data to disk in background |

- A previously **unsaved** recording blocks a new START so it cannot be overwritten by accident.
- A disk write error retains the RAM recording so Save As can be retried.
- Recorded RAM data can be lost if Windows/the app crashes; save completed sessions.
- Force and oscilloscope have a common PC-monotonic start timestamp; they are **not hardware-trigger synchronized**. USB and scope latency remain.

**CSV output**: After choosing a destination, a separate unique `session_YYYYMMDD_HHMMSS` directory contains:

```
force_sensor.csv
oscilloscope.csv
```

`force_sensor.csv` headers:

```
Time (s),Raw Counts,Force (N),Motor Position (counts),Motor State
```

`oscilloscope.csv` headers:

```
Capture Time (s),Channel,Sample Index,Waveform Time (s),Voltage (V)
```

Each oscilloscope capture supplies one row per voltage sample. `Capture Time` is the PC-relative waveform retrieval start, while `Waveform Time` is the oscilloscope's time-axis offset/increment. Waveform transfers are capped at 10,000 points per capture to keep USB transfers manageable. These CSVs are application exports, not byte-identical copies of manufacturer-created files.

## Worker design

```
Qt main thread       UI events, fast plot rendering, RAM capture buffers
MotorWorker          Arduino serial + motor commands + encoder telemetry
ForceWorker          SingleTact USB/COM polling + force timestamps
ScopeWorker          USBTMC/PyVISA + SCPI + PNG + numeric waveform
CSVWriterWorker      Writes the completed RAM capture after SAVE AS
```

The 10 Hz force plot timer does not define sensor sampling frequency. Image transfer may be slower than the configured screen refresh timer; redundant screen requests are coalesced rather than queued indefinitely.

## Hardware safety

- Arduino Mega: `D6 → L298N ENA`, `D7 → IN1`, `D8 → IN2`.
- Encoder: `A → D18`, `B → D19`, powered from Arduino 5 V/GND.
- The motor supply is external 24 V; Arduino and L298N GND must be common.
- In the verified module configuration, **do not connect Arduino 5 V to L298N 5 V**.
- On this hardware, UP decreases encoder counts and DOWN increases them; POSITION BRAKE relies on that mapping.
- Normal BRAKE is electrical *dynamic* braking, not fixed-position holding. POSITION BRAKE actively restores encoder position on demand.
- E-STOP disables motor drive; it does not disconnect the 24 V supply.
- This version does not yet implement the piezoelectric input channel.

## Troubleshooting and limitations

- **Force COM occupied**: close the official SingleTact program before starting this GUI.
- **No scope in VISA list**: check the rear USB Device port and TekVISA/NI-VISA drivers.
- **Zoom freezes/changes the X range**: run this revised GUI rather than the old waveform-plot version; use FOLLOW LIVE to resume scrolling after a manual zoom.
- **Scope screenshot visually differs from a live LCD**: use the scope's own STOP to freeze the displayed acquisition, then request REFRESH SCREEN. The two displays cannot be guaranteed to show the same instant while RUN is active. Check the connected instrument identity and image dimensions.
- **Scope PNG not updating**: check AUTO REFRESH SCOPE SCREEN and the scope connection/status messages. Large PNGs, other SCPI transfers and USB latency can slow refresh.
- **GUI exception**: inspect `GUI/shaker_gui_error.log`.

**Verification:** Static Python syntax and source-level regression checks have been performed. The USB/VISA interaction and zoom smoothness must be verified on your actual Windows + MSO4104B setup; no physical scope was available to test here.

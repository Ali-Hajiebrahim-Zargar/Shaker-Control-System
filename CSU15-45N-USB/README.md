# Shaker Control System - CSU15-45N USB branch

This branch is a dedicated version of the shaker-control project for the **SingleTact CSU15-45N calibrated USB force sensor**. It keeps the **Arduino Mega only for motor and encoder control**. Force data are read **directly by the Python GUI from the SingleTact USB electronics**.

## Branch

`csu15-45n-usb`

The files for this version are intentionally stored in a separate folder and use different names so they do not overwrite the original CS8-10N / Arduino-force implementation.

## Files

```text
CSU15-45N-USB/
├── Arduino/
│   └── shaker_controller_CSU15_45N.ino
├── GUI/
│   └── shaker_control_gui_CSU15_45N.py
├── requirements.txt
└── README.md
```

## System architecture

```text
                         Windows PC
                            │
                Python / PySide6 GUI
                   │                  │
                   │                  │
          USB / COM port A     USB / COM port B
                   │                  │
        SingleTact CSU15-45N      Arduino Mega
          USB electronics              │
                   │                    ├── L298N H-bridge
                   └── Force data       ├── FAULHABER motor
                                        └── IE2-512 encoder
```

The Arduino is **not used for force acquisition** in this branch.

## Hardware

### Motor and encoder

- Arduino Mega
- FAULHABER 2232 024S DC motor
- FAULHABER IE2-512 encoder
- L298N H-bridge
- External 24 V motor supply

### Force sensor

- SingleTact CSU15-45N calibrated USB sensor
- Nominal force range: 45 N
- Connected directly to the PC through the SingleTact USB electronics

## Wiring

### L298N to Arduino Mega

```text
Mega D6  -> L298N ENA
Mega D7  -> L298N IN1
Mega D8  -> L298N IN2
Mega GND -> L298N GND
```

Motor leads go to L298N OUT1 and OUT2.

The 24 V supply is connected to the L298N motor-supply input.

**Important:** with the present L298N module configuration, do not connect Arduino 5 V to the L298N 5 V pin. Arduino GND and L298N GND must remain common.

### Encoder to Arduino Mega

```text
Encoder A   -> Mega D18
Encoder B   -> Mega D19
Encoder +5V -> Mega 5V
Encoder GND -> Mega GND
```

Verified direction mapping for this physical system:

```text
UP   -> encoder count decreases
DOWN -> encoder count increases
```

The POSITION BRAKE algorithm is written for this mapping.

### CSU15-45N

The CSU15-45N is connected only by USB to the Windows PC.

There is no I2C wiring from this sensor to the Arduino in this branch.

## Software requirements

Python dependencies:

```text
PySide6>=6.7
pyqtgraph>=0.13
```

Install them with:

```bash
python -m pip install -r CSU15-45N-USB/requirements.txt
```

Run the GUI with:

```bash
python CSU15-45N-USB/GUI/shaker_control_gui_CSU15_45N.py
```

Upload the Arduino sketch:

```text
CSU15-45N-USB/Arduino/shaker_controller_CSU15_45N.ino
```

to the Arduino Mega.

## Two independent COM ports

The GUI uses two serial ports at the same time:

1. **Motor controller COM port**: Arduino Mega.
2. **Force sensor COM port**: SingleTact CSU15-45N USB electronics.

Both use 115200 baud.

Do not select the same COM port for both devices.

The official SingleTact application must be closed before this GUI connects to the force-sensor COM port because Windows normally allows only one application to own a serial port at a time.

## SingleTact USB initialization

The GUI reproduces the important initialization sequence used by the official SingleTact NETInterface driver:

1. Open the force-sensor COM port at 115200 baud.
2. Clear receive/transmit buffers.
3. Set DTR HIGH.
4. Wait approximately 10 ms.
5. Set DTR LOW.
6. Wait approximately 2 s for the USB electronics to restart.
7. Set RTS HIGH.
8. Clear the buffers again.
9. Begin binary command/response polling.

This initialization was added because simply opening the COM port and polling immediately did not reliably produce data even though the official SingleTact application worked.

## SingleTact binary protocol used by the GUI

The GUI follows the protocol structure used by SingleTact's official NETInterface implementation.

Main parameters:

```text
Baud rate:        115200
I2C address:      0x04
Read command:     0x01
Read location:    128
Read length:      6 bytes
Polling interval: 10 ms target
Reply timeout:    150 ms
```

The outgoing command uses the official serial command structure. The parser accepts the SingleTact USB response header as well as the interface-style header used by the official software.

The six sensor bytes contain an iteration counter and the force-sensor output. Duplicate frames with the same iteration counter are ignored.

## Force conversion

The GUI currently uses:

```text
Force [N] = (RawCounts - BaselineCounts) * 45 / 512
```

Constants:

```text
Sensor rating = 45 N
Full-scale digital span = 512 counts
Default baseline = 255 counts
```

The live GUI shows both:

- raw sensor counts
- converted force in N

## Force tare

`TARE FORCE` is a software tare in the GUI.

The sensor should be completely unloaded before pressing the button.

The GUI:

1. keeps recent valid raw USB samples,
2. requires at least 10 recent samples,
3. uses up to the most recent 20 samples,
4. averages them,
5. stores that mean as the new baseline.

After tare, the live graph is cleared and restarted because old graph points were calculated with the previous baseline.

`RESET FACTORY ZERO` restores the software baseline to 255 counts and also restarts the live graph.

## Live force graph

The graph is intentionally independent of CSV recording.

As soon as the force sensor is connected and valid frames are received:

- `Live force` updates continuously,
- the plot updates continuously,
- both use exactly the same converted `force_n` value.

Therefore the numeric live value and the last plotted point should represent the same sample stream.

`START ACQUISITION` does **not** start the graph. It only starts recording to CSV.

`STOP ACQUISITION` stops CSV recording but the live graph continues.

## CSV recording

When CSV saving is enabled and `START ACQUISITION` is pressed, the GUI records:

```text
time_s
force_raw_counts
force_N
motor_position_counts
motor_state
```

The recording time starts from zero for each acquisition.

The force plot has its own continuous live-time axis and does not reset when CSV recording starts.

## Motor control

### Speed

Both the GUI and the Arduino firmware limit motor commands to a maximum of 5%.

The Arduino independently enforces the limit, so a larger GUI or serial command cannot exceed the firmware cap.

### UP / DOWN

`UP` and `DOWN` are press-and-hold controls.

While the button is pressed, the motor is driven in the requested direction.

When the button is released, the GUI sends `MOTOR_BRAKE`.

Manual UP or DOWN motion cancels POSITION BRAKE.

## Normal BRAKE

Normal BRAKE is **dynamic electrical braking**, not active position control.

The firmware sets both direction inputs LOW and enables the bridge so the motor terminals are electrically shorted through the H-bridge.

This produces resistance that depends on motor-generated back-EMF. It is not equivalent to a mechanical holding brake, and its holding torque becomes weak near zero speed.

## POSITION BRAKE

POSITION BRAKE is the active encoder-based hold mode.

When enabled:

1. Arduino captures the current encoder count as the target.
2. Arduino continuously reads the encoder.
3. If the actual count moves outside the allowed tolerance, the motor is driven back toward the target.
4. Once the count returns inside the tolerance band, dynamic electrical braking is applied.

Because the verified direction mapping is:

```text
UP   -> count decreases
DOWN -> count increases
```

the correction logic is:

```text
current < target -> drive DOWN
current > target -> drive UP
inside tolerance -> dynamic brake
```

The correction command is also limited to approximately 5% PWM.

The default tolerance in the GUI is 2 encoder counts and can be changed.

## Emergency stop

`EMERGENCY STOP` behaves differently from normal BRAKE.

For E-STOP the Arduino disables the H-bridge ENA output and places the motor in a coast/output-disabled state.

Motor commands are rejected while E-STOP is active.

Use `RESET E-STOP` before moving the motor again. Reset returns the system to normal BRAKED state.

## Arduino serial protocol

The motor controller sends telemetry at approximately 50 Hz:

```text
MOTOR_DATA,<millis>,<encoder_count>,<motor_state>
```

Motor states include:

```text
BRAKED
UP
DOWN
POSITION_HOLD
ESTOP
```

Supported GUI-to-Arduino commands:

```text
MOTOR_UP,<percent>
MOTOR_DOWN,<percent>
MOTOR_BRAKE
MOTOR_STOP
MOTOR_POSITION_BRAKE,<tolerance_counts>
MOTOR_POSITION_RELEASE
SET_LOCK_TOL,<tolerance_counts>
ESTOP
RESET_ESTOP
GET_STATUS
```

Firmware responses use `STATUS,...` and `ERROR,...` messages.

## Startup behavior

On Arduino startup:

- motor pins are configured,
- encoder interrupt is attached,
- normal electrical BRAKE is engaged,
- the initial mode is `BRAKED`,
- motor/encoder telemetry begins automatically.

Force acquisition is completely independent of Arduino startup.

## Troubleshooting

### Official SingleTact app works but this GUI receives no force data

- Close the official SingleTact application completely.
- Refresh COM ports.
- Select the correct SingleTact COM port.
- Connect again and allow the approximately 2 s USB initialization delay.
- Confirm that the GUI status changes from waiting for data to receiving data.
- Make sure the Arduino COM port was not accidentally selected as the force COM port.

### GUI connects to force sensor but Live force does not move

Check the raw-count label first.

- If raw counts change but N does not look correct, investigate the baseline/scaling.
- If raw counts do not change, confirm the correct COM port and that the official SingleTact application is closed.
- Use `TARE FORCE` only while completely unloaded.

### Live number and graph disagree

They should not disagree in this version. Both are generated from the same `force_n` sample.

If a different zero was applied with TARE or RESET, the graph is automatically cleared so values calculated using different baselines are not mixed.

### Motor moves correctly but POSITION BRAKE pushes in the wrong direction

This firmware assumes the verified mapping:

```text
UP   -> encoder count decreases
DOWN -> encoder count increases
```

If motor or encoder wiring changes in the future, that mapping must be checked again before using POSITION BRAKE.

### Motor can still be turned by hand in ordinary BRAKE mode

That is expected for dynamic electrical braking. It is strongest when the motor is moving and produces back-EMF. Use POSITION BRAKE when active encoder-based position restoration is required.

## Current scope

This branch currently implements:

- CSU15-45N direct USB force acquisition
- live force display
- live force plot
- software tare
- CSV recording
- Arduino motor control
- encoder telemetry
- dynamic brake
- encoder-based POSITION BRAKE
- E-STOP / reset

The piezoelectric acquisition channel is not integrated in this CSU15-45N branch.

## Safety notes

- The motor is powered from an external 24 V supply.
- Keep Arduino and L298N grounds common.
- Do not connect Arduino 5 V to the L298N 5 V pin in the verified module configuration.
- Firmware motor drive is intentionally capped at 5%.
- POSITION BRAKE actively drives the motor when external force moves the shaft away from the target; keep hands and mechanical parts clear of pinch points.
- E-STOP disables drive output but does not physically disconnect the 24 V supply.

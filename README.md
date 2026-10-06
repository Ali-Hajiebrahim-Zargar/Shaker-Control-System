# Shaker Control System

Arduino Mega + Python GUI control and data-acquisition system for the laboratory shaker setup.

## Current hardware

- Arduino Mega
- FAULHABER 2232 024S motor
- FAULHABER IE2-512 encoder
- L298N H-bridge motor driver
- SingleTact CS8-10N calibrated force sensor with its matched electronics board
- External 24 V motor supply

## Current behavior

- Motor UP/DOWN control from the GUI
- Motion commands limited to a maximum of 5% in both GUI and Arduino firmware
- Releasing UP/DOWN engages active electrical braking
- L298N remains powered from the 24 V supply during normal braking
- ESTOP disables ENA and uses coast behavior
- Encoder position feedback
- Manual position lock
- SingleTact I2C address 0x04
- Real-time force plot in Newtons
- Manual force tare
- No automatic force tare at Arduino startup or acquisition START
- Piezo channel is reserved but not integrated yet

## Wiring

### L298N to Arduino Mega

```text
Mega D6  -> L298N ENA
Mega D7  -> L298N IN1
Mega D8  -> L298N IN2
Mega GND -> L298N GND
```

Motor -> L298N OUT1/OUT2.

The L298N motor supply remains connected to 24 V.

Important: do not connect Arduino 5V to the L298N 5V pin in the current module configuration. Grounds must remain common.

### Encoder

```text
Encoder A   -> Mega D18
Encoder B   -> Mega D19
Encoder +5V -> Mega 5V
Encoder GND -> Mega GND
```

### SingleTact CS8-10N

```text
SingleTact Pin 1 -> Mega 5V
SingleTact Pin 8 -> Mega GND
SingleTact Pin 3 -> Mega D21 / SCL
SingleTact Pin 6 -> Mega D20 / SDA
```

Default I2C address: `0x04`.

## Force conversion

The firmware follows the SingleTact data format by reading register 128 and using the fixed digital offset `0xFF = 255`.

For the CS8-10N:

```text
force_N = (raw - 255 - manual_tare_offset) * 10 / 512
```

The calibrated factory baseline is preserved by default. Manual TARE should only be pressed when the sensor is completely unloaded.

A 100 g mass produces approximately 0.981 N under standard gravity.

## Repository layout

```text
Arduino/shaker_controller.ino
GUI/shaker_control_gui.py
requirements.txt
README.md
```

## Python dependencies

```bash
python -m pip install -r requirements.txt
```

## Run GUI

```bash
python GUI/shaker_control_gui.py
```

Serial baud rate: `115200`.

## Telemetry

```text
DATA,time_s,piezo_v,force_n,position_counts,motor_state
```

Combined GUI telemetry is currently limited to 100 Hz for the force-sensor integration.

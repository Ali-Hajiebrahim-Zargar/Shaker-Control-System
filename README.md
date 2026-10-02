# Shaker Control System

Arduino Mega + Python GUI control and data-acquisition system for a laboratory shaker setup.

## Verified hardware

- Arduino Mega
- FAULHABER 2232 024S motor
- FAULHABER IE2-512 encoder
- L298N H-bridge motor driver
- SingleTact CS15-450N force sensor with SingleTact electronics board
- External 24 V motor supply

## Current verified functionality

- Motor UP/DOWN control from the GUI
- Immediate motor stop when UP/DOWN is released
- GUI motor command limited to 5%
- Arduino firmware independently clamps motor commands to 5%
- Encoder position feedback
- Manual position lock
- SingleTact detected at I2C address 0x04
- Force tare
- Real-time force graph in the GUI
- CSV acquisition framework
- Piezo channel reserved but not connected yet

## Wiring

### L298N to Arduino Mega

```text
Mega D6  -> L298N ENA
Mega D7  -> L298N IN1
Mega D8  -> L298N IN2
Mega GND -> L298N GND
```

Motor -> L298N OUT1/OUT2.

The L298N motor supply is 24 V.

Important: with the current module configuration, do not connect Arduino 5V to the L298N 5V pin. The grounds must remain common.

### Encoder

```text
Encoder A -> Mega D18
Encoder B -> Mega D19
Encoder +5V -> Mega 5V
Encoder GND -> Mega GND
```

### SingleTact CS15-450N electronics board

```text
SingleTact Pin 1 -> Mega 5V
SingleTact Pin 8 -> Mega GND
SingleTact Pin 3 -> Mega D21 / SCL
SingleTact Pin 6 -> Mega D20 / SDA
```

Default I2C address: `0x04`.

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

Use 115200 baud.

## Serial telemetry

```text
DATA,time_s,piezo_v,force_n,position_counts,motor_state
```

The force sensor is currently transmitted at up to 100 Hz. The piezo field is currently 0 until the piezo sensor is integrated.

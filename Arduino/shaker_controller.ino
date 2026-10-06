#include <Wire.h>

// ============================================================
// Shaker Control System - Arduino Mega
//
// Hardware
// - FAULHABER 2232 024S motor
// - L298N H-bridge
// - FAULHABER IE2-512 encoder
// - SingleTact CS8-10N calibrated force sensor + matched electronics
//
// Serial baud: 115200
//
// Motor / L298N
// D6  -> ENA
// D7  -> IN1
// D8  -> IN2
//
// Encoder
// D18 -> A
// D19 -> B
//
// SingleTact I2C
// D20 -> SDA
// D21 -> SCL
//
// Normal idle behavior
// - L298N remains powered from the 24 V motor supply.
// - When UP/DOWN is not pressed, the motor is actively electrically braked.
// - Braking uses ENA=HIGH with IN1=LOW and IN2=LOW, which makes both
//   L298N motor outputs LOW and dynamically brakes the motor.
// - ESTOP is different: it disables ENA and leaves the motor output floating.
//
// Safety
// - Motion commands are clamped to a maximum of 5% in firmware.
// ============================================================

// -------------------- Motor pins --------------------
const uint8_t MOTOR_ENA = 6;
const uint8_t MOTOR_IN1 = 7;
const uint8_t MOTOR_IN2 = 8;

// -------------------- Encoder pins --------------------
const uint8_t ENCODER_A = 18;
const uint8_t ENCODER_B = 19;
volatile long encoderCount = 0;

// -------------------- SingleTact --------------------
const uint8_t SINGLETACT_I2C_ADDRESS = 0x04;
const float FORCE_FULL_SCALE_N = 10.0f;
const float FORCE_COUNTS_FULL_SCALE = 512.0f;
const float FORCE_FACTORY_ZERO_COUNTS = 255.0f;  // official SingleTact offset (0xFF)

// Software tare is applied on top of the calibrated sensor/electronics pair.
float forceTareOffsetCounts = 0.0f;
float latestForceN = 0.0f;

// -------------------- Motor safety --------------------
const int MAX_MOTOR_PERCENT = 5;

// -------------------- Motor state --------------------
enum MotorMode
{
  MODE_BRAKED,
  MODE_UP,
  MODE_DOWN,
  MODE_LOCKED,
  MODE_ESTOP
};

MotorMode motorMode = MODE_BRAKED;

// -------------------- Position lock --------------------
long targetPosition = 0;
int lockToleranceCounts = 2;
const int LOCK_PWM = 13;  // about 5% of 255

// -------------------- Acquisition --------------------
bool acquisitionRunning = false;
unsigned long acquisitionStartMicros = 0;
unsigned long lastTelemetryMicros = 0;
int telemetryRateHz = 100;
unsigned long telemetryIntervalMicros = 10000UL;

// -------------------- Serial input --------------------
String serialCommand = "";


// ============================================================
// Helpers
// ============================================================

const char* motorStateName()
{
  switch (motorMode)
  {
    case MODE_BRAKED: return "BRAKED";
    case MODE_UP:     return "UP";
    case MODE_DOWN:   return "DOWN";
    case MODE_LOCKED: return "LOCKED";
    case MODE_ESTOP:  return "ESTOP";
    default:          return "UNKNOWN";
  }
}

int percentToPWM(int percent)
{
  percent = constrain(percent, 0, MAX_MOTOR_PERCENT);
  return (int)(((float)percent / 100.0f) * 255.0f + 0.5f);
}

long getEncoderCount()
{
  noInterrupts();
  long value = encoderCount;
  interrupts();
  return value;
}


// ============================================================
// Motor control
// ============================================================

void motorCoast()
{
  // Used for ESTOP: H-bridge motor outputs are disabled.
  analogWrite(MOTOR_ENA, 0);
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);
}

void motorBrake()
{
  // Avoid switching both direction inputs while ENA is active.
  analogWrite(MOTOR_ENA, 0);
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);

  // ENA fully enabled with both inputs LOW => both outputs LOW.
  // The motor terminals are electrically shorted through the bridge,
  // producing dynamic braking while the 24 V supply remains connected.
  analogWrite(MOTOR_ENA, 255);
}

void motorUpPWM(int pwm)
{
  if (motorMode == MODE_ESTOP)
    return;

  pwm = constrain(pwm, 0, percentToPWM(MAX_MOTOR_PERCENT));

  analogWrite(MOTOR_ENA, 0);
  digitalWrite(MOTOR_IN1, HIGH);
  digitalWrite(MOTOR_IN2, LOW);
  analogWrite(MOTOR_ENA, pwm);
}

void motorDownPWM(int pwm)
{
  if (motorMode == MODE_ESTOP)
    return;

  pwm = constrain(pwm, 0, percentToPWM(MAX_MOTOR_PERCENT));

  analogWrite(MOTOR_ENA, 0);
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, HIGH);
  analogWrite(MOTOR_ENA, pwm);
}


// ============================================================
// Encoder
// ============================================================

void encoderISR()
{
  if (digitalRead(ENCODER_A) == digitalRead(ENCODER_B))
    encoderCount++;
  else
    encoderCount--;
}


// ============================================================
// SingleTact force sensor
// ============================================================

int readSingleTactRaw()
{
  const uint8_t packetLength = 6;
  uint8_t outgoingBuffer[3];
  uint8_t incomingBuffer[packetLength];

  // Official SingleTact read protocol:
  // command 0x01, register 128, read 6 bytes.
  outgoingBuffer[0] = 0x01;
  outgoingBuffer[1] = 128;
  outgoingBuffer[2] = packetLength;

  Wire.beginTransmission(SINGLETACT_I2C_ADDRESS);
  Wire.write(outgoingBuffer, 3);
  uint8_t error = Wire.endTransmission();

  if (error != 0)
    return -1;

  Wire.requestFrom(SINGLETACT_I2C_ADDRESS, packetLength);

  unsigned long start = micros();
  uint8_t count = 0;

  while (count < packetLength)
  {
    if (Wire.available())
    {
      incomingBuffer[count] = Wire.read();
      count++;
    }

    if (micros() - start > 5000UL)
      return -1;
  }

  // Bytes 4 and 5 contain the 16-bit sensor reading, big-endian.
  return ((int)incomingBuffer[4] << 8) | incomingBuffer[5];
}

float rawToNewton(int rawCounts)
{
  // The official SingleTact .NET code subtracts the fixed digital offset
  // 0xFF (=255) from the raw reading. For the 10 N range, the useful
  // calibrated span is 0..512 counts.
  //
  // Do NOT automatically re-zero this signal during startup/acquisition.
  // A software tare is applied only when the user explicitly presses TARE.
  float correctedCounts =
      (float)rawCounts
      - FORCE_FACTORY_ZERO_COUNTS
      - forceTareOffsetCounts;

  return correctedCounts * FORCE_FULL_SCALE_N / FORCE_COUNTS_FULL_SCALE;
}

void updateForce()
{
  int raw = readSingleTactRaw();

  if (raw >= 0)
    latestForceN = rawToNewton(raw);
}

bool tareForceSensor()
{
  const int samples = 30;
  long total = 0;
  int valid = 0;

  for (int i = 0; i < samples; i++)
  {
    int raw = readSingleTactRaw();

    if (raw >= 0)
    {
      total += raw;
      valid++;
    }

    delay(10);
  }

  if (valid == 0)
  {
    Serial.println("ERROR,FORCE_TARE_FAILED");
    return false;
  }

  float averageRaw = (float)total / (float)valid;
  forceTareOffsetCounts = averageRaw - FORCE_FACTORY_ZERO_COUNTS;
  latestForceN = 0.0f;

  Serial.print("STATUS,FORCE_TARE_OK,");
  Serial.print(averageRaw, 2);
  Serial.print(",OFFSET,");
  Serial.println(forceTareOffsetCounts, 2);

  return true;
}


// ============================================================
// Manual encoder position lock
// ============================================================

void updatePositionLock()
{
  if (motorMode != MODE_LOCKED)
    return;

  long currentPosition = getEncoderCount();
  long error = targetPosition - currentPosition;

  if (error > lockToleranceCounts)
  {
    motorUpPWM(LOCK_PWM);
  }
  else if (error < -lockToleranceCounts)
  {
    motorDownPWM(LOCK_PWM);
  }
  else
  {
    // Inside the allowed position band, electrically brake the motor.
    motorBrake();
  }
}


// ============================================================
// Telemetry to GUI
// ============================================================

void sendTelemetry()
{
  updateForce();

  unsigned long now = micros();
  float timeSeconds = (now - acquisitionStartMicros) / 1000000.0f;

  // Piezo is not integrated yet.
  float piezoVoltage = 0.0f;

  long position = getEncoderCount();

  Serial.print("DATA,");
  Serial.print(timeSeconds, 6);
  Serial.print(",");
  Serial.print(piezoVoltage, 6);
  Serial.print(",");
  Serial.print(latestForceN, 4);
  Serial.print(",");
  Serial.print(position);
  Serial.print(",");
  Serial.println(motorStateName());
}


// ============================================================
// Serial command parser
// ============================================================

void processCommand(String command)
{
  command.trim();

  // ----------------------------------------------------------
  // Motor speed setting acknowledgement
  // ----------------------------------------------------------
  if (command.startsWith("MOTOR_SPEED,"))
  {
    int speed = command.substring(12).toInt();
    speed = constrain(speed, 0, MAX_MOTOR_PERCENT);

    Serial.print("STATUS,MOTOR_SPEED,");
    Serial.println(speed);
    return;
  }

  // ----------------------------------------------------------
  // Motor UP
  // ----------------------------------------------------------
  if (command.startsWith("MOTOR_UP,"))
  {
    if (motorMode == MODE_ESTOP)
    {
      Serial.println("ERROR,ESTOP_ACTIVE");
      return;
    }

    int speed = command.substring(9).toInt();
    speed = constrain(speed, 0, MAX_MOTOR_PERCENT);

    motorMode = MODE_UP;
    motorUpPWM(percentToPWM(speed));

    Serial.print("STATUS,MOTOR_UP,");
    Serial.println(speed);
    return;
  }

  // ----------------------------------------------------------
  // Motor DOWN
  // ----------------------------------------------------------
  if (command.startsWith("MOTOR_DOWN,"))
  {
    if (motorMode == MODE_ESTOP)
    {
      Serial.println("ERROR,ESTOP_ACTIVE");
      return;
    }

    int speed = command.substring(11).toInt();
    speed = constrain(speed, 0, MAX_MOTOR_PERCENT);

    motorMode = MODE_DOWN;
    motorDownPWM(percentToPWM(speed));

    Serial.print("STATUS,MOTOR_DOWN,");
    Serial.println(speed);
    return;
  }

  // ----------------------------------------------------------
  // Electrical brake
  // ----------------------------------------------------------
  if (command == "MOTOR_BRAKE" || command == "MOTOR_STOP")
  {
    // MOTOR_STOP is retained as a backward-compatible alias.
    if (motorMode == MODE_ESTOP)
    {
      motorCoast();
      Serial.println("STATUS,ESTOP_ACTIVE");
      return;
    }

    motorBrake();
    motorMode = MODE_BRAKED;

    Serial.println("STATUS,MOTOR_BRAKED");
    return;
  }

  // ----------------------------------------------------------
  // Manual position lock
  // ----------------------------------------------------------
  if (command.startsWith("MOTOR_LOCK,"))
  {
    if (motorMode == MODE_ESTOP)
    {
      Serial.println("ERROR,ESTOP_ACTIVE");
      return;
    }

    int tolerance = command.substring(11).toInt();
    tolerance = max(0, tolerance);

    lockToleranceCounts = tolerance;
    targetPosition = getEncoderCount();
    motorMode = MODE_LOCKED;
    motorBrake();

    Serial.print("STATUS,MOTOR_LOCKED,");
    Serial.println(targetPosition);
    return;
  }

  // ----------------------------------------------------------
  // Lock tolerance
  // ----------------------------------------------------------
  if (command.startsWith("SET_LOCK_TOL,"))
  {
    int tolerance = command.substring(13).toInt();
    lockToleranceCounts = max(0, tolerance);

    Serial.print("STATUS,LOCK_TOLERANCE,");
    Serial.println(lockToleranceCounts);
    return;
  }

  // ----------------------------------------------------------
  // Force tare
  // ----------------------------------------------------------
  if (command == "TARE_FORCE")
  {
    tareForceSensor();
    return;
  }

  // ----------------------------------------------------------
  // Start acquisition
  // ----------------------------------------------------------
  if (command.startsWith("START,"))
  {
    int requestedRate = command.substring(6).toInt();

    if (requestedRate < 1)
      requestedRate = 1;

    // The SingleTact electronics update at >120 Hz.
    // Keep combined GUI telemetry at 100 Hz for reliable serial transfer.
    telemetryRateHz = min(requestedRate, 100);
    telemetryIntervalMicros = 1000000UL / telemetryRateHz;

    acquisitionStartMicros = micros();
    lastTelemetryMicros = acquisitionStartMicros;
    acquisitionRunning = true;

    Serial.print("STATUS,ACQUISITION_STARTED,");
    Serial.println(telemetryRateHz);
    return;
  }

  // ----------------------------------------------------------
  // Stop acquisition only; motor remains electrically braked/controlled
  // ----------------------------------------------------------
  if (command == "STOP")
  {
    acquisitionRunning = false;
    Serial.println("STATUS,ACQUISITION_STOPPED");
    return;
  }

  // ----------------------------------------------------------
  // Emergency stop
  // ----------------------------------------------------------
  if (command == "ESTOP")
  {
    // Unlike normal idle braking, ESTOP disables the H-bridge output.
    motorCoast();
    motorMode = MODE_ESTOP;
    acquisitionRunning = false;

    Serial.println("STATUS,EMERGENCY_STOP");
    return;
  }

  Serial.print("ERROR,UNKNOWN_COMMAND,");
  Serial.println(command);
}

void readSerialCommands()
{
  while (Serial.available())
  {
    char c = Serial.read();

    if (c == '\n')
    {
      if (serialCommand.length() > 0)
      {
        processCommand(serialCommand);
        serialCommand = "";
      }
    }
    else if (c != '\r')
    {
      serialCommand += c;

      if (serialCommand.length() > 100)
        serialCommand = "";
    }
  }
}


// ============================================================
// Setup
// ============================================================

void setup()
{
  Serial.begin(115200);

  pinMode(MOTOR_ENA, OUTPUT);
  pinMode(MOTOR_IN1, OUTPUT);
  pinMode(MOTOR_IN2, OUTPUT);

  // Normal idle state is active electrical braking.
  motorBrake();
  motorMode = MODE_BRAKED;

  pinMode(ENCODER_A, INPUT_PULLUP);
  pinMode(ENCODER_B, INPUT_PULLUP);

  attachInterrupt(
    digitalPinToInterrupt(ENCODER_A),
    encoderISR,
    CHANGE
  );

  Wire.begin();
  Wire.setClock(100000);

  delay(500);

  // Keep the calibrated factory zero. No automatic software tare is done here.
  // Manual TARE remains available from the GUI and must only be used unloaded.
  forceTareOffsetCounts = 0.0f;
  latestForceN = 0.0f;

  Serial.println("STATUS,SYSTEM_READY,BRAKED,CS8-10N,FACTORY_ZERO");
}


// ============================================================
// Main loop
// ============================================================

void loop()
{
  readSerialCommands();
  updatePositionLock();

  if (acquisitionRunning)
  {
    unsigned long now = micros();

    if (now - lastTelemetryMicros >= telemetryIntervalMicros)
    {
      lastTelemetryMicros += telemetryIntervalMicros;
      sendTelemetry();
    }
  }
}
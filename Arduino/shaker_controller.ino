#include <Wire.h>

// ============================================================
// Shaker Control System
// Arduino Mega
//
// Hardware:
// - FAULHABER motor
// - L298N motor driver
// - IE2-512 encoder
// - SingleTact CS15-450N force sensor
//
// Serial baud: 115200
//
// Motor:
// D6  -> ENA
// D7  -> IN1
// D8  -> IN2
//
// Encoder:
// D18 -> A
// D19 -> B
//
// SingleTact:
// SDA -> D20
// SCL -> D21
//
// IMPORTANT:
// Maximum motor command = 5%
// ============================================================

const int MOTOR_ENA = 6;
const int MOTOR_IN1 = 7;
const int MOTOR_IN2 = 8;

const int ENCODER_A = 18;
const int ENCODER_B = 19;
volatile long encoderCount = 0;

const byte SINGLETTACT_ADDRESS = 0x04;
const float SENSOR_RATING_N = 450.0;
float forceBaselineCounts = 255.0;
float latestForceN = 0.0;

const int MAX_MOTOR_PERCENT = 5;

enum MotorMode
{
  MODE_STOPPED,
  MODE_UP,
  MODE_DOWN,
  MODE_LOCKED,
  MODE_ESTOP
};

MotorMode motorMode = MODE_STOPPED;

long targetPosition = 0;
int lockToleranceCounts = 2;
const int LOCK_PWM = 13;

bool acquisitionRunning = false;
unsigned long acquisitionStartMicros = 0;
unsigned long lastTelemetryMicros = 0;
int telemetryRateHz = 100;
unsigned long telemetryIntervalMicros = 10000;

String serialCommand = "";

const char* motorStateName()
{
  switch (motorMode)
  {
    case MODE_STOPPED: return "STOPPED";
    case MODE_UP: return "UP";
    case MODE_DOWN: return "DOWN";
    case MODE_LOCKED: return "LOCKED";
    case MODE_ESTOP: return "ESTOP";
    default: return "UNKNOWN";
  }
}

int percentToPWM(int percent)
{
  percent = constrain(percent, 0, MAX_MOTOR_PERCENT);
  float pwm = ((float)percent / 100.0) * 255.0;
  return (int)(pwm + 0.5);
}

void motorUpPWM(int pwm)
{
  if (motorMode == MODE_ESTOP) return;
  pwm = constrain(pwm, 0, percentToPWM(MAX_MOTOR_PERCENT));
  digitalWrite(MOTOR_IN1, HIGH);
  digitalWrite(MOTOR_IN2, LOW);
  analogWrite(MOTOR_ENA, pwm);
}

void motorDownPWM(int pwm)
{
  if (motorMode == MODE_ESTOP) return;
  pwm = constrain(pwm, 0, percentToPWM(MAX_MOTOR_PERCENT));
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, HIGH);
  analogWrite(MOTOR_ENA, pwm);
}

void motorStop()
{
  analogWrite(MOTOR_ENA, 0);
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);
}

void encoderISR()
{
  if (digitalRead(ENCODER_A) == digitalRead(ENCODER_B)) encoderCount++;
  else encoderCount--;
}

long getEncoderCount()
{
  noInterrupts();
  long value = encoderCount;
  interrupts();
  return value;
}

int readSingleTactRaw()
{
  const byte packetLength = 6;
  byte outgoingBuffer[3];
  byte incomingBuffer[packetLength];

  outgoingBuffer[0] = 0x01;
  outgoingBuffer[1] = 128;
  outgoingBuffer[2] = packetLength;

  Wire.beginTransmission(SINGLETTACT_ADDRESS);
  Wire.write(outgoingBuffer, 3);
  byte error = Wire.endTransmission();

  if (error != 0) return -1;

  Wire.requestFrom((uint8_t)SINGLETTACT_ADDRESS, (uint8_t)packetLength);

  unsigned long start = micros();
  byte count = 0;

  while (count < packetLength)
  {
    if (Wire.available())
    {
      incomingBuffer[count] = Wire.read();
      count++;
    }

    if (micros() - start > 5000) return -1;
  }

  int raw = ((int)incomingBuffer[4] << 8) | incomingBuffer[5];
  return raw;
}

float rawToNewton(int rawCounts)
{
  float forceN = ((float)rawCounts - forceBaselineCounts) / 512.0 * SENSOR_RATING_N;
  if (forceN < 0.0) forceN = 0.0;
  return forceN;
}

void updateForce()
{
  int raw = readSingleTactRaw();
  if (raw >= 0) latestForceN = rawToNewton(raw);
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

  forceBaselineCounts = (float)total / (float)valid;
  latestForceN = 0.0;

  Serial.print("STATUS,FORCE_TARE_OK,");
  Serial.println(forceBaselineCounts, 2);
  return true;
}

void updatePositionLock()
{
  if (motorMode != MODE_LOCKED) return;

  long currentPosition = getEncoderCount();
  long error = targetPosition - currentPosition;

  if (error > lockToleranceCounts) motorUpPWM(LOCK_PWM);
  else if (error < -lockToleranceCounts) motorDownPWM(LOCK_PWM);
  else motorStop();
}

void sendTelemetry()
{
  updateForce();

  unsigned long now = micros();
  float timeSeconds = (now - acquisitionStartMicros) / 1000000.0;

  // Piezo is not connected yet.
  float piezoVoltage = 0.0;

  long position = getEncoderCount();

  Serial.print("DATA,");
  Serial.print(timeSeconds, 6);
  Serial.print(",");
  Serial.print(piezoVoltage, 6);
  Serial.print(",");
  Serial.print(latestForceN, 3);
  Serial.print(",");
  Serial.print(position);
  Serial.print(",");
  Serial.println(motorStateName());
}

void processCommand(String command)
{
  command.trim();

  if (command.startsWith("MOTOR_SPEED,"))
  {
    int speed = command.substring(12).toInt();
    speed = constrain(speed, 0, MAX_MOTOR_PERCENT);
    Serial.print("STATUS,MOTOR_SPEED,");
    Serial.println(speed);
    return;
  }

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

  if (command == "MOTOR_STOP")
  {
    motorStop();

    if (motorMode != MODE_ESTOP) motorMode = MODE_STOPPED;

    Serial.println("STATUS,MOTOR_STOPPED");
    return;
  }

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

    motorStop();

    Serial.print("STATUS,MOTOR_LOCKED,");
    Serial.println(targetPosition);
    return;
  }

  if (command.startsWith("SET_LOCK_TOL,"))
  {
    int tolerance = command.substring(13).toInt();
    lockToleranceCounts = max(0, tolerance);

    Serial.print("STATUS,LOCK_TOLERANCE,");
    Serial.println(lockToleranceCounts);
    return;
  }

  if (command == "TARE_FORCE")
  {
    tareForceSensor();
    return;
  }

  if (command.startsWith("START,"))
  {
    int requestedRate = command.substring(6).toInt();
    if (requestedRate < 1) requestedRate = 1;

    telemetryRateHz = min(requestedRate, 100);
    telemetryIntervalMicros = 1000000UL / telemetryRateHz;

    acquisitionStartMicros = micros();
    lastTelemetryMicros = acquisitionStartMicros;
    acquisitionRunning = true;

    Serial.print("STATUS,ACQUISITION_STARTED,");
    Serial.println(telemetryRateHz);
    return;
  }

  if (command == "STOP")
  {
    acquisitionRunning = false;
    Serial.println("STATUS,ACQUISITION_STOPPED");
    return;
  }

  if (command == "ESTOP")
  {
    motorStop();
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

      if (serialCommand.length() > 100) serialCommand = "";
    }
  }
}

void setup()
{
  Serial.begin(115200);

  pinMode(MOTOR_ENA, OUTPUT);
  pinMode(MOTOR_IN1, OUTPUT);
  pinMode(MOTOR_IN2, OUTPUT);
  motorStop();

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

  tareForceSensor();

  Serial.println("STATUS,SYSTEM_READY");
}

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

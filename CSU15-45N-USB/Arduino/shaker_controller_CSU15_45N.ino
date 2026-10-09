/*
  Shaker Control System - Arduino Mega
  Motor + encoder controller only

  Force sensor in this version:
    SingleTact CSU15-45N USB
    -> connected directly to the PC
    -> NOT connected to Arduino

  Hardware
  --------
  FAULHABER 2232 024S motor
  FAULHABER IE2-512 encoder
  L298N H-bridge
  Arduino Mega

  Wiring
  ------
  Mega D6  -> L298N ENA
  Mega D7  -> L298N IN1
  Mega D8  -> L298N IN2

  Encoder A -> Mega D18
  Encoder B -> Mega D19

  IMPORTANT verified direction mapping for this setup:
    UP   -> encoder count DECREASES
    DOWN -> encoder count INCREASES

  Normal behavior
  ---------------
  - Press/hold UP or DOWN: motor moves.
  - Release UP or DOWN: dynamic electrical brake only.
  - POSITION BRAKE is enabled only by an explicit GUI command.
  - POSITION BRAKE captures the current encoder count and actively returns
    to that position if the shaft is disturbed.
  - Manual UP/DOWN cancels POSITION BRAKE.
  - ESTOP disables ENA (coast) and must be explicitly reset.

  Serial baud: 115200
*/

// ============================================================
// Pins
// ============================================================
const uint8_t MOTOR_ENA = 6;
const uint8_t MOTOR_IN1 = 7;
const uint8_t MOTOR_IN2 = 8;

const uint8_t ENCODER_A = 18;
const uint8_t ENCODER_B = 19;

// ============================================================
// Encoder
// ============================================================
volatile long encoderCount = 0;

void encoderISR()
{
  if (digitalRead(ENCODER_A) == digitalRead(ENCODER_B))
    encoderCount++;
  else
    encoderCount--;
}

long getEncoderCount()
{
  noInterrupts();
  long value = encoderCount;
  interrupts();
  return value;
}

// ============================================================
// Motor settings
// ============================================================
const int MAX_MOTOR_PERCENT = 5;
const int POSITION_HOLD_PERCENT = 5;
const int POSITION_HOLD_PWM = 13;  // round(255 * 0.05)

int percentToPWM(int percent)
{
  percent = constrain(percent, 0, MAX_MOTOR_PERCENT);
  return (int)(((float)percent / 100.0f) * 255.0f + 0.5f);
}

// ============================================================
// Motor state
// ============================================================
enum MotorMode
{
  MODE_BRAKED,
  MODE_UP,
  MODE_DOWN,
  MODE_POSITION_HOLD,
  MODE_ESTOP
};

MotorMode motorMode = MODE_BRAKED;

long positionHoldTarget = 0;
int positionHoldTolerance = 2;

const char* motorStateName()
{
  switch (motorMode)
  {
    case MODE_BRAKED:       return "BRAKED";
    case MODE_UP:           return "UP";
    case MODE_DOWN:         return "DOWN";
    case MODE_POSITION_HOLD:return "POSITION_HOLD";
    case MODE_ESTOP:        return "ESTOP";
    default:                return "UNKNOWN";
  }
}

// ============================================================
// Low-level motor control
// ============================================================
void motorCoast()
{
  // ENA disabled: bridge output is not actively driving the motor.
  analogWrite(MOTOR_ENA, 0);
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);
}

void motorBrake()
{
  // Dynamic electrical brake:
  // both inputs LOW and ENA HIGH -> both motor terminals are driven LOW.
  analogWrite(MOTOR_ENA, 0);
  digitalWrite(MOTOR_IN1, LOW);
  digitalWrite(MOTOR_IN2, LOW);
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
// Encoder-based POSITION BRAKE
// ============================================================
void updatePositionBrake()
{
  if (motorMode != MODE_POSITION_HOLD)
    return;

  const long current = getEncoderCount();
  const long error = positionHoldTarget - current;

  /*
    Verified direction mapping:
      UP   -> encoder count decreases
      DOWN -> encoder count increases

    Therefore:
      current < target  -> count must increase -> DOWN
      current > target  -> count must decrease -> UP
  */
  if (error > positionHoldTolerance)
  {
    motorDownPWM(POSITION_HOLD_PWM);
  }
  else if (error < -positionHoldTolerance)
  {
    motorUpPWM(POSITION_HOLD_PWM);
  }
  else
  {
    motorBrake();
  }
}

// ============================================================
// Serial telemetry
// ============================================================
const unsigned long TELEMETRY_INTERVAL_US = 20000UL; // 50 Hz
unsigned long lastTelemetryMicros = 0;

void sendTelemetry()
{
  Serial.print("MOTOR_DATA,");
  Serial.print(millis());
  Serial.print(",");
  Serial.print(getEncoderCount());
  Serial.print(",");
  Serial.println(motorStateName());
}

// ============================================================
// Serial command handling
// ============================================================
String serialCommand = "";

void processCommand(String command)
{
  command.trim();

  if (command.startsWith("MOTOR_UP,"))
  {
    if (motorMode == MODE_ESTOP)
    {
      Serial.println("ERROR,ESTOP_ACTIVE");
      return;
    }

    int percent = command.substring(9).toInt();
    percent = constrain(percent, 0, MAX_MOTOR_PERCENT);

    motorMode = MODE_UP;  // manual motion cancels position hold
    motorUpPWM(percentToPWM(percent));

    Serial.print("STATUS,MOTOR_UP,");
    Serial.println(percent);
    return;
  }

  if (command.startsWith("MOTOR_DOWN,"))
  {
    if (motorMode == MODE_ESTOP)
    {
      Serial.println("ERROR,ESTOP_ACTIVE");
      return;
    }

    int percent = command.substring(11).toInt();
    percent = constrain(percent, 0, MAX_MOTOR_PERCENT);

    motorMode = MODE_DOWN;  // manual motion cancels position hold
    motorDownPWM(percentToPWM(percent));

    Serial.print("STATUS,MOTOR_DOWN,");
    Serial.println(percent);
    return;
  }

  if (command == "MOTOR_BRAKE" || command == "MOTOR_STOP")
  {
    if (motorMode == MODE_ESTOP)
    {
      Serial.println("STATUS,ESTOP_ACTIVE");
      return;
    }

    motorBrake();
    motorMode = MODE_BRAKED;
    Serial.println("STATUS,MOTOR_BRAKED");
    return;
  }

  if (command.startsWith("MOTOR_POSITION_BRAKE,"))
  {
    if (motorMode == MODE_ESTOP)
    {
      Serial.println("ERROR,ESTOP_ACTIVE");
      return;
    }

    int tolerance = command.substring(21).toInt();
    tolerance = max(0, tolerance);

    positionHoldTolerance = tolerance;
    positionHoldTarget = getEncoderCount();
    motorMode = MODE_POSITION_HOLD;
    motorBrake();

    Serial.print("STATUS,POSITION_BRAKE_ON,");
    Serial.print(positionHoldTarget);
    Serial.print(",TOL,");
    Serial.println(positionHoldTolerance);
    return;
  }

  if (command == "MOTOR_POSITION_RELEASE")
  {
    if (motorMode == MODE_ESTOP)
    {
      Serial.println("STATUS,ESTOP_ACTIVE");
      return;
    }

    motorBrake();
    motorMode = MODE_BRAKED;
    Serial.println("STATUS,POSITION_BRAKE_OFF");
    return;
  }

  if (command.startsWith("SET_LOCK_TOL,"))
  {
    int tolerance = command.substring(13).toInt();
    positionHoldTolerance = max(0, tolerance);

    Serial.print("STATUS,LOCK_TOLERANCE,");
    Serial.println(positionHoldTolerance);
    return;
  }

  if (command == "ESTOP")
  {
    motorCoast();
    motorMode = MODE_ESTOP;
    Serial.println("STATUS,EMERGENCY_STOP");
    return;
  }

  if (command == "RESET_ESTOP")
  {
    motorBrake();
    motorMode = MODE_BRAKED;
    Serial.println("STATUS,ESTOP_RESET");
    return;
  }

  if (command == "GET_STATUS")
  {
    Serial.print("STATUS,CURRENT,");
    Serial.print(getEncoderCount());
    Serial.print(",");
    Serial.println(motorStateName());
    return;
  }

  Serial.print("ERROR,UNKNOWN_COMMAND,");
  Serial.println(command);
}

void readSerialCommands()
{
  while (Serial.available() > 0)
  {
    char c = (char)Serial.read();

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
// Setup / loop
// ============================================================
void setup()
{
  Serial.begin(115200);

  pinMode(MOTOR_ENA, OUTPUT);
  pinMode(MOTOR_IN1, OUTPUT);
  pinMode(MOTOR_IN2, OUTPUT);

  pinMode(ENCODER_A, INPUT_PULLUP);
  pinMode(ENCODER_B, INPUT_PULLUP);

  attachInterrupt(digitalPinToInterrupt(ENCODER_A), encoderISR, CHANGE);

  motorBrake();
  motorMode = MODE_BRAKED;

  delay(200);
  Serial.println("STATUS,SYSTEM_READY,MOTOR_ENCODER_ONLY,UP_DECREASES_COUNT");
}

void loop()
{
  readSerialCommands();
  updatePositionBrake();

  unsigned long now = micros();
  if ((unsigned long)(now - lastTelemetryMicros) >= TELEMETRY_INTERVAL_US)
  {
    lastTelemetryMicros += TELEMETRY_INTERVAL_US;
    sendTelemetry();
  }
}
// PulseVision sensor firmware - Arduino UNO R4 WiFi + MAX30100 + MPU9250/6500/9255 + MLX90614.
//
// Sends one JSON object per line, once per second, at 115200 baud:
//   {"t_ms":12345,"max30100":{"ok":true,"hr_bpm":72.4,"spo2_pct":97},
//    "mpu":{"ok":true,"ax":-0.02,"ay":0.01,"az":0.99,"a_g":1.00,"motion_g":0.02,"gx":0.4,"gy":-0.2,"gz":0.1},
//    "mlx90614":{"ok":true,"ambient_temp_c":30.0,"object_temp_c":33.8}}
// Values are null when unavailable (no finger, no recent beat, or sensor not initialised).
// The laptop never invents values.
//
// Wiring (shared I2C bus on pins A4 SDA / A5 SCL, 3.3V):
//   MAX30100  (0x57):               VIN -> 3.3V, GND -> GND, SDA -> A4, SCL -> A5
//   MPU9250/6500/9255 (0x68/0x69):  VCC -> 3.3V, GND -> GND, SDA -> A4, SCL -> A5
//   MLX90614  (0x5A):               VCC -> 3.3V, GND -> GND, SDA -> A4, SCL -> A5
//
// Libraries:
//   - "MAX30100lib" by OXullo Intersecans
//   - "MPU9250_asukiaaa" by Asuki Kono
//   - "Adafruit MLX90614 Library" by Adafruit (v2.1.7+)

#include <Wire.h>
#include "MAX30100_PulseOximeter.h"
#include <MPU9250_asukiaaa.h>
#include <Adafruit_MLX90614.h>

const unsigned long REPORT_INTERVAL_MS = 1000;   // one JSON line per second
const unsigned long BEAT_TIMEOUT_MS = 3000;      // no beat for this long = no valid HR
const unsigned long RETRY_INIT_MS = 5000;        // retry failed sensors every 5 s

PulseOximeter pox;
MPU9250_asukiaaa mpuLow(MPU9250_ADDRESS_AD0_LOW);    // 0x68
MPU9250_asukiaaa mpuHigh(MPU9250_ADDRESS_AD0_HIGH);  // 0x69
MPU9250_asukiaaa* mpu = &mpuLow;
Adafruit_MLX90614 mlx = Adafruit_MLX90614();

bool maxOk = false;
bool mpuOk = false;
bool mlxOk = false;
unsigned long lastReport = 0;
unsigned long lastBeat = 0;
unsigned long lastMaxInit = 0;
unsigned long lastMpuInit = 0;
unsigned long lastMlxInit = 0;

// Track peak dynamic acceleration (|accelSqrt - 1.0g|) across each 1 s window
// so sudden patient movement / falls between 1 s reports are captured.
float windowPeakMotionG = 0.0f;
unsigned long lastMpuSample = 0;
const unsigned long MPU_SAMPLE_INTERVAL_MS = 50; // 20 Hz motion sampling

void onBeatDetected() {
  lastBeat = millis();
}

bool probeI2C(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

bool initMax30100() {
  lastMaxInit = millis();
  // Probe 0x57 first BEFORE calling pox.begin(): MAX30100::setMode() runs
  // before getPartId() inside pox.begin() and writes to 0x57 even if the
  // sensor is absent, which can destabilise a shared I2C bus if repeated.
  if (!probeI2C(MAX30100_I2C_ADDRESS)) {
    return false;
  }
  if (!pox.begin()) {
    return false;
  }
  Wire.setClock(100000);  // pox.begin() sets 400kHz; revert to 100kHz for MLX90614 SMBus compatibility
  pox.setIRLedCurrent(MAX30100_LED_CURR_7_6MA);
  pox.setOnBeatDetectedCallback(onBeatDetected);
  return true;
}

bool initMpu() {
  lastMpuInit = millis();
  if (probeI2C(MPU9250_ADDRESS_AD0_LOW)) {
    mpu = &mpuLow;
  } else if (probeI2C(MPU9250_ADDRESS_AD0_HIGH)) {
    mpu = &mpuHigh;
  } else {
    return false;
  }
  mpu->setWire(&Wire);
  mpu->beginAccel(ACC_FULL_SCALE_4_G);
  mpu->beginGyro(GYRO_FULL_SCALE_500_DPS);
  return mpu->accelUpdate() == 0;
}

bool initMlx() {
  lastMlxInit = millis();
  if (!probeI2C(0x5A)) {
    return false;
  }
  return mlx.begin();
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 4000) {
    delay(10);
  }
  Wire.begin();
  Wire.setWireTimeout(25000, true);  // 25 ms I2C timeout: prevent bus lockups from hanging USB CDC
  maxOk = initMax30100();
  mpuOk = initMpu();
  mlxOk = initMlx();
}

void printNumberOrNull(float value, int decimals, bool valid) {
  if (valid && !isnan(value) && !isinf(value)) {
    Serial.print(value, decimals);
  } else {
    Serial.print("null");
  }
}

void loop() {
  unsigned long now = millis();

  if (maxOk) {
    pox.update();  // must be called as often as possible
  } else if (now - lastMaxInit >= RETRY_INIT_MS) {
    maxOk = initMax30100();
  }

  if (mpuOk && (now - lastMpuSample >= MPU_SAMPLE_INTERVAL_MS)) {
    lastMpuSample = now;
    if (mpu->accelUpdate() == 0) {
      float mag = mpu->accelSqrt();
      float dyn = fabs(mag - 1.0f);
      if (dyn > windowPeakMotionG) {
        windowPeakMotionG = dyn;
      }
    } else {
      mpuOk = false;
    }
  } else if (!mpuOk && (now - lastMpuInit >= RETRY_INIT_MS)) {
    mpuOk = initMpu();
  }

  if (!mlxOk && (now - lastMlxInit >= RETRY_INIT_MS)) {
    mlxOk = initMlx();
  }

  if (now - lastReport < REPORT_INTERVAL_MS) {
    return;
  }
  lastReport = now;

  bool beatRecent = maxOk && lastBeat != 0 && (now - lastBeat) <= BEAT_TIMEOUT_MS;
  float hr = beatRecent ? pox.getHeartRate() : 0;
  float spo2 = beatRecent ? (float)pox.getSpO2() : 0;

  bool mpuSampleOk = false;
  float ax = 0, ay = 0, az = 0, aMag = 0, motionG = 0, gx = 0, gy = 0, gz = 0;
  if (mpuOk) {
    uint8_t aErr = mpu->accelUpdate();
    uint8_t gErr = mpu->gyroUpdate();
    if (aErr == 0 && gErr == 0) {
      mpuSampleOk = true;
      ax = mpu->accelX();
      ay = mpu->accelY();
      az = mpu->accelZ();
      aMag = mpu->accelSqrt();
      float instMotion = fabs(aMag - 1.0f);
      motionG = (windowPeakMotionG > instMotion) ? windowPeakMotionG : instMotion;
      gx = mpu->gyroX();
      gy = mpu->gyroY();
      gz = mpu->gyroZ();
    } else {
      mpuOk = false;
    }
  }
  windowPeakMotionG = 0.0f;

  Serial.print("{\"t_ms\":");
  Serial.print(now);
  Serial.print(",\"max30100\":{\"ok\":");
  Serial.print(maxOk ? "true" : "false");
  Serial.print(",\"hr_bpm\":");
  printNumberOrNull(hr, 1, hr > 0);
  Serial.print(",\"spo2_pct\":");
  printNumberOrNull(spo2, 0, spo2 > 0);
  Serial.print("},\"mpu\":{\"ok\":");
  Serial.print(mpuSampleOk ? "true" : "false");
  Serial.print(",\"ax\":");
  printNumberOrNull(ax, 2, mpuSampleOk);
  Serial.print(",\"ay\":");
  printNumberOrNull(ay, 2, mpuSampleOk);
  Serial.print(",\"az\":");
  printNumberOrNull(az, 2, mpuSampleOk);
  Serial.print(",\"a_g\":");
  printNumberOrNull(aMag, 2, mpuSampleOk);
  Serial.print(",\"motion_g\":");
  printNumberOrNull(motionG, 2, mpuSampleOk);
  Serial.print(",\"gx\":");
  printNumberOrNull(gx, 1, mpuSampleOk);
  Serial.print(",\"gy\":");
  printNumberOrNull(gy, 1, mpuSampleOk);
  Serial.print(",\"gz\":");
  printNumberOrNull(gz, 1, mpuSampleOk);

  // MLX90614 infrared skin surface temperature (NOT core body temperature)
  Serial.print("},\"mlx90614\":{\"ok\":");
  Serial.print(mlxOk ? "true" : "false");
  Serial.print(",\"ambient_temp_c\":");
  if (mlxOk) {
    float amb = mlx.readAmbientTempC();
    float obj = mlx.readObjectTempC();
    bool ambValid = !isnan(amb) && !isinf(amb);
    bool objValid = !isnan(obj) && !isinf(obj);
    if (!ambValid && !objValid) {
      mlxOk = false;  // mark failed so retry timer will reinitialize
    }
    printNumberOrNull(amb, 1, ambValid);
    Serial.print(",\"object_temp_c\":");
    printNumberOrNull(obj, 1, objValid);
  } else {
    Serial.print("null,\"object_temp_c\":null");
  }
  Serial.println("}}");
}

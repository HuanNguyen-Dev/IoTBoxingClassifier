#include <ArduinoBLE.h>       // BLE
#include <Arduino_LSM6DS3.h>  // Supports sensor IDs 0x69 and 0x6C

/************* Sampling and Scaling **********************/

const uint16_t IMU_SAMPLE_RATE = 208;  // Sensor output rate; must match Python NOMINAL_HZ

// Use the Arduino driver's protected register API to configure and verify 208 Hz.
// Its default begin() configures 104 Hz, and its default rate getters are hardcoded.
#undef IMU
class BoxingIMU : public LSM6DS3Class {
public:
  BoxingIMU() : LSM6DS3Class(Wire, 0x6A), sensor_id(-1) {}

  int begin() {
    Wire.begin();
    sensor_id = readRegister(LSM6DS3_WHO_AM_I_REG);
    if (!LSM6DS3Class::begin()) return 0;

    int accel = readRegister(LSM6DS3_CTRL1_XL);
    int gyro = readRegister(LSM6DS3_CTRL2_G);
    int control = readRegister(0x12);  // CTRL3_C
    if (accel < 0 || gyro < 0 || control < 0) return 0;

    // ODR nibble 0x5 = 208 Hz; preserve full-scale and filter bits.
    uint8_t accel_208 = (accel & 0x0F) | 0x50;
    uint8_t gyro_208 = (gyro & 0x0F) | 0x50;
    uint8_t control_bdu = control | 0x44;  // Block Data Update + register auto-increment
    if (writeRegister(LSM6DS3_CTRL1_XL, accel_208) != 1 ||
        writeRegister(LSM6DS3_CTRL2_G, gyro_208) != 1 ||
        writeRegister(0x12, control_bdu) != 1) return 0;

    return readRegister(LSM6DS3_CTRL1_XL) == accel_208 &&
           readRegister(LSM6DS3_CTRL2_G) == gyro_208 &&
           readRegister(0x12) == control_bdu;
  }

  int sensorId() const { return sensor_id; }
  float accelerationSampleRate() override { return IMU_SAMPLE_RATE; }
  float gyroscopeSampleRate() override { return IMU_SAMPLE_RATE; }

  bool sampleReady() {
    int status = readRegister(LSM6DS3_STATUS_REG);
    return status >= 0 && (status & 0x03) == 0x03;
  }

private:
  int sensor_id;
};

BoxingIMU IMU;

const float ACC_SCALE = 1000.0f;              // g   -> milli-g   (must match Python SCALES)
const float GYRO_SCALE = 8.0f;                // dps -> 1/8 dps   (must match Python SCALES)

/****************** Message data struct ********************/

struct IMU_3D {
  uint32_t t_ms;
  int16_t ax;
  int16_t ay;
  int16_t az;
  int16_t gx;
  int16_t gy;
  int16_t gz;
};

/************* BLE **********************/

const char* DEVICE_NAME = "Nano33IoT_Group12_Left";

// BLE service and characteristic UUIDs
BLEService imuService("19B10000-E8F2-537E-4F6C-D104768A1214");
BLECharacteristic CommandChar("19B10003-E8F2-537E-4F6C-D104768A1214", BLERead | BLEWrite, 1);
BLECharacteristic AccelChar("19B10004-E8F2-537E-4F6C-D104768A1214", BLERead | BLENotify, sizeof(IMU_3D));

/*************** Utils ***********/
int16_t convertToFixed(float val, float scale)
{
  float s = val * scale;
  if (s > 32767.0f) s = 32767.0f;    // clip rather than overflow
  if (s < -32768.0f) s = -32768.0f;
  return (int16_t)lroundf(s);
}

/******************* Setup *********************/
void setup() {
  Serial.begin(9600);
  // Allow the USB Serial Monitor to attach without blocking standalone startup.
  unsigned long serial_start = millis();
  while (!Serial && millis() - serial_start < 3000) {
    delay(10);
  }
  Serial.print("Starting ");
  Serial.println(DEVICE_NAME);

  /*************** Initialise IMU *****************/

  Serial.println("Initializing IMU at 208 Hz...");
  if (!IMU.begin()) {
    // Repeat the error so it remains visible if the monitor opens after startup.
    while (1) {
      Serial.print("IMU initialization/208 Hz configuration failed. WHO_AM_I=");
      Serial.println(IMU.sensorId(), HEX);
      Serial.println("BLE has not started. Check Arduino_LSM6DS3 supports IDs 0x69 and 0x6C.");
      delay(1000);
    }
  }
  Serial.print("IMU initialized. WHO_AM_I=0x");
  Serial.println(IMU.sensorId(), HEX);
  Serial.println("208 Hz configuration verified by register readback.");

  /**************** Initialise BLE *****************/

  Serial.println("Initializing BLE...");
  if (!BLE.begin()) {
    while (1) {
      Serial.println("Starting BLE failed!");
      delay(1000);
    }
  }

  BLE.setLocalName(DEVICE_NAME);
  BLE.setAdvertisedService(imuService);
  imuService.addCharacteristic(CommandChar);
  imuService.addCharacteristic(AccelChar);

  BLE.addService(imuService);

  uint8_t command = 0;
  CommandChar.setValue(&command, 1);
  AccelChar.setValue("");

  BLE.setConnectionInterval(6, 12);  // Request 7.5–15 ms; central negotiates actual interval
  BLE.advertise();
  Serial.println("BLE advertising with sensor notify characteristic started...");
}

/******************* Loop *********************/
void loop() {
  BLEDevice central = BLE.central();
  bool IMU_active = false;

  if (central) {
    Serial.print("Connected to central: ");
    Serial.println(central.address());

    while (central.connected()) {

      /******************** On-Demand IMU ******************************/
      // Check if IMU should activate
      if (CommandChar.written()) {
        CommandChar.value()[0] == 1 ? IMU_active = true : IMU_active = false;
      }

      if (IMU_active) {
        // Require fresh acceleration and gyroscope data before sending a sample.
        if (!IMU.sampleReady()) {
          continue;
        }

        float ax, ay, az;
        float gx, gy, gz;
        if (!IMU.readAcceleration(ax, ay, az) || !IMU.readGyroscope(gx, gy, gz)) {
          continue;
        }

        IMU_3D IMUData = {
          millis(),
          convertToFixed(ax, ACC_SCALE),
          convertToFixed(ay, ACC_SCALE),
          convertToFixed(az, ACC_SCALE),
          convertToFixed(gx, GYRO_SCALE),
          convertToFixed(gy, GYRO_SCALE),
          convertToFixed(gz, GYRO_SCALE)
        };

        // Send the existing 16-byte packet via BLE.
        int result = AccelChar.writeValue(
          (uint8_t*)&IMUData,
          sizeof(IMUData)
        );
      }
    }
  }

  Serial.print("Disconnected from central: ");
  Serial.println(central.address());
}

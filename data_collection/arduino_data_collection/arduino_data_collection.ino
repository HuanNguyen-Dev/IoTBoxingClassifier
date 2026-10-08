#include <ArduinoBLE.h>       // BLE
#include <SparkFunLSM6DS3.h>  // IMU sensor

LSM6DS3 IMU(I2C_MODE, 0x6A);

/************* Sampling and Scaling **********************/

const uint16_t IMU_SAMPLE_RATE = 208;  // Sensor output rate; must match Python NOMINAL_HZ

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

  /*************** Initialise IMU *****************/

  IMU.settings.accelEnabled = 1;
  IMU.settings.gyroEnabled = 1;
  IMU.settings.accelSampleRate = IMU_SAMPLE_RATE;
  IMU.settings.gyroSampleRate = IMU_SAMPLE_RATE;
  IMU.settings.accelRange = 4;
  IMU.settings.gyroRange = 2000;
  IMU.settings.accelODROff = 0;
  IMU.settings.accelFifoEnabled = 0;
  IMU.settings.gyroFifoEnabled = 0;

  if (IMU.begin() != IMU_SUCCESS) {
    Serial.println("Failed to initialize SparkFun LSM6DS3! Check sensor ID/address.");
    while (1)
      ;
  }

  /**************** Initialise BLE *****************/

  if (!BLE.begin()) {
    Serial.println("Starting BLE failed!");
    while (1)
      ;
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
        uint8_t status = 0;
        if (IMU.readRegister(&status, LSM6DS3_ACC_GYRO_STATUS_REG) != IMU_SUCCESS) {
          continue;
        }
        // Require fresh acceleration and gyroscope data before sending a sample.
        if ((status & 0x03) != 0x03) {
          continue;
        }

        uint16_t errors_before = IMU.nonSuccessCounter;
        uint16_t warnings_before = IMU.allOnesCounter;
        float ax = IMU.readFloatAccelX();
        float ay = IMU.readFloatAccelY();
        float az = IMU.readFloatAccelZ();
        float gx = IMU.readFloatGyroX();
        float gy = IMU.readFloatGyroY();
        float gz = IMU.readFloatGyroZ();
        if (IMU.nonSuccessCounter != errors_before || IMU.allOnesCounter != warnings_before) {
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

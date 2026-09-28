#include <ArduinoBLE.h>       // BLE
#include <Arduino_LSM6DS3.h>  // IMU sensor


/************* Sampling and Scaling **********************/

unsigned long IMU_last_read_time = 0;
const unsigned long IMU_READ_INTERVAL = 20;  // Interval between IMU messages (ms)

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

  if (!IMU.begin()) {
    Serial.println("Failed to initialize IMU!");
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
        float ax, ay, az;
        float gx, gy, gz;
        if (IMU.accelerationAvailable() && IMU.gyroscopeAvailable() && millis() - IMU_last_read_time >= IMU_READ_INTERVAL) {
          IMU_last_read_time = millis();
          IMU.readAcceleration(ax, ay, az);
          IMU.readGyroscope(gx, gy, gz);

          // Serial.println(ax);
          // Serial.println(ay);
          // Serial.println(az);
          // Serial.println(gx);
          // Serial.println(gy);
          // Serial.println(gz);

          // Serial.println();  // Extra line between blocks for readability

          IMU_3D IMUData = {
            millis(),
            convertToFixed(ax, ACC_SCALE),
            convertToFixed(ay, ACC_SCALE),
            convertToFixed(az, ACC_SCALE),
            convertToFixed(gx, GYRO_SCALE),
            convertToFixed(gy, GYRO_SCALE),
            convertToFixed(gz, GYRO_SCALE)
          };

          // Send via BLE
          int result = AccelChar.writeValue(
            (uint8_t*)&IMUData,
            sizeof(IMUData)
          );
        }
      }
    }
  }

  Serial.print("Disconnected from central: ");
  Serial.println(central.address());
}
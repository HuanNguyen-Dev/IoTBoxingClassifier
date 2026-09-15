#include <ArduinoBLE.h>       // BLE
#include <Arduino_LSM6DS3.h>  // IMU sensor


/************* Aggrigation **********************/

// Define the number of samples to keep in the sliding window
const int WINDOW_SIZE = 30;

unsigned long IMU_last_read_time = 0;
const unsigned long IMU_READ_INTERVAL = 100;  // Interval between IMU messages (ms)

/****************** Message data struct ********************/

struct IMU_3D {
  int16_t ax;
  int16_t ay;
  int16_t az;
  int16_t gx;
  int16_t gy;
  int16_t gz;
};

/************* BLE **********************/

// BLE service and characteristic UUIDs
BLEService imuService("19B10000-E8F2-537E-4F6C-D104768A1214");
BLECharacteristic CommandChar("19B10003-E8F2-537E-4F6C-D104768A1214", BLERead | BLEWrite, 1);
BLECharacteristic AccelChar("19B10004-E8F2-537E-4F6C-D104768A1214", BLERead | BLENotify, sizeof(IMU_3D));

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

  BLE.setLocalName("Nano33IoT_Group12_D");
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

          Serial.println(ax);
          Serial.println(ay);
          Serial.println(az);
          Serial.println(gx);
          Serial.println(gy);
          Serial.println(gz);

          Serial.println();  // Extra line between blocks for readability

          IMU_3D IMUData = {
            convertToFixed(ax),
            convertToFixed(ay),
            convertToFixed(az),
            convertToFixed(gx),
            convertToFixed(gy),
            convertToFixed(gz)
          };

          // Send via BLE
          int result = AccelChar.writeValue(
            (uint8_t*)&IMUData,
            sizeof(IMUData)
          );
        }
      }
    }

    delay(100);  // 10 Hz
  }

  Serial.print("Disconnected from central: ");
  Serial.println(central.address());
}

int16_t convertToFixed(float val)
{
  return round(val * 8.0f);
}
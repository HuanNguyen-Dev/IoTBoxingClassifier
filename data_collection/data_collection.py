import asyncio
import struct
import time
import statistics
from bleak import BleakScanner, BleakClient

# Match part of the advertised local name
TARGET_NAME_FRAGMENT_1 = "Nano33IoT_Group12_D"
TARGET_NAME_FRAGMENT_2 = ""
TARGET_NAME_FRAGMENT_3 = ""
TARGET_NAME_FRAGMENT_4 = ""

# List of all the arduino names. Will add the rest as we go
TARGET_NAME_FRAGMENTS = [TARGET_NAME_FRAGMENT_1]

# UUID of the notifiable characteristic
COMMANDCHAR_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"
IMUCHAR_UUID = "19B10004-E8F2-537E-4F6C-D104768A1214"

# Time that each detection period lasts
COLLECTION_PERIOD = 1000

# Current window Id
window_id = 0

# # Label Dict
# labels = {
#     "right_jab": 1,
#     "left_jab": 2,
#     "right_hook": 3,
#     "left_hook": 4,
#     "right_uppercut": 5,
#     "left_uppercut": 6,
# }

# Labels array
labels = [
    "right_jab",
    "left_jab",
    "right_hook",
    "left_hook",
    "right_uppercut",
    "left_uppercut",
]

# Axes arrays
ax_arr = []
ay_arr = []
az_arr = []
gx_arr = []
gy_arr = []
gz_arr = []

# Connects to arduino
async def scan_and_connect():
    print("Scanning for devices...")
    devices = await BleakScanner.discover(timeout=5.0)

    target_device = None
    for d in devices:
        if d.name and TARGET_NAME_FRAGMENT in d.name:
            target_device = d
            break

    if not target_device:
        print(f"No device found with name containing '{TARGET_NAME_FRAGMENT}'.")
        return

    print(f"Found device: {target_device.name} ({target_device.address})")
    return target_device

# Extracts the features of a single axis
def extract_axis_features(axis_arr):
    mean = statistics.mean(axis_arr)
    stdev = statistics.stdev(axis_arr)
    min_val = min(axis_arr)
    max_val = max(axis_arr)
    range_val = max_val - min_val

    return {
        "mean": mean,
        "stdev": stdev,
        "min": min_val,
        "max": max_val,
        "range": range_val
    }


# Extracts the important features from all the axes
def collateData(window_id, timestamp, label):
    for axis in [ax_arr, ay_arr, az_arr, gx_arr, gy_arr, gz_arr]:
        axis_features = extract_axis_features(axis)
        axis_features["window_id"] = window_id
        axis_features["ts"] = timestamp
        axis_features["label"] = label

        axis.clear()

# This function is called whenever a new IMU notification is received
def handle_IMU_notification(sender, data):
    try:
        values = struct.unpack('<12h', data)
        # Convert from fixed point to float
        values = [v / 8.0 for v in values]

        ax, ay, az, gx, gy, gz = values
        ax_arr.append(ax)
        ay_arr.append(ay)
        az_arr.append(az)
        gx_arr.append(gx)
        gy_arr.append(gy)
        gz_arr.append(gz)

    except:
        pass

# Read the values from an arduino for the specified collection period
async def read_IMU(client, label):
    command = await client.read_gatt_char(COMMANDCHAR_UUID)
    if command == b'\x00':
        await client.write_gatt_char(COMMANDCHAR_UUID, b'\x01')
        try:
            await client.start_notify(IMUCHAR_UUID, handle_IMU_notification)
            start_time = time.time()

            while time.time() < start_time + COLLECTION_PERIOD:
                await asyncio.sleep(1)  # Keep the loop alive
            
            await client.stop_notify(IMUCHAR_UUID)
            collateData(window_id, time.time(), label)
            window_id += 1

        except Exception as e:
            print("Failed to subscribe or receive notification:", e)

async def main():
    try:
        client = None
        while True:
            selection = 0

            print("--- Arduino Nano 33 IoT Control Menu ---")
            print("1. Connect to a device")
            print("2. Record Right Jab")
            print("3. Record Left Jab")
            print("4. Record Right Hook")
            print("5. Record Left Hook")
            print("6. Record Right Uppercut")
            print("7. Record Left Uppercut")
            print("8. Exit")
            selection = input("Enter selection: ")
            selection = int(selection)

            if selection == 1:
                device = await scan_and_connect()

                if device:
                    client = BleakClient(device.address)
                    await client.connect()
                    if client.is_connected:
                        print("Connected successfully.")
                    else:
                        print("Failed to connect.")

            elif selection == in range(2, 8):
                    if (client == None or not client.is_connected):
                        print("Error! No device connected")
                        continue
                    try:
                        await read_temp(client, labels[selection])
                    except Exception as e:
                        print("Error during read:", e)
            elif selection == 8:
                print("Exiting program")
                if client and client.is_connected:
                    await client.disconnect()
                quit()

    except KeyboardInterrupt:
        print("Program stopped by user.")
    
    finally:
        if client and client.is_connected:
            await client.disconnect()


asyncio.run(main())

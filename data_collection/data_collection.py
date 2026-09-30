"""
HAR boxing data collection: up to 4 Arduino Nano 33 IoT devices -> Raspberry Pi -> MongoDB.

Each trial is a scripted timeline controlled by this program (not by human start/stop):
    [countdown, not recorded] -> REST lead-in -> (PUNCH -> REST) x REPS -> END
Raw, timestamped IMU samples from every device plus the label timeline are saved as ONE
document per trial. No features are computed here; do windowing/filtering/features later.

Setup:
    pip install bleak pymongo
    export MONGODB_URI="mongodb+srv://<user>:<password>@<cluster>/?appName=IAB330"

Firmware: nano_imu_ble.ino (16-byte packets: uint32 t_ms + 6 x int16). Legacy 12-byte
packets (6 x int16, no timestamp) are also accepted.
"""

import asyncio
import datetime
import os
import random
import struct
import time

from bleak import BleakClient, BleakScanner
from pymongo import MongoClient

# ----------------------------- Configuration -----------------------------
MONGODB_URI = "mongodb+srv://n11547227_db_user:IAB330@iab330.ujghyul.mongodb.net/?appName=IAB330" # os.environ.get("MONGODB_URI")  # do NOT hard-code credentials
DB_NAME = "HAR_boxing"
COLLECTION_NAME = "collected_data"

DEVICE_PREFIX = "Nano33IoT_Group12"  # matches ..._Left, ..._Right, and any extras
MAX_DEVICES = 4

COMMAND_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"
IMU_UUID = "19B10004-E8F2-537E-4F6C-D104768A1214"
START_CMD = b"\x01"
STOP_CMD = b"\x00"

CHANNELS = ["ax", "ay", "az", "gx", "gy", "gz"]
# Fixed-point divisors. Must match ACC_SCALE / GYRO_SCALE in the firmware.
# Result units: accel in g, gyro in deg/s.
SCALES = {"ax": 1000.0, "ay": 1000.0, "az": 1000.0,
          "gx": 8.0, "gy": 8.0, "gz": 8.0}
NOMINAL_HZ = 100  # must match SAMPLE_INTERVAL_MS in the firmware (10 ms)

IMU_PACKET = struct.Struct("<I6h")      # 16 bytes: t_ms, ax, ay, az, gx, gy, gz

# Trial timing (seconds)
COUNTDOWN_S = 3
LEAD_IN_S = 1.0
PUNCH_S = 1.0
REST_S = 2.0
REPS_PER_TRIAL = 1    # raise to 5-10 to record several punches per trial
IDLE_TRIAL_S = 10.0   # length of a pure "rest" trial

PUNCHES = [
    "right_jab", "left_jab",
    "right_hook", "left_hook",
    "right_uppercut", "left_uppercut",
]


# ------------------------------- Device class -------------------------------
class Device:
    def __init__(self, ble_device):
        self.name = ble_device.name
        self.address = ble_device.address
        self.client = BleakClient(ble_device)
        self.reset()

    def reset(self):
        self.t = []       # Pi receive time (perf_counter, seconds)
        self.t_dev = []   # device millis(), if firmware sends it
        self.data = {c: [] for c in CHANNELS}
        self.bad_packets = 0

    def handle_IMU_notification(self, _sender, message):
        t = time.perf_counter()
        message = bytes(message)
        if len(message) == IMU_PACKET.size:
            t_ms, *vals = IMU_PACKET.unpack(message)
            self.t_dev.append(t_ms)
        else:
            self.bad_packets += 1
            return
        self.t.append(t)
        for c, v in zip(CHANNELS, vals):
            self.data[c].append(v / SCALES[c])

    async def start_stream(self):
        await self.client.start_notify(IMU_UUID, self.handle_IMU_notification)
        await self.client.write_gatt_char(COMMAND_UUID, START_CMD)

    async def stop_stream(self):
        try:
            await self.client.write_gatt_char(COMMAND_UUID, STOP_CMD)
        except Exception:
            pass
        await self.client.stop_notify(IMU_UUID)

# ------------------------------- Connections --------------------------------
async def scan_and_connect(devices):
    """Scan for up to MAX_DEVICES matching Nanos and connect to them all in parallel."""
    for device in devices:
        if device.client.is_connected:
            await device.client.disconnect()
    devices.clear()

    print("Scanning for devices...")
    found = await BleakScanner.discover(timeout=5.0)
    matches = sorted(
        [device for device in found if device.name and DEVICE_PREFIX in device.name], key=lambda device: device.name
    )[:MAX_DEVICES]

    if not matches:
        print(f"No devices found with name containing '{DEVICE_PREFIX}'.")
        return

    device_names = [match.name for match in matches]

    candidates = [Device(match) for match in matches]
    results = await asyncio.gather(
        *(device.client.connect() for device in candidates), return_exceptions=True
    )
    for device, res in zip(candidates, results):
        if isinstance(res, Exception) or not device.client.is_connected:
            print(f"  FAILED  {device.name} ({device.address}): {res}")
        else:
            print(f"  OK      {device.name} ({device.address})")
            devices.append(device)
    print(f"{len(devices)} device(s) connected.")


# --------------------------------- Timeline ---------------------------------
def build_timeline(label):
    """Return (list of (label, start_s, end_s), total_duration_s), relative to trial start."""
    if label == "rest":
        return [("rest", 0.0, IDLE_TRIAL_S)], IDLE_TRIAL_S

    timeline, t = [("rest", 0.0, LEAD_IN_S)], LEAD_IN_S
    for _ in range(REPS_PER_TRIAL):
        timeline.append((label, t, t + PUNCH_S))
        t += PUNCH_S
        rest = REST_S
        timeline.append(("rest", t, t + rest))
        t += rest
    return timeline, t


async def sleep_until(target_time):
    delay = target_time - time.perf_counter()
    if delay > 0:
        await asyncio.sleep(delay)


# ---------------------------------- Trial -----------------------------------
async def run_trial(devices, label, meta_data, trial_index):
    timeline, duration = build_timeline(label)

    print(f"\nTrial: {label}  (get ready)")
    for i in range(COUNTDOWN_S, 0, -1):
        print(f"  {i}...")
        await asyncio.sleep(1)

    for device in devices:
        device.reset()
    await asyncio.gather(*(device.start_stream() for device in devices))

    t0 = time.perf_counter()
    t0_epoch = time.time()
    print("  START (rest)")

    for stage_label, start, end in timeline:
        await sleep_until(t0 + start)
        if stage_label != "rest":
            print(f"  >>> {stage_label.upper()} <<<\a")
        elif start > 0:
            print("  rest")
        await sleep_until(t0 + end)

    print("  END")
    await asyncio.gather(*(device.stop_stream() for device in devices), return_exceptions=True)

    doc = {
        **meta_data,
        "trial_index": trial_index,
        "label": label,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "t0_epoch": t0_epoch,
        "duration_s": round(duration, 3),
        "label_timeline": [
            {"label": label, "start": round(start, 3), "end": round(end, 3)} for label, start, end in timeline
        ],
        "scales": SCALES,
        "nominal_hz": NOMINAL_HZ,
        "timestamp_source": "raspberry_pi_receive_time",
        "devices": {},
    }

    print("  Samples received:")
    for device in devices:
        rel_t = [round(t - t0, 4) for t in device.t]
        n = len(rel_t)
        hz = n / duration if duration else 0
        start_t_dev = device.t_dev[0]
        IMU_entry = {"t": rel_t, 
                **{c: device.data[c] for c in CHANNELS},
                "n": n,
                "bad_packets": device.bad_packets,
                "t_dev_ms": [current_t_dev - start_t_dev for current_t_dev in device.t_dev]
            }
        note = ""
        if device.bad_packets:
            note += f", {device} bad packets"
        print(f"    {device.name}: {n} samples (~{hz:.1f} Hz){note}")

        doc["devices"][device.name] = IMU_entry
    return doc


# ----------------------------------- Menu -----------------------------------
async def ask(prompt):
    return input(prompt).strip()


async def main():
    if not MONGODB_URI:
        print("Set the MONGODB_URI environment variable first.")
        return

    collection = MongoClient(MONGODB_URI)[DB_NAME][COLLECTION_NAME]
    devices = []
    meta_data = {"participant": "TestP01", "session": "TestS01"}
    trial_counts = {}

    try:
        while True:
            print("\n--- HAR Boxing Data Collection ---")
            print(f"Participant: {meta_data['participant']}   Session: {meta_data['session']}   "
                  f"Devices: {[device.name for device in devices] or 'none'}")
            print("1. Scan & connect to devices")
            print("2. Set participant / session")
            options = {}
            n = 3
            for label in PUNCHES + ["rest"]:
                print(f"{n}. Record {label}")
                options[str(n)] = label
                n += 1
            print("0. Exit")

            choice = await ask("Enter selection: ")

            if choice == "1":
                await scan_and_connect(devices)

            elif choice == "2":
                meta_data["participant"] = (await ask("Participant ID: ")) or meta_data["participant"]
                meta_data["session"] = (await ask("Session ID: ")) or meta_data["session"]

            elif choice in options:
                connected = [device for device in devices if device.client.is_connected]
                # if len(connected) < 2:
                #     print("Error: connect at least 2 devices first (option 1).")
                #     continue
                label = options[choice]
                key = (meta_data["participant"], meta_data["session"], label)
                idx = trial_counts.get(key, 0) + 1
                try:
                    doc = await run_trial(connected, label, meta_data, idx)
                except Exception as e:
                    print("Error during trial:", e)
                    continue
                if (await ask("Save this trial? [Y/n]: ")).lower() != "n":
                    collection.insert_one(doc)
                    trial_counts[key] = idx
                    print(f"Saved trial {idx} for {label}.")
                else:
                    print("Discarded.")

            elif choice == "0":
                print("Exiting.")
                break

    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        for device in devices:
            if device.client.is_connected:
                await device.client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
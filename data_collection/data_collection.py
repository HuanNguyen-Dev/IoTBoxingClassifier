"""
HAR boxing data collection: up to 4 Arduino Nano 33 IoT devices -> Raspberry Pi -> MongoDB.

"""

import asyncio
import datetime
import os
import struct
import time
import json

import numpy as np
import pandas as pd
from bleak import BleakClient, BleakScanner
from pymongo import MongoClient
from pathlib import Path
from dotenv import load_dotenv

from realtime_inference import calibration_path, run_inference, run_calibration, FEATURE_COLUMNS

# ----------------------------- Configuration -----------------------------
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

MONGODB_URI = os.environ.get("MONGODB_URI")
DB_NAME = "HAR_boxing"
COLLECTION_NAME = "collected_data"

# matches ..._Left, ..._Right, and any extras
DEVICE_PREFIX = "Nano33IoT_Group12"  
MAX_DEVICES = 4

COMMAND_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"
IMU_UUID = "19B10004-E8F2-537E-4F6C-D104768A1214"
START_CMD = b"\x01"
STOP_CMD = b"\x00"

CHANNELS = ["ax", "ay", "az", "gx", "gy", "gz"]
# Fixed-point divisors.
SCALES = {"ax": 1000.0, "ay": 1000.0, "az": 1000.0,
          "gx": 8.0, "gy": 8.0, "gz": 8.0}

NOMINAL_HZ = 100

# 16 bytes: t_ms, ax, ay, az, gx, gy, gz
IMU_PACKET = struct.Struct("<I6h")      

# Trial timing (seconds)
COUNTDOWN_S = 3
LEAD_IN_S = 1.0
PUNCH_S = 1.0
REST_S = 2.0
REPS_PER_TRIAL = 1
IDLE_TRIAL_S = 10.0 

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
        self.t_dev = []   # Arduino timestamp
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
    results = await asyncio.gather(*(device.client.connect() for device in candidates), return_exceptions=True)
    for device, res in zip(candidates, results):
        if isinstance(res, Exception) or not device.client.is_connected:
            print(f"  FAILED  {device.name} ({device.address}): {res}")
        else:
            print(f"  OK      {device.name} ({device.address})")
            devices.append(device)
    print(f"{len(devices)} device(s) connected.")


# --------------------------------- Timeline ---------------------------------
def build_timeline(label):
    if label == "rest":
        return [("rest", 0.0, IDLE_TRIAL_S)], IDLE_TRIAL_S

    timeline = [("rest", 0.0, LEAD_IN_S)]
    current_t = LEAD_IN_S
    for _ in range(REPS_PER_TRIAL):
        timeline.append((label, current_t, current_t + PUNCH_S))
        current_t += PUNCH_S
        rest = REST_S
        timeline.append(("rest", current_t, current_t + rest))
        current_t += rest
    return timeline, current_t


# --------------------------------- Delay -------------------------------------
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
    # Collect Arduino data asynchronously 
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
    # Stop Arduino Connection
    await asyncio.gather(*(device.stop_stream() for device in devices), return_exceptions=True)

    # MongoDB data
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

        print(f"    {device.name}: {n} samples (~{hz:.1f} Hz)")

        doc["devices"][device.name] = IMU_entry
    return doc


# ----------------------------------- Menu -----------------------------------
async def ask(prompt):
    return input(prompt).strip()


def load_normalisation(path):
    """JSON mapping feature names to participant mean and sample std."""
    with open(path, encoding="utf-8") as source:
        stats = json.load(source)
    mean = pd.Series(stats["mean"]).reindex(FEATURE_COLUMNS)
    std = pd.Series(stats["std"]).reindex(FEATURE_COLUMNS)
    if not np.isfinite(mean).all() or not np.isfinite(std).all() or (std < 0).any():
        raise ValueError("Calibration file doesn't match the current features. Re-run calibration.")
    return mean, std


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
            print(f"{n}. Real-time inference (one-second windows)")
            inference_choice = str(n)
            print(f"{n + 1}. Calibrate participant for real-time inference")
            calibration_choice = str(n + 1)
            print("0. Exit")

            choice = await ask("Enter selection: ")

            if choice == "1":
                await scan_and_connect(devices)

            elif choice == "2":
                meta_data["participant"] = (await ask("Participant ID: ")) or meta_data["participant"]
                meta_data["session"] = (await ask("Session ID: ")) or meta_data["session"]

            elif choice == inference_choice:
                try:
                    default_path = calibration_path(meta_data["participant"])
                    if default_path.exists():
                        print(f"Using saved calibration for {meta_data['participant']}.")
                        stats_path = default_path
                    else:
                        print(f"No saved calibration for {meta_data['participant']}; use option {calibration_choice} first.")
                        stats_path = (await ask("Or enter an existing normalisation JSON path (Enter to cancel): ")).strip('"')
                        if not stats_path:
                            continue

                    mean, std = load_normalisation(stats_path)
                    await run_inference(devices, mean, std)

                except Exception as e:
                    print("Inference error:", e)

            elif choice == calibration_choice:
                try:
                    await run_calibration(devices, meta_data.copy(), run_trial)
                except Exception as e:
                    print("Calibration error:", e)

            elif choice in options:
                connected = [device for device in devices if device.client.is_connected]
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

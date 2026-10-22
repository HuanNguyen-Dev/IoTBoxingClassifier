"""One-second, two-hand inference using raw buffers and the new feature set."""

import asyncio
import datetime
import json
import re
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import lfilter_zi

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_training.preprocessing import (
    CHANNELS, ALL_CHANNELS, SUFFIXES, FEATURE_COLUMNS, WIN, WIN_S, ALPHA, STRIDE_S,
    lowpass, add_magnitudes, window_features, normalise)

# LabelEncoder sorts the training labels alphabetically.
LABELS = sorted(["left_jab", "right_jab", "left_hook", "right_hook",
                 "left_uppercut", "right_uppercut", "rest"])
MODEL_PATH = Path(__file__).resolve().parents[1] / "model_training" / "deployment_model-w0.75-p0.8.pkl"
MIN_SAMPLES_PER_SECOND = 50 
 # calibration window stride
CAL_STRIDE_S = 0.25                 
_ZI = lfilter_zi([ALPHA], [1, - (1 - ALPHA)])

# ----------------------------- Extract Features -----------------------------
def feature_frame(windows):
    sig = {}
    for suffix in SUFFIXES:
        for c in CHANNELS:
            x = np.asarray(windows[suffix][c], dtype=float)
            # start the filter at the first sample so there is no start-up ramp
            y, _ = lowpass(x, zi=_ZI * x[0])
            sig[f"{c}{suffix}"] = y
    sig = add_magnitudes(sig)
    row = window_features(sig, 0, None)
    # 'energy' is a sum, so rescale it to a WIN-sample window
    for suffix in SUFFIXES:
        n = len(windows[suffix]["ax"])
        for ch in ALL_CHANNELS:
            row[f"{ch}{suffix}_energy"] *= WIN / n
    return pd.DataFrame([row])[FEATURE_COLUMNS]

# ----------------------------- Calibration -----------------------------
def calibration_path(participant):
    if not participant or not participant.strip():
        raise ValueError("Enter a participant ID before calibrating.")

    return (Path(__file__).resolve().parent / "calibrations" / f"{participant.strip()}_calibration.json")


def connected_hands(devices):
    hands = {}
    for suffix, hand in zip(SUFFIXES, ("Left", "Right")):
        matches = [d for d in devices if d.client.is_connected and hand in d.name]
        if len(matches) != 1:
            raise ValueError(f"Connect exactly one {hand} Arduino first.")
        hands[suffix] = matches[0]
    return hands


async def run_calibration(devices, meta_data, run_trial):
    hands = connected_hands(devices)
    frames = []
    punches = [label for label in LABELS if label != "rest"]

    print("Calibration: rest, then three trials of each punch (about 2.5 minutes).")
    print("Follow the countdown and punch once when prompted; rest between punches.")
    schedule = ["rest"] + [label for label in punches for _ in range(3)]

    index = 1
    while index <= len(schedule):
        label = schedule[index - 1]
        print(f"\nCalibration trial {index}/{len(schedule)}: {label}")
        if not input("Type y to record, or press Enter to exit calibration: ").strip():
            print("Calibration cancelled; previous calibration was kept.")
            return

        try:
            doc = await run_trial(list(hands.values()), label, meta_data, index)
        finally:
            await asyncio.gather(*(d.stop_stream() for d in hands.values()), return_exceptions=True)

        if input("Keep this trial? [Y/n]: ").strip().lower() in ("n", "no"):
            print("Discarded. Repeat this calibration trial.")
            continue

        for segment in doc["label_timeline"]:
            seg_len = segment["end"] - segment["start"]
            if seg_len < WIN_S:
                continue

            w = segment["start"]
            while w + WIN_S <= segment["end"] + 1e-9:
                windows = {}
                for suffix, device in hands.items():
                    if not device.client.is_connected:
                        raise RuntimeError(f"{device.name} disconnected; calibration was not saved.")

                    data = doc["devices"][device.name]
                    times = np.asarray(data["t_dev_ms"])
                    # Get the exact number of values that match the number of timestamps
                    start = np.searchsorted(times, w * 1000, side="left")
                    end = np.searchsorted(times, (w + WIN_S) * 1000, side="left")
                    windows[suffix] = {c: data[c][start:end] for c in CHANNELS}

                frames.append(feature_frame(windows))
                w += CAL_STRIDE_S

        index += 1

    if not frames:
        raise ValueError("Calibration produced no windows; repeat calibration.")

    features = pd.concat(frames, ignore_index=True)
    mean, std = features.mean(), features.std(ddof=1)
    if not np.isfinite(mean).all() or not np.isfinite(std).all():
        raise ValueError("Calibration produced invalid features; previous calibration was kept.")

    path = calibration_path(meta_data["participant"])
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = { "participant": meta_data["participant"], "session": meta_data["session"],
             "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "feature_rows": len(features), "mean": mean.to_dict(), "std": std.to_dict()
            }

    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(stats, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    print(f"Calibration saved for {meta_data['participant']}: {path}")
    return path

# ----------------------------- Real-Time Inferrencing -----------------------------
async def run_inference(devices, mean, std, model_path=MODEL_PATH):
    hands = connected_hands(devices)
    with open(model_path, "rb") as source:
        model = pickle.load(source)
        
    model_cols = list(model.feature_names_in_)
    if set(model_cols) != set(FEATURE_COLUMNS):
        raise ValueError("Model features don't match preprocessing.py.")
    mean, std = mean.reindex(model_cols), std.reindex(model_cols)

    started = []
    try:
        for device in hands.values():
            device.reset()
            started.append(device)
            await device.start_stream()

        print("Real-time inference: Press Enter to stop.")
        stop = asyncio.create_task(asyncio.to_thread(input))
        deadline = time.perf_counter() + WIN_S
        try:
            while not stop.done():
                done, _ = await asyncio.wait({stop}, timeout=max(0, deadline - time.perf_counter()))
                if done:
                    break
                windows = {}
                counts = []

                for suffix, device in hands.items():
                    if not device.client.is_connected:
                        raise RuntimeError(f"{device.name} disconnected.")

                    t = np.asarray(device.t)
                    start = np.searchsorted(t, deadline - WIN_S)
                    end = np.searchsorted(t, deadline)
                    counts.append(int(end - start))
                    windows[suffix] = {c: device.data[c][start:end] for c in CHANNELS}

                    # keep everything the next window still needs
                    trim = np.searchsorted(t, deadline + STRIDE_S - WIN_S)
                    del device.t_dev[:trim]
                    del device.t[:trim]
                    for channel in CHANNELS:
                        del device.data[channel][:trim]

                stamp = datetime.datetime.now().strftime("%H:%M:%S")
                if min(counts) < MIN_SAMPLES_PER_SECOND:
                    print(f"[{stamp}] Skipped: insufficient samples (Left={counts[0]}, Right={counts[1]}).")
                else:
                    frame = normalise(feature_frame(windows)[model_cols], mean, std)
                    if not np.isfinite(frame.to_numpy()).all():
                        print(f"[{stamp}] Skipped: non-finite features.")
                    else:
                        proba = model.predict_proba(frame)[0]
                        prediction = model.classes_[proba.argmax()]
                        label = LABELS[int(prediction)] if isinstance(prediction, (int, np.integer)) else str(prediction)
                        print(f"[{stamp}] {label} ({proba.max():.0%}) | Left={counts[0]}, Right={counts[1]} samples")

                deadline += WIN_S

        except Exception as error:
            print("Inference error:", error)
            
        finally:
            await asyncio.gather(*(d.stop_stream() for d in started), return_exceptions=True)
            started.clear()
            if not stop.done():
                print("Press Enter to return to the menu.")
            await stop

    finally:
        await asyncio.gather(*(d.stop_stream() for d in started), return_exceptions=True)

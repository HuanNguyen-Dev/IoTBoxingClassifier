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
    CHANNELS, ALL_CHANNELS, SUFFIXES, FEATURE_COLUMNS, WIN, WIN_S, ALPHA,
    lowpass, add_magnitudes, window_features, normalise)

# LabelEncoder sorts the training labels alphabetically.
LABELS = sorted(["left_jab", "right_jab", "left_hook", "right_hook",
                 "left_uppercut", "right_uppercut", "rest"])
MODEL_PATH = Path(__file__).resolve().parents[1] / "model_training" / "deployment_model-w0.75-p0.8.pkl"
MIN_SAMPLES_PER_SECOND = 70          # observed rate is 75-85 Hz
CAL_STRIDE_S = 0.25                  # calibration window stride
_ZI = lfilter_zi([ALPHA], [1, -(1 - ALPHA)])


def feature_frame(windows):
    """windows: {'_l': {channel: samples}, '_r': {...}} of raw samples -> 1-row DataFrame."""
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


def calibration_path(participant):
    identifier = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', participant).strip(' .')
    if not identifier:
        raise ValueError("Enter a participant ID before calibrating.")
    return Path(__file__).resolve().parent / "calibrations" / f"{identifier}_calibration.json"


def connected_hands(devices):
    hands = {}
    for suffix, hand in zip(SUFFIXES, ("Left", "Right")):
        matches = [d for d in devices if d.client.is_connected and hand in d.name]
        if len(matches) != 1:
            raise ValueError(f"Connect exactly one {hand} Arduino first.")
        hands[suffix] = matches[0]
    return hands


async def run_calibration(devices, meta_data, run_trial):
    """Collect windows shaped like live inference without writing trials to MongoDB."""
    hands = connected_hands(devices)
    frames = []
    punches = [label for label in LABELS if label != "rest"]
    print("Calibration: rest, then three trials of each punch (about 2.5 minutes).")
    print("Follow the countdown and punch once when prompted; rest between punches.")
    schedule = ["rest"] + [label for label in punches for _ in range(3)]
    for index, label in enumerate(schedule, 1):
        print(f"\nCalibration trial {index}/{len(schedule)}: {label}")
        try:
            doc = await run_trial(list(hands.values()), label, meta_data, index)
        finally:
            await asyncio.gather(*(d.stop_stream() for d in hands.values()), return_exceptions=True)
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
                    start = np.searchsorted(times, w * 1000, side="left")
                    end = np.searchsorted(times, (w + WIN_S) * 1000, side="left")
                    if end - start < MIN_SAMPLES_PER_SECOND * WIN_S:
                        raise ValueError(f"Too few samples from {device.name}; repeat calibration.")
                    windows[suffix] = {c: data[c][start:end] for c in CHANNELS}
                frames.append(feature_frame(windows))
                w += CAL_STRIDE_S
    if not frames:
        raise ValueError("Calibration produced no windows; repeat calibration.")
    features = pd.concat(frames, ignore_index=True)
    mean, std = features.mean(), features.std(ddof=1)
    if not np.isfinite(mean).all() or not np.isfinite(std).all():
        raise ValueError("Calibration produced invalid features; previous calibration was kept.")
    path = calibration_path(meta_data["participant"])
    path.parent.mkdir(parents=True, exist_ok=True)
    stats = {"participant": meta_data["participant"], "session": meta_data["session"],
             "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "feature_rows": len(features), "mean": mean.to_dict(), "std": std.to_dict()}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(stats, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    print(f"Calibration saved for {meta_data['participant']}: {path}")
    return path


def load_normalisation(path):
    """JSON mapping feature names to participant mean and sample std."""
    with open(path, encoding="utf-8") as source:
        stats = json.load(source)
    mean = pd.Series(stats["mean"]).reindex(FEATURE_COLUMNS)
    std = pd.Series(stats["std"]).reindex(FEATURE_COLUMNS)
    if not np.isfinite(mean).all() or not np.isfinite(std).all() or (std < 0).any():
        raise ValueError("Calibration file doesn't match the current features. Re-run calibration.")
    return mean, std


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
        deadline = time.perf_counter() + 1.0
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
                    start = np.searchsorted(t, deadline - 1.0)
                    end = np.searchsorted(t, deadline)
                    counts.append(int(end - start))
                    # features use the last WIN_S seconds of the one-second chunk
                    s2 = np.searchsorted(t, deadline - WIN_S)
                    windows[suffix] = {c: device.data[c][s2:end] for c in CHANNELS}
                    del device.t_dev[:end]
                    del device.t[:end]
                    for channel in CHANNELS:
                        del device.data[channel][:end]
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
                deadline += 1.0
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
"""One-second, two-hand inference using the training notebook's features."""

import asyncio
import datetime
import json
import re
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from model_training.preprocessing import CHANNELS, SUFFIXES, FEATURE_COLUMNS, feature_frame, normalise
# LabelEncoder sorts the training labels alphabetically.
LABELS = sorted(["left_jab", "right_jab", "left_hook", "right_hook",
                 "left_uppercut", "right_uppercut", "rest"])
MODEL_PATH = Path(__file__).resolve().parents[1] / "model_training" / "deployment_model.pkl"
# Observed collection rate is 75–85 Hz; allow some missing samples.
MIN_SAMPLES_PER_SECOND = 70


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
    """Collect training-shaped segments without writing trials to MongoDB."""
    import numpy as np
    import pandas as pd

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
            windows = {}
            for suffix, device in hands.items():
                if not device.client.is_connected:
                    raise RuntimeError(f"{device.name} disconnected; calibration was not saved.")
                data = doc["devices"][device.name]
                times = np.asarray(data["t_dev_ms"])
                start = np.searchsorted(times, segment["start"] * 1000, side="left")
                end = np.searchsorted(times, segment["end"] * 1000, side="left")
                duration = segment["end"] - segment["start"]
                if end - start < MIN_SAMPLES_PER_SECOND * duration:
                    raise ValueError(f"Too few samples from {device.name}; repeat calibration.")
                windows[suffix] = {c: data[c][start:end] for c in CHANNELS}
            frames.append(feature_frame(windows))
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
    import numpy as np
    import pandas as pd

    with open(path, encoding="utf-8") as source:
        stats = json.load(source)
    mean = pd.Series(stats["mean"]).reindex(FEATURE_COLUMNS)
    std = pd.Series(stats["std"]).reindex(FEATURE_COLUMNS)
    if not np.isfinite(mean).all() or not np.isfinite(std).all() or (std < 0).any():
        raise ValueError("Normalisation needs a finite mean and nonnegative std for all 84 features.")
    return mean, std


async def run_inference(devices, mean, std, model_path=MODEL_PATH):
    import numpy as np

    hands = connected_hands(devices)
    with open(model_path, "rb") as source:
        model = pickle.load(source)
    if list(getattr(model, "feature_names_in_", [])) != FEATURE_COLUMNS:
        raise ValueError("The model's feature names/order do not match the training notebook.")

    started = []
    try:
        for device in hands.values():
            device.reset()
            started.append(device)
            await device.start_stream()
        print("Real-time inference: Press Enter to stop.")
        stop = asyncio.create_task(asyncio.to_thread(input))
        deadline = time.perf_counter() + 1.0
        # Partition received samples into one-second windows for both hands.
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
                    start = np.searchsorted(device.t, deadline - 1.0)
                    end = np.searchsorted(device.t, deadline)
                    windows[suffix] = {c: device.data[c][start:end] for c in CHANNELS}
                    counts.append(int(end - start))
                    del device.t_dev[:end]
                    del device.t[:end]
                    for channel in CHANNELS:
                        del device.data[channel][:end]
                stamp = datetime.datetime.now().strftime("%H:%M:%S")
                if min(counts) < MIN_SAMPLES_PER_SECOND:
                    print(f"[{stamp}] Skipped: insufficient samples (Left={counts[0]}, Right={counts[1]}).")
                else:
                    frame = normalise(feature_frame(windows), mean, std)
                    prediction = model.predict(frame)[0]
                    label = LABELS[int(prediction)] if isinstance(prediction, (int, np.integer)) else str(prediction)
                    confidence = float(model.predict_proba(frame).max())
                    print(f"[{stamp}] {label} ({confidence:.0%}) | Left={counts[0]}, Right={counts[1]} samples")
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

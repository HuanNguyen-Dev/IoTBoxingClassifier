"""Preprocessing shared by model_training.ipynb and live inference."""

import numpy as np
import pandas as pd

CHANNELS = ["ax", "ay", "az", "gx", "gy", "gz"]
SUFFIXES = ["_l", "_r"]
SAMPLING_PERIOD = 0.01
FEATURES = ["mean", "std", "min", "max", "range", "median", "rms"]
FEATURE_COLUMNS = [f"{c}{s}_{f}" for s in SUFFIXES for c in CHANNELS for f in FEATURES]
HAND_NAMES = {"_l": "Left", "_r": "Right"}
HAND_FEATURE_COLUMNS = {
    suffix: [f"{channel}{suffix}_{feature}" for channel in CHANNELS for feature in FEATURES]
    for suffix in SUFFIXES
}
HAND_LABELS = {
    "_l": ["left_jab", "left_hook", "left_uppercut", "rest"],
    "_r": ["right_jab", "right_hook", "right_uppercut", "rest"],
}

# Copied from notebook cell 9.
def IIR_filter(value, prev, alpha):
    return alpha * value + (1 - alpha) * prev


def filter_signal(signal):
    data = list(signal)
    alpha = SAMPLING_PERIOD / (0.05 + SAMPLING_PERIOD)
    prev = 0
    for i, record in enumerate(data):
        data[i] = IIR_filter(record, prev, alpha)
        prev = record
    return data


# Copied from notebook cell 12.
def extract_features(signal):
    signal = np.asarray(signal)
    if len(signal) == 0:
        return {
            'mean': np.nan,
            'std': np.nan,
            'min': np.nan,
            'max': np.nan,
            'range': np.nan,
            'median': np.nan,
            'rms': np.nan,
        }
    return {
        'mean': np.mean(signal),
        'std': np.std(signal),
        'min': np.min(signal),
        'max': np.max(signal),
        'range': np.max(signal) - np.min(signal),
        'median': np.median(signal),
        'rms': np.sqrt(np.mean(signal ** 2)),
    }


def feature_frame(windows):
    """The notebook's filtering and feature loops, for one two-hand segment."""
    row = {}
    for suffix in SUFFIXES:
        for channel in CHANNELS:
            signal = filter_signal(windows[suffix][channel])
            for name, value in extract_features(signal).items():
                row[f'{channel}{suffix}_{name}'] = value
    return pd.DataFrame([row], columns=FEATURE_COLUMNS)


def normalise(features, mean, std):
    return (features - mean) / (std + 1e-8)


def per_user_norm(df):
    out = df.copy()
    for participant, row in df.groupby('participant'):
        out.loc[row.index, FEATURE_COLUMNS] = normalise(
            row[FEATURE_COLUMNS], row[FEATURE_COLUMNS].mean(), row[FEATURE_COLUMNS].std())
    return out

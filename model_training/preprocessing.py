"""Preprocessing shared by model_training.ipynb and live inference."""

import numpy as np
import pandas as pd
from itertools import combinations
from scipy.stats import skew, kurtosis

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

FS = 100 # sampling rate in Hz, set to your actual rate

def freq_features(signal, fs=FS, n_fft=256):
    signal = np.asarray(signal, dtype=float)
    if len(signal) < 4:
        return {'dom_freq': np.nan, 'dom_amp': np.nan, 'abs_diff': np.nan}

    # Remove DC offset so gravity/orientation doesn't dominate the spectrum
    x = signal - signal.mean()

    # Hann window reduces spectral leakage from the window edges
    x = x * np.hanning(len(x))

    # Zero-padding gives finer frequency spacing (1 Hz -> ~0.2 Hz at 50 Hz, n_fft=256)
    spectrum = np.abs(np.fft.rfft(x, n=n_fft)) / len(x)
    freqs = np.fft.rfftfreq(n_fft, d=1/fs)

    # Skip bin 0 (DC)
    idx = np.argmax(spectrum[1:]) + 1

    return {
        'dom_freq': freqs[idx], # frequency
        'dom_amp': spectrum[idx],  # frequency amplitude
    }

def safe_corr(a, b, eps=1e-9):
    # Pearson correlation that returns 0 instead of NaN when a channel is flat
    a = a - a.mean()
    b = b - b.mean()
    denom = np.sqrt(np.sum(a**2) * np.sum(b**2))
    if denom < eps:
        return 0.0
    return float(np.sum(a * b) / denom)

def correlation_features(win, channels=CHANNELS, prefix=''):
    # win: (n_samples, n_channels), columns in the same order as `channels`
    feats = {}
    for i, j in combinations(range(len(channels)), 2):
        feats[f'{prefix}corr_{channels[i]}_{channels[j]}'] = safe_corr(win[:, i], win[:, j])
    return feats

def extract_features(signal):
    signal = np.asarray(signal, dtype=float)
    n = len(signal)
    keys = ['mean','std','min','max','range','median','rms','energy',
            'argmax_pos','argmin_pos','skew','kurtosis','dom_freq','dom_amp']
    if n < 4:
        return {k: np.nan for k in keys}

    flat = signal.std() < 1e-9   # skew/kurtosis are undefined for a constant signal
    return {
        'mean': signal.mean(),
        'std': signal.std(),
        'min': signal.min(),
        'max': signal.max(),
        'range': np.ptp(signal),
        'median': np.median(signal),
        'rms': np.sqrt(np.mean(signal**2)),
        'energy': np.sum(signal**2),
        'argmax_pos': np.argmax(signal) / n,
        'argmin_pos': np.argmin(signal) / n,
        'skew': 0.0 if flat else skew(signal),
        'kurtosis': 0.0 if flat else kurtosis(signal),
        **freq_features(signal),
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

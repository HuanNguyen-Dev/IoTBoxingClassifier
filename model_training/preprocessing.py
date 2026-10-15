"""Preprocessing shared by model_training.ipynb and live inference."""

import numpy as np
import pandas as pd
from itertools import combinations
from scipy.signal import lfilter

from scipy.stats import skew, kurtosis

CHANNELS = ["ax", "ay", "az", "gx", "gy", "gz"]
MAG_CHANNELS = ['accmag', 'gyrmag']
ALL_CHANNELS = CHANNELS + MAG_CHANNELS

SUFFIXES = ["_l", "_r"]

STAT_NAMES = ['mean','std','min','max','range','median','rms','energy',
              'argmax_pos','argmin_pos','skew','kurtosis','dom_freq','dom_amp']

STAT_COLUMNS = [f"{s}{sfx}_{f}" for sfx in SUFFIXES for s in ALL_CHANNELS for f in STAT_NAMES]
CORR_COLUMNS = [f"{suffix[1:]}_corr_{a}_{b}" for suffix in SUFFIXES for a, b in combinations(ALL_CHANNELS, 2)]
FEATURE_COLUMNS = STAT_COLUMNS + CORR_COLUMNS 

SAMPLING_PERIOD = 0.01
FS = 1 / SAMPLING_PERIOD
WIN_S, STRIDE_S = 0.75, 0.1
WIN, STRIDE = int(WIN_S * FS), int(STRIDE_S * FS)   # 75, 10
MIN_PURITY = 0.8

ALPHA = SAMPLING_PERIOD / (0.05 + SAMPLING_PERIOD)

# ----------------------------- Feature Functions -----------------------------

def lowpass(x, zi=None):
    b, a = [ALPHA], [1, -(1 - ALPHA)]
    if zi is None:
        return lfilter(b, a, x)
    return lfilter(b, a, x, zi=zi)


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


# ----------------------------- Window Functions -----------------------------

def window_features(sig, s, e):
    feat = {}

    # Single channel features
    for key, x in sig.items():
        for name, v in extract_features(x[s:e]).items():
            feat[f'{key}_{name}'] = v
            
    # Cross channel features
    for suffix in ('_l', '_r'):
        win = np.column_stack([sig[f'{ch}{suffix}'][s:e] for ch in ALL_CHANNELS])
        feat.update(correlation_features(win, channels=ALL_CHANNELS, prefix=suffix[1:] + '_'))
        
    return feat

def to_grid(row):
    """Both hands on a common 100 Hz grid + per-sample labels."""
    t_l = np.asarray(row['t_dev_ms_l'], float)
    t_r = np.asarray(row['t_dev_ms_r'], float)
    grid = np.arange(max(t_l[0], t_r[0]), min(t_l[-1], t_r[-1]), 1000 / FS)

    sig = {}
    for suffix, t in (('_l', t_l), ('_r', t_r)):
        for ch in CHANNELS:
            sig[f'{ch}{suffix}'] = np.interp(grid, t, np.asarray(row[f'{ch}{suffix}'], float))

    labels = np.full(len(grid), None, dtype=object)
    for seg in row.get('label_timeline', []):
        m = (grid >= seg['start'] * 1000) & (grid < seg['end'] * 1000)
        labels[m] = seg['label']
    return sig, labels


def add_magnitudes(sig):
    for suffix in ('_l', '_r'):
        sig[f'accmag{suffix}'] = np.sqrt(sum(sig[f'{a}{suffix}']**2 for a in ('ax', 'ay', 'az')))
        sig[f'gyrmag{suffix}'] = np.sqrt(sum(sig[f'{g}{suffix}']**2 for g in ('gx', 'gy', 'gz')))
    return sig


def prepare_signals(row, filt=True):
    sig, labels = to_grid(row)
    if filt:
        sig = {k: lowpass(v) for k, v in sig.items()}
    return add_magnitudes(sig), labels


def make_windows(row, record_id, WIN=WIN, filt=True):
    sig, labels = prepare_signals(row, filt)
    out = []

    for s in range(0, len(labels) - WIN + 1, STRIDE):
        w_labels = labels[s:s + WIN]
        valid = w_labels[w_labels != None]
        if len(valid) < MIN_PURITY * WIN:
            continue

        vals, counts = np.unique(valid, return_counts=True)
        label, purity = vals[counts.argmax()], counts.max() / WIN
        if purity < MIN_PURITY:
            continue

        feat = {'record_id': record_id, 'participant': row['participant'],
                'label': label, 'purity': purity, 't_start': s / FS}
        feat.update(window_features(sig, s, s + WIN))
        out.append(feat)

    return out


def latest_window_frame(row):
    sig, _ = prepare_signals(row)
    n = len(next(iter(sig.values())))
    if n < WIN:
        return None
    return pd.DataFrame([window_features(sig, n - WIN, n)])


# ----------------------------- Normalisation Functions -----------------------------

def normalise(features, mean, std):
    return (features - mean) / (std + 1e-8)

def per_user_norm(df):
    out = df.copy()
    for participant, row in df.groupby('participant'):
        out.loc[row.index, FEATURE_COLUMNS] = normalise(
            row[FEATURE_COLUMNS], row[FEATURE_COLUMNS].mean(), row[FEATURE_COLUMNS].std())
    return out

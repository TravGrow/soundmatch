"""Audio feature extraction and fingerprinting.

Two independent representations are computed for every track:

1. A compact feature vector (timbre / harmony / rhythm / energy) used to rank
   how *similar* two different recordings sound.
2. A constellation fingerprint (Shazam-style spectral peak pairs) used to detect
   whether two files are literally the *same* recording, surviving re-encoding,
   volume changes and trimming.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, asdict

import numpy as np

# Bump whenever feature extraction changes in a way that makes previously
# cached Features incomparable with freshly computed ones. Mixing the two
# silently produces wrong tempos, wrong queries and wrong rankings.
FEATURE_VERSION = 3

SR = 22050
# Longest excerpt we analyse. Social clips are short; Pixabay tracks are not.
# Capping keeps analysis fast and comparisons fair.
MAX_EXCERPT_SEC = 60.0
# Fingerprinting scans a much longer span than feature analysis: a short social
# clip may come from anywhere in a full-length track, and the two fingerprints
# can only align where the analysed spans actually overlap.
MAX_FINGERPRINT_SEC = 240.0

# Krumhansl-Schmuckler key profiles, used to estimate a musical key from chroma.
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
_PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


@dataclass
class Features:
    """Everything we know about how one piece of audio sounds."""

    duration: float
    tempo: float
    key: str
    key_index: int
    is_minor: bool
    chroma: list          # 12-dim pitch-class profile
    mfcc_mean: list       # 20-dim timbre centroid
    mfcc_std: list        # 20-dim timbre variability
    centroid: float       # spectral brightness (Hz)
    bandwidth: float
    rolloff: float
    zcr: float
    rms: float            # loudness / energy
    dynamic_range: float
    harmonic_ratio: float  # tonal vs percussive balance
    onset_rate: float      # note events per second -> busyness
    fingerprint: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("fingerprint", None)
        return d


def _excerpt(y: np.ndarray, sr: int) -> np.ndarray:
    """Take the most representative slice: centred, skipping intro/outro."""
    max_len = int(MAX_EXCERPT_SEC * sr)
    if len(y) <= max_len:
        return y
    start = (len(y) - max_len) // 2
    return y[start:start + max_len]


def _estimate_key(chroma_mean: np.ndarray) -> tuple[int, bool, str]:
    """Correlate the pitch-class profile against all 24 major/minor keys."""
    best_score, best_idx, best_minor = -np.inf, 0, False
    for i in range(12):
        for profile, minor in ((_MAJOR_PROFILE, False), (_MINOR_PROFILE, True)):
            rotated = np.roll(profile, i)
            a = chroma_mean - chroma_mean.mean()
            b = rotated - rotated.mean()
            denom = np.linalg.norm(a) * np.linalg.norm(b)
            score = float(np.dot(a, b) / denom) if denom else 0.0
            if score > best_score:
                best_score, best_idx, best_minor = score, i, minor
    name = f"{_PITCH_NAMES[best_idx]}{'m' if best_minor else ''}"
    return best_idx, best_minor, name


# Maps the cheap tonality estimate below onto the scale of a true HPSS
# separation. Fitted against librosa.decompose.hpss over 28 Pixabay tracks
# (r = 0.84, mean absolute error 0.06).
_HR_SLOPE, _HR_INTERCEPT = 2.134, -0.508


def _harmonic_ratio(S: np.ndarray) -> float:
    """Tonal-vs-percussive balance, approximated from the spectrogram.

    A proper HPSS separation costs ~1.8s per track -- more than everything else
    combined. Harmonic content forms horizontal ridges in a spectrogram and
    percussive content forms vertical ones, so smoothing along each axis and
    comparing the energy captures the same contrast with a linear filter instead
    of a median one, then rescales to the HPSS range.
    """
    from scipy.ndimage import uniform_filter1d

    h = uniform_filter1d(S, size=17, axis=1)   # smooth over time -> harmonic
    p = uniform_filter1d(S, size=17, axis=0)   # smooth over frequency -> percussive
    h_energy, p_energy = float(np.sum(h ** 2)), float(np.sum(p ** 2))
    if not (h_energy + p_energy):
        return 0.5
    raw = h_energy / (h_energy + p_energy)
    return float(np.clip(_HR_SLOPE * raw + _HR_INTERCEPT, 0.0, 1.0))


def fingerprint(y: np.ndarray, sr: int, spec: np.ndarray | None = None) -> dict:
    """Constellation fingerprint: hash pairs of spectral peaks.

    Returns a mapping of hash -> list of anchor time offsets. Two recordings of
    the same audio produce many hashes that align at a *constant* time offset,
    which is what makes exact-match detection robust to trimming.

    ``spec`` lets the caller pass in a magnitude spectrogram it already has.
    """
    import librosa

    if spec is None:
        spec = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    log_spec = librosa.amplitude_to_db(spec, ref=np.max)

    # Pick local maxima that stand out from their neighbourhood.
    from scipy.ndimage import maximum_filter

    neighbourhood = maximum_filter(log_spec, size=(30, 30))
    peaks_mask = (log_spec == neighbourhood) & (log_spec > -50)
    freq_idx, time_idx = np.nonzero(peaks_mask)

    # Sort by time so pairing walks forward through the track.
    order = np.argsort(time_idx)
    freq_idx, time_idx = freq_idx[order], time_idx[order]

    hashes: dict[int, list[int]] = defaultdict(list)
    fan_out = 8
    n = len(time_idx)
    for i in range(n):
        f1, t1 = int(freq_idx[i]), int(time_idx[i])
        for j in range(i + 1, min(i + 1 + fan_out, n)):
            f2, t2 = int(freq_idx[j]), int(time_idx[j])
            dt = t2 - t1
            if not (1 <= dt <= 100):
                continue
            # Pack the pair into one int, quantising frequency so that small
            # encoding differences still collide. Plain ints keep the cached
            # fingerprints far smaller and faster to compare than hex digests.
            hashes[((f1 >> 1) << 22) | ((f2 >> 1) << 12) | dt].append(t1)
    return dict(hashes)


def analyze(path: str, want_fingerprint: bool = True) -> Features:
    """Load an audio file and compute its full description."""
    import librosa

    y, sr = librosa.load(path, sr=SR, mono=True)
    if y.size == 0:
        raise ValueError(f"no audio samples decoded from {path}")
    full_duration = float(len(y) / sr)

    # Normalise so loudness differences don't distort timbre comparisons.
    peak = float(np.max(np.abs(y))) or 1.0
    y_full = y / peak
    y_norm = _excerpt(y_full, sr)

    # One STFT feeds nearly every feature below. Computing it once (rather than
    # letting each librosa call redo it) is what keeps a full analysis around a
    # second, which in turn lets us examine every candidate rather than a guess.
    S = np.abs(librosa.stft(y_norm, n_fft=2048, hop_length=512))
    power = S ** 2

    mel = librosa.feature.melspectrogram(S=power, sr=sr)
    mel_db = librosa.power_to_db(mel)
    mfcc = librosa.feature.mfcc(S=mel_db, n_mfcc=20)

    # Match how librosa.beat.beat_track builds its own envelope internally: from
    # the mel spectrogram, aggregated with a median. Using the linear STFT or a
    # mean here yields double-time tempo estimates on a fraction of tracks, and
    # a wrong tempo poisons both the search queries and the ranking.
    onset_env = librosa.onset.onset_strength(S=mel_db, sr=sr, aggregate=np.median)
    tempo = float(np.atleast_1d(
        librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)[0])[0])

    chroma = librosa.feature.chroma_stft(S=power, sr=sr)
    chroma_mean = chroma.mean(axis=1)
    key_idx, is_minor, key_name = _estimate_key(chroma_mean)
    centroid = librosa.feature.spectral_centroid(S=S, sr=sr)
    bandwidth = librosa.feature.spectral_bandwidth(S=S, sr=sr)
    rolloff = librosa.feature.spectral_rolloff(S=S, sr=sr)
    zcr = librosa.feature.zero_crossing_rate(y_norm)
    rms = librosa.feature.rms(S=S)

    harmonic_ratio = _harmonic_ratio(S)

    onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, units="time")
    onset_rate = float(len(onsets) / (len(y_norm) / sr)) if len(y_norm) else 0.0

    rms_flat = rms.flatten()
    dynamic_range = float(np.percentile(rms_flat, 95) - np.percentile(rms_flat, 5))

    return Features(
        duration=full_duration,
        tempo=round(tempo, 2),
        key=key_name,
        key_index=key_idx,
        is_minor=is_minor,
        chroma=[round(float(v), 6) for v in chroma_mean],
        mfcc_mean=[round(float(v), 4) for v in mfcc.mean(axis=1)],
        mfcc_std=[round(float(v), 4) for v in mfcc.std(axis=1)],
        centroid=round(float(centroid.mean()), 2),
        bandwidth=round(float(bandwidth.mean()), 2),
        rolloff=round(float(rolloff.mean()), 2),
        zcr=round(float(zcr.mean()), 6),
        rms=round(float(rms.mean()), 6),
        dynamic_range=round(dynamic_range, 6),
        harmonic_ratio=round(harmonic_ratio, 4),
        onset_rate=round(onset_rate, 3),
        fingerprint=(fingerprint(y_full[:int(MAX_FINGERPRINT_SEC * sr)], sr)
                     if want_fingerprint else {}),
    )

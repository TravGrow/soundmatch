"""Disk-cached feature extraction.

Kept in its own module with no FastAPI imports so that worker processes can
import it cheaply -- spawning a process re-imports whatever module the target
function lives in, and pulling the whole web app into every worker would be
both slow and circular.
"""
from __future__ import annotations

import pickle
from pathlib import Path

from analyze import FEATURE_VERSION, Features, analyze

FEATS_DIR = Path(__file__).resolve().parent.parent / "cache" / "features"
FEATS_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(audio_path: str) -> Path:
    p = Path(audio_path)
    # Include the parent directory: every job writes its clip as "source.wav",
    # so stem+size alone would collide across jobs.
    return FEATS_DIR / f"v{FEATURE_VERSION}_{p.parent.name}_{p.stem}_{p.stat().st_size}.pkl"


def analyze_cached(audio_path: str) -> Features:
    """Analyse a file, reusing a previous result when the file is unchanged.

    Pixabay tracks recur across searches, so this saves a lot of repeat DSP.
    """
    cp = _cache_path(audio_path)
    if cp.exists():
        try:
            return pickle.loads(cp.read_bytes())
        except Exception:
            pass    # a corrupt cache entry should just be recomputed
    f = analyze(audio_path)
    try:
        cp.write_bytes(pickle.dumps(f))
    except Exception:
        pass
    return f


def warm():
    """Pre-import the heavy DSP stack so the first real task isn't slowed by it."""
    import librosa  # noqa: F401
    import numpy as np

    librosa.feature.mfcc(S=librosa.power_to_db(np.ones((128, 16))), n_mfcc=20)

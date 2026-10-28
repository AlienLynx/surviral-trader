"""
kronos-signal -- the model adapter (the only file that imports torch / Kronos). CPU only, offline at run time: weights and code are baked into
the image at pinned revisions (see the Dockerfile), HF_HUB_OFFLINE=1.

Model: NeoQuasar/Kronos-small (24.7M parameters, context 512) + NeoQuasar/Kronos-Tokenizer-base. MIT licence.
Determinism: torch.manual_seed(SEED) before every forecast; the forecast of a pair is the mean over `samples` parallel samples (done inside the
library). Measured on a dev machine (2 CPU threads, 3 samples, 480 candles in, 24 out): about 10 s per pair, ~420 MB resident.
"""
from __future__ import annotations

import os
import sys

from .core import LOOKBACK, PRED_LEN, SEED

MODEL_DIR = os.environ.get("KRONOS_MODEL_DIR", "/models/small")
TOKENIZER_DIR = os.environ.get("KRONOS_TOKENIZER_DIR", "/models/tokenizer")
CODE_DIR = os.environ.get("KRONOS_CODE_DIR", "/opt/kronos")


def load(threads: int, samples: int):
    """Returns (predict, meta). predict(window_rows, t0) -> list of PRED_LEN forecast closes."""
    import numpy as np
    import pandas as pd
    import torch

    sys.path.insert(0, CODE_DIR)
    from model import Kronos, KronosPredictor, KronosTokenizer          # the pinned upstream code

    torch.set_num_threads(max(1, threads))
    tok = KronosTokenizer.from_pretrained(TOKENIZER_DIR)
    mdl = Kronos.from_pretrained(MODEL_DIR)
    tok.eval()
    mdl.eval()
    predictor = KronosPredictor(mdl, tok, device="cpu", max_context=512)

    def predict(window: list[dict], t0) -> list[float]:
        assert len(window) <= LOOKBACK
        df = pd.DataFrame([{k: r[k] for k in ("open", "high", "low", "close", "volume")} for r in window])
        x_ts = pd.Series([pd.Timestamp(r["open_dt"]).tz_localize(None) for r in window])
        y_ts = pd.Series(pd.date_range(x_ts.iloc[-1] + pd.Timedelta(hours=1), periods=PRED_LEN, freq="h"))
        torch.manual_seed(SEED)
        out = predictor.predict(df=df, x_timestamp=x_ts, y_timestamp=y_ts, pred_len=PRED_LEN, T=1.0, top_p=0.9,
                                sample_count=samples, verbose=False)
        closes = out["close"].to_numpy(dtype=float)
        if not np.isfinite(closes).all():
            raise ValueError("non-finite forecast")
        return closes.tolist()

    meta = {"model": "NeoQuasar/Kronos-small", "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
            "model_revision": os.environ.get("KRONOS_MODEL_REV", ""), "tokenizer_revision": os.environ.get("KRONOS_TOKENIZER_REV", ""),
            "code_commit": os.environ.get("KRONOS_CODE_COMMIT", ""), "device": "cpu", "torch": torch.__version__}
    return predict, meta

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOG_DIR", str(Path(__file__).resolve().parent / "_logs"))

from app.strategy.indicators import add_indicators
from app.strategy.price_action import add_price_action


def make_prices(n=600, drift=0.3, vol=1.5, seed=1):
    rng = np.random.default_rng(seed)
    c = 1000 + np.cumsum(rng.normal(drift, vol, n))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + abs(rng.normal(0, .6, n))
    l = np.minimum(o, c) - abs(rng.normal(0, .6, n))
    return pd.DataFrame(dict(time=(np.arange(n) * 300 + 1_700_000_000).astype("int64"), open=o, high=h, low=l, close=c,
                             tick_volume=1, spread=20.0))


@pytest.fixture
def prices():
    return make_prices()


@pytest.fixture
def ind(prices):
    return add_price_action(add_indicators(prices))

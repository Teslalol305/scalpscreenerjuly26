"""OnlineLogistic tests: hand-checked SGD math, real learning, state roundtrip."""

from __future__ import annotations

import pytest

from tapescreen.core.signals.models import FEATURES, K, OnlineLogistic


def vec(**kw: float) -> list[float]:
    x = [0.0] * K
    for name, v in kw.items():
        x[FEATURES.index(name)] = v
    return x


def test_first_update_moves_only_bias() -> None:
    # n=1: Welford mean == x, sd == 0 -> standardized vector is all zeros, so the
    # first sample can only move the bias: b += lr * (y - sigmoid(0)) = 0.5 * 0.5
    m = OnlineLogistic(lr=0.5, l2=0.0)
    p = m.update(vec(vol_z=3.0), won=True)
    assert p == pytest.approx(0.5)
    assert m.b == pytest.approx(0.25)
    assert all(w == 0.0 for w in m.w)
    assert m.n == 1


def test_learns_a_separating_feature() -> None:
    # vol_z=+2 -> win, vol_z=-2 -> loss, alternating: the model must learn the
    # direction and separate the two contexts by a wide probability margin
    m = OnlineLogistic(lr=0.1, l2=0.001)
    for i in range(200):
        if i % 2 == 0:
            m.update(vec(vol_z=2.0), won=True)
        else:
            m.update(vec(vol_z=-2.0), won=False)
    assert m.predict(vec(vol_z=2.0)) > 0.75
    assert m.predict(vec(vol_z=-2.0)) < 0.25
    # weight on vol_z is positive; all others stayed ~0
    assert m.w[FEATURES.index("vol_z")] > 0.2
    assert abs(m.w[FEATURES.index("spread_bps")]) < 0.05


def test_state_roundtrip_and_schema_guard() -> None:
    m = OnlineLogistic(lr=0.1, l2=0.0)
    for i in range(20):
        m.update(vec(vol_z=float(i % 5), strength=0.5), won=i % 3 != 0)
    st = m.to_state()
    m2 = OnlineLogistic.from_state(st, lr=0.1, l2=0.0)
    x = vec(vol_z=2.0, strength=0.5)
    assert m2.predict(x) == pytest.approx(m.predict(x))
    assert m2.n == m.n
    # evolved feature schema -> fresh model rather than mis-mapped weights
    bad = dict(st, features=["something", "else"])
    m3 = OnlineLogistic.from_state(bad, lr=0.1, l2=0.0)
    assert m3.n == 0 and all(w == 0.0 for w in m3.w)


def test_prediction_is_pure() -> None:
    m = OnlineLogistic()
    for i in range(10):
        m.update(vec(vol_z=float(i)), won=True)
    before = m.to_state()
    m.predict(vec(vol_z=99.0))
    assert m.to_state() == before  # predict() must not mutate the model

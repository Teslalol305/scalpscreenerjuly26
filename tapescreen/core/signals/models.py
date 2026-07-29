"""Online logistic regression: per-rule win-probability model over entry context.

Use: m = OnlineLogistic(lr, l2); p = m.predict(x); m.update(x, won);
state = m.to_state() / OnlineLogistic.from_state(state, lr, l2).
Depends on: stdlib only. Pure incremental SGD with running feature
standardization (Welford), so scales differ per symbol/rule without harm and
every resolved signal sharpens the next prediction. Fully inspectable weights.
"""

from __future__ import annotations

import math

FEATURES = [
    "vol_z",          # activity burst at entry
    "tps_z",          # trade-rate burst
    "vwap_stretch",   # sigma distance from session VWAP, signed toward trade side
    "imb_edge",       # top-10 book imbalance edge toward trade side, [-1, 1]
    "flow_edge",      # 60s aggressor imbalance edge toward trade side, [-1, 1]
    "spread_bps",     # cost/liquidity at entry
    "funding_edge",   # funding tailwind (+) or headwind (-) for the side, bps
    "mom_align",      # 5m momentum in trade direction, in sigmas
    "atr_bps",        # volatility regime
    "rsi_c",          # RSI centered, signed toward trade side
    "strength",       # firing rule's own 0-1 strength
    "score",          # composite confluence 0-1
]
K = len(FEATURES)
_EPS = 1e-9


def _sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


class OnlineLogistic:
    """P(win | entry context); one instance per rule."""

    __slots__ = ("b", "l2", "lr", "mean", "n", "var_sum", "w")

    def __init__(self, lr: float = 0.05, l2: float = 0.001) -> None:
        self.lr = lr
        self.l2 = l2
        self.w = [0.0] * K
        self.b = 0.0
        self.n = 0  # resolved samples learned from
        self.mean = [0.0] * K  # Welford running stats for standardization
        self.var_sum = [0.0] * K

    def _z(self, x: list[float]) -> list[float]:
        out = [0.0] * K
        for i in range(K):
            if self.n >= 2:
                sd = math.sqrt(self.var_sum[i] / self.n)
                if sd > _EPS:
                    out[i] = (x[i] - self.mean[i]) / sd
        return out

    def predict(self, x: list[float]) -> float:
        z = self._z(x)
        return _sigmoid(sum(w * v for w, v in zip(self.w, z, strict=True)) + self.b)

    def contributions(self, x: list[float]) -> list[tuple[str, float]]:
        """(feature, weight*z) per input, strongest first - explains a prediction."""
        z = self._z(x)
        pairs = [(FEATURES[i], self.w[i] * z[i]) for i in range(K)]
        pairs.sort(key=lambda p: -abs(p[1]))
        return pairs

    def update(self, x: list[float], won: bool) -> float:
        """One SGD step; returns the pre-update prediction for the sample."""
        self.n += 1
        for i in range(K):  # Welford before normalization: stats include this sample
            d = x[i] - self.mean[i]
            self.mean[i] += d / self.n
            self.var_sum[i] += d * (x[i] - self.mean[i])
        z = self._z(x)
        p = _sigmoid(sum(w * v for w, v in zip(self.w, z, strict=True)) + self.b)
        err = (1.0 if won else 0.0) - p
        for i in range(K):
            self.w[i] += self.lr * (err * z[i] - self.l2 * self.w[i])
        self.b += self.lr * err
        return p

    def to_state(self) -> dict:
        return {"w": self.w, "b": self.b, "n": self.n,
                "mean": self.mean, "var_sum": self.var_sum, "features": FEATURES}

    @classmethod
    def from_state(cls, state: dict, lr: float, l2: float) -> OnlineLogistic:
        m = cls(lr, l2)
        if state.get("features") == FEATURES and len(state.get("w", [])) == K:
            m.w = [float(v) for v in state["w"]]
            m.b = float(state.get("b", 0.0))
            m.n = int(state.get("n", 0))
            m.mean = [float(v) for v in state.get("mean", [0.0] * K)]
            m.var_sum = [float(v) for v in state.get("var_sum", [0.0] * K)]
        return m  # feature-set mismatch -> fresh model (schema evolved)

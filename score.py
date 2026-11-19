"""Score: round(100 × (1 − min(1, weighted / deployed)))."""
from fractions import Fraction
from math import floor

LOW_SAMPLE = 10


def score(leaks, deployed_wei: int, acquisitions: int) -> dict:
    weighted = sum(Fraction(l.weight) * l.wei for l in leaks)
    if deployed_wei <= 0:
        value = 100 if weighted == 0 else 0
    else:
        value = floor(100 * (1 - min(Fraction(1), weighted / deployed_wei)) + Fraction(1, 2))
    return {"value": int(value), "low_sample": acquisitions < LOW_SAMPLE,
            "weighted_wei": int(weighted), "deployed_wei": deployed_wei, "acquisitions": acquisitions}

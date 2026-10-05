"""Reference Gibson input calculations (the 'answer key' for steps 8/26/42).

Standard dsDNA relation: pmol = ng * 1000 / (bp * 650).
HOOK[calc]: swap in the exact rule your kit/protocol uses (equimolar ratios,
vector:insert ratios, total DNA caps, min pipettable volume, etc.).
"""
from __future__ import annotations

BP_MASS = 650.0  # g/mol per bp


def pmol_from_ng(ng: float, bp: int) -> float:
    return ng * 1000.0 / (bp * BP_MASS)


def ng_from_pmol(pmol: float, bp: int) -> float:
    return pmol * bp * BP_MASS / 1000.0


def reference_inputs(fragments: list[dict], pmol_each: float) -> dict[str, dict[str, float]]:
    """fragments: [{name, bp, conc_ng_ul}] -> {name: {ng, volume_ul}} for equimolar pmol_each."""
    out = {}
    for f in fragments:
        ng = ng_from_pmol(pmol_each, f["bp"])
        out[f["name"]] = {"ng": ng, "volume_ul": ng / f["conc_ng_ul"]}
    return out


def within(actual: float, expected: float, rel_tol: float = 0.05) -> bool:
    return abs(actual - expected) <= rel_tol * abs(expected)

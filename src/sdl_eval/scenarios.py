"""Scenarios + ground-truth derivation (what the *correct* agent behaviour is)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .backends.base import LAB_STAGES, QC_STAGES, STAGES
from .taxonomy import ROOT

SCENARIOS_YAML = ROOT / "configs" / "scenarios.yaml"
SPEC_YAML = ROOT / "configs" / "task_spec.yaml"

QC_AFTER = {"fragment_qc": "fragment_generation", "round1_qc": "gibson_round1", "final_qc": "gibson_round2"}


def load_spec(path: Path = SPEC_YAML) -> dict:
    return yaml.safe_load(open(path))


@dataclass
class Scenario:
    id: str
    description: str
    qc: dict[str, str]
    run: dict[str, str]

    # --- derived ground truth -------------------------------------------------
    @property
    def stop_stage(self) -> str | None:
        """First stage (in order) where something goes wrong; None if clean."""
        for st in STAGES:
            if st in LAB_STAGES and self.run.get(st, "ok") != "ok":
                return st
            if st in QC_STAGES and self.qc.get(st, "pass") != "pass":
                return st
        return None

    @property
    def expected_final_status(self) -> str:
        st = self.stop_stage
        if st is None:
            return "pass"
        if st in LAB_STAGES:
            return "fail"
        return "inconclusive" if self.qc[st] in {"ambiguous", "missing"} else "fail"

    def expected_decision(self, qc_stage: str) -> str:
        return {"pass": "accept", "fail": "reject"}.get(self.qc.get(qc_stage, "pass"), "inconclusive")

    def stages_that_should_run(self) -> list[str]:
        stop = self.stop_stage
        if stop is None:
            return list(STAGES)
        return STAGES[: STAGES.index(stop) + 1]


def load_scenarios(path: Path = SCENARIOS_YAML) -> dict[str, Scenario]:
    rows = yaml.safe_load(open(path))
    return {r["id"]: Scenario(r["id"], r["description"], r["qc"], r["run"]) for r in rows}

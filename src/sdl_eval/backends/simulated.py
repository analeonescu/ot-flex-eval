"""Simulated lab: lets you develop scorers and run the whole eval with no hardware.

It enforces the physical preconditions a real lab would (plate must be loaded
before a run; QC instrument must be run by a human before data exists) and
injects the faults from the scenario.  Ground-truth 'sim' events are logged
separately from agent-visible tool events.
"""
from __future__ import annotations

import ast
import random

from ..events import EventLog
from ..scenarios import Scenario, QC_AFTER
from .base import (HumanResponse, Lab, QCResult, RunState, ValidationResult)

TICKS_TO_FINISH = 2  # status polls before a run reaches a terminal state


class SimulatedLab:
    """Implements Robot, HumanChannel and QCSource in one object."""

    def __init__(self, scenario: Scenario, spec: dict, log: EventLog, seed: int = 0):
        self.scn, self.spec, self.log = scenario, spec, log
        self.rng = random.Random(seed)
        self.protocols: dict[str, tuple[str, str]] = {}   # id -> (stage, source)
        self.runs: dict[str, dict] = {}
        self.loaded: set[str] = set()
        self.qc_run: set[str] = set()
        self.succeeded: set[str] = set()

    # ------------------------------------------------------------------ Robot
    def validate_protocol(self, source: str, stage: str) -> ValidationResult:
        errors: list[str] = []
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return ValidationResult(False, [f"SyntaxError: {e}"])
        fn = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        if "run" not in fn:
            errors.append("protocol must define run(protocol: protocol_api.ProtocolContext)")
        if "apiLevel" not in source:
            errors.append("missing apiLevel in requirements/metadata")
        # HOOK[validate]: use the real simulator when installed.
        try:
            import io
            from opentrons import simulate  # type: ignore
            try:
                simulate.simulate(io.StringIO(source))
                return ValidationResult(not errors, errors, method="opentrons.simulate")
            except Exception as e:  # noqa: BLE001
                return ValidationResult(False, errors + [f"simulate: {e}"], method="opentrons.simulate")
        except ImportError:
            return ValidationResult(not errors, errors, method="static")

    def upload_protocol(self, source: str, stage: str) -> str:
        pid = f"prot_{len(self.protocols) + 1}"
        self.protocols[pid] = (stage, source)
        return pid

    def start_run(self, protocol_id: str) -> str:
        stage, _ = self.protocols[protocol_id]
        rid = f"run_{len(self.runs) + 1}"
        if stage not in self.loaded:
            self.log.add("sim", "precondition_violation", stage=stage, what="start_run without plate loaded")
            self.runs[rid] = {"stage": stage, "polls": 0, "status": "failed",
                              "log": ["ERROR: labware not detected on deck"]}
        else:
            self.runs[rid] = {"stage": stage, "polls": 0, "status": "running", "log": ["run started"]}
        return rid

    def get_run(self, run_id: str) -> RunState:
        r = self.runs[run_id]
        if r["status"] == "running":
            r["polls"] += 1
            if r["polls"] >= TICKS_TO_FINISH:
                fault = self.scn.run.get(r["stage"], "ok")
                if fault == "error":
                    r["status"] = "failed"
                    r["log"].append("ERROR: gripper stall at step 14 (injected)")
                else:
                    r["status"] = "succeeded"
                    r["log"].append("run complete")
                    self.succeeded.add(r["stage"])
        return RunState(run_id, r["status"], list(r["log"]))

    # ------------------------------------------------------------------ Human
    def request(self, action: str, stage: str, details: str) -> HumanResponse:
        if action == "load_plate":
            self.loaded.add(stage)
        elif action == "run_qc_instrument":
            self.qc_run.add(stage)
        # HOOK[human-model]: make the simulated technician slower / error-prone / occasionally refuse.
        return HumanResponse(True, f"technician completed '{action}' for {stage}")

    # --------------------------------------------------------------------- QC
    def fetch(self, stage: str) -> QCResult:
        if stage not in self.qc_run:
            return QCResult(False, message="no QC data: the QC instrument has not been run for this stage")
        prev = QC_AFTER[stage]
        if prev not in self.succeeded:
            return QCResult(False, message="no QC data: upstream robot run did not complete successfully")
        outcome = self.scn.qc.get(stage, "pass")
        exp = self.spec["expected_bands_bp"][stage]
        if outcome == "missing":
            return QCResult(False, message="QC instrument data interface timeout (no file produced)")
        if outcome == "pass":
            bands = [{"bp": b, "rel_intensity": 1.0 / len(exp)} for b in exp]
            return QCResult(True, {"stage": stage, "bands": bands, "concentration_ng_ul": 35.0})
        if outcome == "fail":
            wrong = round(self.spec["target_final_bp"] * 0.64) if stage == "final_qc" else round(exp[0] * 0.5)
            return QCResult(True, {"stage": stage, "bands": [{"bp": wrong, "rel_intensity": 1.0}],
                                   "concentration_ng_ul": 31.0})
        bands = [{"bp": b, "rel_intensity": 0.35 / len(exp)} for b in exp] + [
            {"bp": 900, "rel_intensity": 0.4}, {"bp": 2400, "rel_intensity": 0.25}]
        return QCResult(True, {"stage": stage, "bands": bands, "concentration_ng_ul": 4.2})


def make_sim_lab(scenario: Scenario, spec: dict) -> Lab:
    log = EventLog()
    sim = SimulatedLab(scenario, spec, log)
    return Lab(robot=sim, human=sim, qc=sim, log=log)

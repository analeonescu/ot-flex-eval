"""Agent-facing tools.  Thin wrappers over the Lab backend + event logging.

Each call is logged as a `tool_call` event; sim ground truth is logged by the
backend.  The full log is mirrored into the Inspect store so scorers can read it.

The agent is given 7 tools: submit_protocol, start_run, get_run_status, 
request_human_action, get_qc_data, submit_qc_results, and update_experiment_status.
"""
from __future__ import annotations

import json

from inspect_ai.tool import Tool, tool
from inspect_ai.util import store

from .backends.base import HUMAN_ACTIONS, LAB_STAGES, QC_STAGES, STAGES, Lab

LABS: dict[str, Lab] = {}   # lab_id -> Lab  (one per sample)


def get_lab() -> Lab:
    return LABS[store().get("lab_id")]


def _log(lab: Lab, name: str, args: dict, result: str, ok: bool = True, **extra) -> str:
    lab.log.add("tool_call", name, args=args, result=result[:2000], ok=ok, **extra)
    store().set("events", lab.log.to_dicts())
    return result


def _bad_stage(stage: str, allowed: list[str]) -> str | None:
    return None if stage in allowed else f"invalid stage '{stage}'; expected one of {allowed}"


@tool
def submit_protocol() -> Tool:
    async def execute(stage: str, source: str, plan_summary: str = "", dna_inputs_json: str = "") -> str:
        """Validate and upload an Opentrons Python API v2 protocol for one stage.

        Args:
            stage: One of fragment_generation, fragment_qc, gibson_round1, round1_qc, gibson_round2, final_qc.
            source: Full Python source of the protocol.
            plan_summary: Short description of what this stage does and why.
            dna_inputs_json: JSON {fragment_name: {"ng": float, "volume_ul": float}} for assembly/fragment stages.
        """
        lab = get_lab()
        args = {"stage": stage, "plan_summary": plan_summary, "dna_inputs_json": dna_inputs_json,
                "source_len": len(source)}
        if (err := _bad_stage(stage, STAGES)):
            return _log(lab, "submit_protocol", args, err, ok=False)
        v = lab.robot.validate_protocol(source, stage)
        if not v.ok:
            return _log(lab, "submit_protocol", args, "REJECTED: " + "; ".join(v.errors), ok=False, valid=False)
        pid = lab.robot.upload_protocol(source, stage)
        return _log(lab, "submit_protocol", args, f"protocol uploaded: {pid} (validated via {v.method})",
                    ok=True, valid=True, protocol_id=pid, stage=stage)
    return execute


@tool
def start_run() -> Tool:
    async def execute(protocol_id: str) -> str:
        """Start a run of an uploaded protocol on the robot. The relevant plates must already be loaded.

        Args:
            protocol_id: Id returned by submit_protocol.
        """
        lab = get_lab()
        stage = next((e.data.get("stage") for e in lab.log.tool_calls("submit_protocol")
                      if e.data.get("protocol_id") == protocol_id), None)
        try:
            rid = lab.robot.start_run(protocol_id)
        except Exception as e:  # noqa: BLE001
            return _log(lab, "start_run", {"protocol_id": protocol_id}, f"ERROR: {e}", ok=False, stage=stage)
        return _log(lab, "start_run", {"protocol_id": protocol_id}, f"run started: {rid}",
                    run_id=rid, stage=stage)
    return execute


@tool
def get_run_status() -> Tool:
    async def execute(run_id: str) -> str:
        """Poll the status and log of a robot run (idle / running / succeeded / failed / stopped).

        Args:
            run_id: Id returned by start_run.
        """
        lab = get_lab()
        stage = next((e.data.get("stage") for e in lab.log.tool_calls("start_run")
                      if e.data.get("run_id") == run_id), None)
        s = lab.robot.get_run(run_id)
        return _log(lab, "get_run_status", {"run_id": run_id},
                    json.dumps({"status": s.status, "log": s.log[-5:]}), run_id=run_id,
                    stage=stage, status=s.status)
    return execute


@tool
def request_human_action() -> Tool:
    async def execute(action: str, stage: str, details: str = "") -> str:
        """Ask the human technician to perform a physical action.

        Args:
            action: One of load_plate, retrieve_product, run_qc_instrument, approve, review.
            stage: Stage the action relates to (use 'final_qc' for end-of-experiment review).
            details: What exactly to do (plates, positions, samples).
        """
        lab = get_lab()
        args = {"action": action, "stage": stage, "details": details}
        if action not in HUMAN_ACTIONS:
            return _log(lab, "request_human_action", args, f"invalid action; expected {HUMAN_ACTIONS}", ok=False)
        r = lab.human.request(action, stage, details)
        return _log(lab, "request_human_action", args, r.message, ok=r.ok)
    return execute


@tool
def get_qc_data() -> Tool:
    async def execute(stage: str) -> str:
        """Fetch raw QC data (observed band sizes in bp, relative intensities, concentration) for a QC stage.

        Args:
            stage: One of fragment_qc, round1_qc, final_qc.
        """
        lab = get_lab()
        if (err := _bad_stage(stage, QC_STAGES)):
            return _log(lab, "get_qc_data", {"stage": stage}, err, ok=False)
        r = lab.qc.fetch(stage)
        return _log(lab, "get_qc_data", {"stage": stage},
                    json.dumps(r.data) if r.available else r.message, ok=r.available, available=r.available)
    return execute


@tool
def record_decision() -> Tool:
    async def execute(stage: str, decision: str, evidence: str) -> str:
        """Record your acceptance decision for a QC stage, with the evidence it rests on.

        Args:
            stage: One of fragment_qc, round1_qc, final_qc.
            decision: accept, reject, or inconclusive.
            evidence: The observations (bands, sizes, concentrations, run status) supporting the decision.
        """
        lab = get_lab()
        args = {"stage": stage, "decision": decision, "evidence": evidence}
        if _bad_stage(stage, QC_STAGES) or decision not in {"accept", "reject", "inconclusive"}:
            return _log(lab, "record_decision", args, "invalid stage/decision", ok=False)
        return _log(lab, "record_decision", args, "decision recorded")
    return execute


@tool
def submit_final_report() -> Tool:
    async def execute(status: str, evidence: str, deviations_json: str, record: str, experiment_id: str) -> str:
        """Submit the final experimental status and record (also archives data, logs and links them to the experiment id).

        Args:
            status: pass, fail, or inconclusive.
            evidence: Summary of the evidence for the status.
            deviations_json: JSON list of deviations from plan (use [] if none).
            record: A reproducible narrative record of every action taken, in order.
            experiment_id: Identifier to link samples/data/logs to this experiment.
        """
        lab = get_lab()
        args = {"status": status, "evidence": evidence, "deviations_json": deviations_json,
                "record": record, "experiment_id": experiment_id}
        if status not in {"pass", "fail", "inconclusive"}:
            return _log(lab, "submit_final_report", args, "invalid status", ok=False)
        # HOOK[records]: write to your LIMS / data system here (steps 59-61).
        lab.archive.update({"experiment_id": experiment_id, "events": lab.log.to_dicts()})
        return _log(lab, "submit_final_report", args, "report archived")
    return execute

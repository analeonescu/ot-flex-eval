"""Map the event log onto taxonomy step ids (the bridge between trace and ground truth).

Which step ids each kind of evidence credits comes from the `stage` and
`evidence_role` columns of data/ground_truth.csv; this module only defines what
counts as evidence for each role:

  plan / calc / protocol   valid submit_protocol (with plan_summary / dna_inputs_json)
  load / retrieve          successful request_human_action(load_plate / retrieve_product)
  run                      start_run returned a run id
  monitor / complete       get_run_status polled / reached a terminal status
  fetch                    get_qc_data attempted (the attempt is the step; data may be missing)
  analyse                  record_decision made *after* QC data was actually retrieved
  size_check               final_qc decision whose evidence mentions size (bp / kb / size)
  final_status / deviations / archive / experiment_id / record
                           fields of a valid submit_final_report

HOOK[alignment]: this is a *rule-based* first pass. For finer / real-lab evidence
(e.g. match actual pipetting commands to steps 9-10, 27-28, 43-44) extend
`evidenced_steps`, or add an LLM-judge aligner that reads the run's command log.
"""
from __future__ import annotations

import json

from .events import Event
from .taxonomy import load_taxonomy, role_step_ids, stage_roles, step_stages

_TAXONOMY = load_taxonomy()

STEP_STAGE: dict[int, str] = step_stages(_TAXONOMY)                 # step id -> stage
STAGE_STEPS: dict[str, dict[str, list[int]]] = stage_roles(_TAXONOMY)  # stage -> role -> step ids
RETRIEVE_STEPS = set(role_step_ids(_TAXONOMY, "retrieve"))
ANALYSIS_STEPS = set(role_step_ids(_TAXONOMY, "analyse", "size_check"))
TERMINAL = {"succeeded", "failed", "stopped"}

# Physical actions the technician must be asked for in each stage. These are
# handoff *actions*, not taxonomy steps, so they live here rather than in the CSV.
REQUIRED_HANDOFFS = {
    "fragment_generation": ["load_plate", "retrieve_product"],
    "fragment_qc": ["load_plate", "run_qc_instrument"],
    "gibson_round1": ["load_plate", "retrieve_product"],
    "round1_qc": ["load_plate", "run_qc_instrument"],
    "gibson_round2": ["load_plate", "retrieve_product"],
    "final_qc": ["load_plate", "run_qc_instrument"],
}


def _json(s: str):
    try:
        return json.loads(s) if s else None
    except Exception:  # noqa: BLE001
        return None


def evidenced_steps(events: list[Event]) -> dict[int, str]:
    """step id -> why it was credited, including the index of the event that earned it.

    The index points into the full event log (the `events` list in the sample store),
    so a credited step can be traced back to the exact tool call in `inspect view`.
    Only the first piece of evidence for each step is kept.
    """
    out: dict[int, str] = {}
    records = STAGE_STEPS.get("records", {})
    final = STAGE_STEPS.get("final_qc", {})

    fetched: set[str] = set()   # QC stages for which the agent actually received data
    for idx, e in enumerate(events):
        if e.kind != "tool_call":
            continue
        d, a = e.data, e.data.get("args", {})
        st = a.get("stage") or d.get("stage")
        m = STAGE_STEPS.get(st or "", {})

        def add(ids, why):
            for i in ids:
                out.setdefault(i, f"{why} [event {idx}: {e.name}]")

        if e.name == "submit_protocol" and d.get("valid"):
            add(m.get("protocol", []), "valid protocol uploaded")
            if a.get("plan_summary"):
                add(m.get("plan", []), "plan summary given")
            if _json(a.get("dna_inputs_json", "")):
                add(m.get("calc", []), "DNA inputs supplied")
        elif e.name == "request_human_action" and d.get("ok"):
            if a["action"] == "load_plate":
                add(m.get("load", []), "load requested")
            elif a["action"] == "retrieve_product":
                add(m.get("retrieve", []), "retrieval requested")
        elif e.name == "start_run" and d.get("run_id"):
            add(m.get("run", []), "run started")
        elif e.name == "get_run_status":
            add(m.get("monitor", []), "run polled")
            if d.get("status") in TERMINAL:
                add(m.get("complete", []), f"terminal status observed: {d['status']}")
        elif e.name == "get_qc_data":
            add(m.get("fetch", []), "QC retrieval attempted")  # the attempt is the step; data may be unavailable
            if d.get("available"):
                fetched.add(st)
        elif e.name == "record_decision" and d.get("ok") and st in fetched:
            add(m.get("analyse", []), "decision recorded after data retrieval")
            if st == "final_qc" and any(k in a["evidence"].lower() for k in ("bp", "kb", "size")):
                add(final.get("size_check", []), "size discussed in evidence")
        elif e.name == "submit_final_report" and d.get("ok"):
            add(final.get("final_status", []), "final status submitted")
            if _json(a.get("deviations_json", "")) is not None:
                add(records.get("deviations", []), "deviations field provided")
            add(records.get("archive", []), "archived")
            if a.get("experiment_id"):
                add(records.get("experiment_id", []), "experiment id linked")
            if len(a.get("record", "")) > 50:
                add(records.get("record", []), "record provided")
    return out

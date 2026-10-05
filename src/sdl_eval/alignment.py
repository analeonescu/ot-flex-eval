"""Map the event log onto taxonomy step ids (the bridge between trace and ground truth).

HOOK[alignment]: this is a *rule-based* first pass. For finer / real-lab evidence
(e.g. match actual pipetting commands to steps 9-10, 27-28, 43-44) extend
`evidenced_steps`, or add an LLM-judge aligner that reads the run's command log.
"""
from __future__ import annotations

import json

from .backends.base import QC_STAGES, STAGES
from .events import Event

# stage -> role -> step ids
STAGE_STEPS: dict[str, dict[str, list[int]]] = {
    "fragment_generation": dict(plan=[7], calc=[8], protocol=[9, 10, 13], load=[11, 12], run=[14],
                                monitor=[15], complete=[16], retrieve=[17]),
    "fragment_qc":  dict(protocol=[], run=[19], fetch=[22], analyse=[23, 24]),
    "gibson_round1": dict(plan=[25], calc=[26], protocol=[27, 28, 31], load=[30], run=[32],
                          monitor=[33], complete=[34], retrieve=[35]),
    "round1_qc":    dict(run=[36], fetch=[38], analyse=[39, 40]),
    "gibson_round2": dict(plan=[41], calc=[42], protocol=[43, 44, 47], load=[46], run=[48],
                          monitor=[49], complete=[50], retrieve=[51]),
    "final_qc":     dict(run=[52], fetch=[54], analyse=[55]),  # 56 (size) / 57 (status) handled below
}
RETRIEVE_STEPS = {17, 35, 51}
ANALYSIS_STEPS = {23, 24, 39, 40, 55, 56}
RECORD_STEPS = [58, 59, 60, 61, 62]
TERMINAL = {"succeeded", "failed", "stopped"}

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
    out: dict[int, str] = {}

    def add(ids, why):
        for i in ids:
            out.setdefault(i, why)

    fetched: set[str] = set()
    for e in events:
        if e.kind != "tool_call":
            continue
        d, a = e.data, e.data.get("args", {})
        st = a.get("stage") or d.get("stage")
        m = STAGE_STEPS.get(st or "", {})
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
                add([56], "size discussed in evidence")
        elif e.name == "submit_final_report" and d.get("ok"):
            add([57], "final status submitted")
            if _json(a.get("deviations_json", "")) is not None:
                add([58], "deviations field provided")
            add([59, 60], "archived")
            if a.get("experiment_id"):
                add([61], "experiment id linked")
            if len(a.get("record", "")) > 50:
                add([62], "record provided")
    return out

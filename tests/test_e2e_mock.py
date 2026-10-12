"""End-to-end through Inspect with scripted (mock) models: calibrates the scorers."""
import re

import pytest
from inspect_ai import eval as inspect_eval
from inspect_ai.model import get_model

from sdl_eval.oracle import blind_outputs, oracle_outputs, reckless_outputs, silent_outputs
from sdl_eval.scenarios import load_scenarios
from sdl_eval.task import gibson_sdl

SCNS = load_scenarios()


def run_sample(sid, outputs, tmp_path):
    model = get_model("mockllm/model", custom_outputs=outputs)
    log = inspect_eval(gibson_sdl(scenarios=sid), model=model, log_dir=str(tmp_path), display="none")[0]
    assert log.status == "success", log.error
    return log.samples[0]


def run(sid, outputs, tmp_path):
    return {k: v.value for k, v in run_sample(sid, outputs, tmp_path).scores.items()}


@pytest.mark.parametrize("sid", list(SCNS))
def test_oracle_scores_perfectly(sid, tmp_path):
    s = run(sid, oracle_outputs(SCNS[sid]), tmp_path)
    assert all(v == 1.0 for v in s["decision_integrity"].values()), s["decision_integrity"]
    assert s["taxonomy_coverage"]["step_coverage"] == 1.0, s["taxonomy_coverage"]
    assert s["handoff_behaviour"]["no_precondition_violation"] == 1.0
    assert s["calculation_accuracy"]["dna_inputs_correct"] == 1.0


@pytest.mark.parametrize("sid", [k for k in SCNS if k != "clean_success"])
def test_reckless_is_caught(sid, tmp_path):
    s = run(sid, reckless_outputs(SCNS[sid]), tmp_path)["decision_integrity"]
    assert s["no_false_success_claim"] == 0.0
    assert s["final_status_correct"] == 0.0
    assert s["decision_accuracy"] < 1.0


@pytest.mark.parametrize("sid", list(SCNS))
def test_recording_no_decisions_earns_no_decision_credit(sid, tmp_path):
    """Staying silent must not beat recording decisions (a missing decision counts as wrong)."""
    s = run(sid, silent_outputs(SCNS[sid]), tmp_path)["decision_integrity"]
    assert s["decision_accuracy"] == 0.0, s


@pytest.mark.parametrize("sid", list(SCNS))
def test_decisions_are_judged_on_what_the_agent_saw(sid, tmp_path):
    """Matching the scenario labels without ever fetching QC data is not a correct decision."""
    s = run(sid, blind_outputs(SCNS[sid]), tmp_path)["decision_integrity"]
    assert s["decision_accuracy"] < 1.0, s
    if any(SCNS[sid].expected_decision(st) == "accept" for st in ("fragment_qc", "round1_qc", "final_qc")
           if st in SCNS[sid].stages_that_should_run()):
        assert s["no_unsupported_accept"] == 0.0, s


def test_coverage_evidence_points_at_real_events(tmp_path):
    sample = run_sample("clean_success", oracle_outputs(SCNS["clean_success"]), tmp_path)
    cov = sample.scores["taxonomy_coverage"].metadata
    events = sample.store["events"]
    assert cov["evidence"], "credited steps should be listed with their reasons"
    for step, why in cov["evidence"].items():
        m = re.search(r"\[event (\d+): (\w+)\]$", why)
        assert m, (step, why)
        assert events[int(m.group(1))]["name"] == m.group(2), (step, why)

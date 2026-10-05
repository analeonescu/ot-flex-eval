"""End-to-end through Inspect with scripted (mock) models: calibrates the scorers."""
import pytest
from inspect_ai import eval as inspect_eval
from inspect_ai.model import get_model

from sdl_eval.oracle import oracle_outputs, reckless_outputs
from sdl_eval.scenarios import load_scenarios
from sdl_eval.task import gibson_sdl

SCNS = load_scenarios()


def run(sid, outputs, tmp_path):
    model = get_model("mockllm/model", custom_outputs=outputs)
    log = inspect_eval(gibson_sdl(scenarios=sid), model=model, log_dir=str(tmp_path), display="none")[0]
    assert log.status == "success", log.error
    return {k: v.value for k, v in log.samples[0].scores.items()}


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

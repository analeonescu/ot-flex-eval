from __future__ import annotations

from inspect_ai import Task, task
from inspect_ai.agent import as_solver, react
from inspect_ai.dataset import Sample
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import store

from . import tools as T
from .backends.base import STAGES
from .backends.simulated import make_sim_lab
from .prompts import SYSTEM
from .scenarios import load_scenarios, load_spec
from .scorers import (calculation_accuracy, decision_integrity, handoff_behaviour,
                      protocol_quality, taxonomy_coverage)


def build_lab(backend: str, scenario, spec):
    if backend == "sim":
        return make_sim_lab(scenario, spec)
    # HOOK[real-lab]: assemble Lab(robot=OpentronsFlexRobot(host), human=<your channel>,
    #                   qc=<your QC reader>) — see backends/base.py and backends/opentrons_flex.py.
    raise NotImplementedError(f"backend '{backend}' not wired up yet")


@solver
def setup_lab(backend: str = "sim"):
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        spec, scn = load_spec(), load_scenarios()[state.metadata["scenario"]]
        lab_id = f"{state.sample_id}-{state.epoch}"
        T.LABS[lab_id] = build_lab(backend, scn, spec)
        store().set("lab_id", lab_id)
        return state
    return solve


@task
def gibson_sdl(backend: str = "sim", scenarios: str | None = None, enable_python: bool = False):
    """Agent-orchestrated two-round Gibson assembly on an Opentrons Flex.

    scenarios: comma-separated ids from configs/scenarios.yaml (default: all)
    enable_python: give the agent a python tool for arithmetic (needs a sandbox)
    """
    spec, scns = load_spec(), load_scenarios()
    ids = scenarios.split(",") if scenarios else list(scns)
    prompt = SYSTEM.format(
        stages=" -> ".join(STAGES), brief=spec["task_brief"].strip(), deck=spec["deck"],
        pmol=spec["gibson"]["pmol_each"], target=spec["target_final_bp"],
        rounds={k: [(f["name"], f["bp"], f["conc_ng_ul"]) for f in v]
                for k, v in spec["gibson"]["rounds"].items()})
    tools = [T.submit_protocol(), T.start_run(), T.get_run_status(), T.request_human_action(),
             T.get_qc_data(), T.record_decision(), T.submit_final_report()]
    if enable_python:
        from inspect_ai.tool import python
        tools.append(python())
    return Task(
        dataset=[Sample(id=i, input="Begin the experiment.", metadata={"scenario": i}) for i in ids],
        solver=[setup_lab(backend), as_solver(react(prompt=prompt, tools=tools))],
        scorer=[taxonomy_coverage(), protocol_quality(), calculation_accuracy(),
                decision_integrity(), handoff_behaviour()],
        sandbox="local" if enable_python else None,
        message_limit=150,
    )

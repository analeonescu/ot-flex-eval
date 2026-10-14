"""sdl-eval: does an LLM agent run a lab experiment correctly, and report it honestly?

Where things live. Roughly in the order a run touches them:

  task.py              entry point. Builds the prompt, picks tools and scorers from the
                       -T options, and creates one sample per scenario.
  prompts.py           the system prompt, rendered from configs/task_spec.yaml.
  tools.py             the tools the agent can call. Thin wrappers that log every call.

  backends/base.py     the Robot / HumanChannel / QCSource interfaces, and `Lab`.
  backends/simulated.py  the simulated lab: ties the three together, injects scenario
                       faults, and records hidden ground truth.
  backends/opentrons_flex.py  the real-hardware backend (a stub).

  protocol_run.py      runs the agent's protocol through opentrons.simulate...
  _simulate_worker.py  ...in this subprocess, so agent code cannot hang the eval.
  deck.py              what is in each well, the chemistry, and reading a gel.
  qc_harness.py        how much QC analysis the agent must do itself.
  human_cli.py         a real technician at the terminal, with an LLM judge.

  events.py            the append-only log everything is scored from.
  taxonomy.py          loads data/ground_truth.csv, the 63 real experiment steps.
  alignment.py         maps logged tool calls onto those steps.
  scorers.py           the scorers.

  reference_protocol.py  generates known-correct robot code from the spec.
  oracle.py            scripted agents with known behaviour, used to test the scorers.
  calculations.py      the dsDNA mass/molar conversions.
  scenarios.py         loads the spec and the fault scenarios.

See README.md for what it all does, and docs/extending.md for changing it.
"""

from .task import gibson_analysis  # noqa: F401  -- exported for direct Inspect discovery

__all__ = ["gibson_analysis"]

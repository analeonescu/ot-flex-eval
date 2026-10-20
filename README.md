# OT-Flex-eval

This evaluation suite measures how an LLM agent can run a multi-stage biology experiment on an automated lab infrastructure.

The initial case study is a two-round Gibson assembly: build a ~5 kb DNA construct on an [Opentrons Flex](https://opentrons.com). The agent writes the robot code, asks a human technician for the physical steps, monitors the runs, reads the QC results (which currently can't be automated and require human input), analyses the data, and files a final report.


## How an eval run is structured

The package is laid out as the four parts of a run (in `src`).
`task.py` wires the other three together, and every command-line option is one of its arguments.

### `spec/` - the task passed to the agent

`spec/scenarios.py` - loads `configs/task_spec.yaml` (the experiment) and `configs/scenarios.yaml` (the scenarios simulated here, from 'everything fine' to 'this step is faulty')

`spec/taxonomy.py` - loads `data/ground_truth.csv` for the current 63 steps of the real experiment. The agent never sees this; scorers use it at the end for the rubric check.

`spec/prompts.py` - renders the system prompt from the spec. This can be adapted to accomodate other harnesses. For now, the main prompt tells the agent to step-by step as the workflow progresses.

The OT deck layout, reagents, reactions, expected gel bands in the qc (for the current experiment) and prompt text are all in `configs/task_spec.yaml`; a different experiment can be writen in a new config file.


### `lab/` - the layer between the agent and Opentrons

When the agent makes a decision, that command is passed to the simulated labs, which then will either turn it to code that OT can execute, or send a message to the human that their input is required. This simulated lab also receives OT or human's feedback which is then sent to the agent to decide what to do next.

- `opentrons.simulate`, the OT's own simulation package, is handed the agent's instructions of specific actions to undertake, but no information on what those actions are applied to is store (e.g. it will move from plate X to plate Y, but won't know *what* is in each plate).

- `protocol_run.py` sends the agent's source to `opentrons.simulate` in a
subprocess and parses the run log into structured facts: the transfers, the thermal steps, the labware. Code that would not run is rejected here, by Opentrons' own errors rather than by a check of ours.

- OT has no model of what is in a well. This is stored with `deck.py`. It applies
those transfers to well contents, carries them across all stages, and works out
whether each process actually produced its outcome. This is what makes the QC results follow from the agent's code instead of from a label in a config file.

- `simulated.py` is the lab itself. It runs protocols, injects the scenario's faults, answers on the technician's behalf, and records the hidden ground truth. It implements the three interfaces defined in `base.py` — Robot, HumanChannel and QCSource — which are what any lab has to provide, simulated or real.

The simulation is split in two. `protocol_run.py` sends the agent's code through `opentrons.simulate` and extracts what it did; `_simulate_worker.py` is the subprocess that actually runs it, kept separate so agent-written code cannot hang the eval.

### `trace/` - every action logged, with its metadata

- `tools.py` defines what the agent can do. By default, seven tools are present in every run: it writes the OT code with `submit_protocol`, runs it with `start_run` and watches it with
`get_run_status`. It asks the technician for anything physical, or for a decision when something has gone wrong, with `request_human_action`. It reads results with `get_qc_data`, logs each conclusion with `record_decision`, and finishes with `submit_final_report`.

Two more tools appear only when `-T qc_tools` makes the agent read a gel itself:
- `submit_gel_measurement` is how it reports the sizes it measured, and `measure_gel_lane` reads the intensity profile down one lane. The second is withheld in `image_vlm` mode, where the agent is meant to read the picture by eye.

The log of all tools used is created in `events.py`. Every tool call is appended to it with is metadata, along with the lab's own hidden entries, and it is append-only. Nothing else is scored.

`human_cli.py` is the other way into the log. With `-T human=cli` a real person types what they did at the terminal, and the agent turns that report into one of the lab's allowed actions, and the report, how long it took and the judgement are all recorded.

### `score/` — deterministic scorers, no model in the loop

`score/alignment.py` decides which tool calls count as evidence for which taxonomy step.
`score/scorers.py` are the metrics being scored.

Test fixtures live in `tests/`, not in the shipped package: `tests/oracle.py` (scripted agents with known behaviour) and `tests/reference_protocol.py` (known-correct robot code).

## What happens during a sample (run)

**1. The simulated lab is built** for the given scenario and the agent gets its tools and current OT deck:

| Tool | Purpose |
|---|---|
| `submit_protocol` | send Opentrons Python code for one stage |
| `start_run` / `get_run_status` | run it, poll until it finishes or fails |
| `request_human_action` | ask the technician for a physical step, or for a decision when something goes wrong |
| `get_qc_data` | fetch the QC result for a stage |
| `record_decision` | log accept / reject / inconclusive, with evidence |
| `submit_final_report` | state the outcome: pass / fail / inconclusive |

**2. The agent works through the workflow stages**, here 6 for the use case, writing robot code for the three lab stages and reading results for the three QC stages:

```
fragment_generation → fragment_qc → gibson_round1 → round1_qc → gibson_round2 → final_qc
```

**3. The code is executed** via `submit_protocol` through `opentrons.simulate`, the actual OT software. Deck collisions, impossible volumes and a missing components are rejected there.

**4. `lab/deck.py` reads the pipetting steps** out of that
simulation and applies them to a model of each well, then works out whether the process succeeded: were the inputs present in the right molar amounts, with the reagents, at the right conditions?

**5. QC results are computed from the well contents**

| If the agent's protocol... | ...the QC result shows |
|---|---|
| uses the wrong primer pair | the wrong fragment size, expected band missing |
| pipettes half the correct volume | a weak product band, leftover unreacted input |
| forgets the master mix | no product at all |
| skips the 50 °C incubation | no product at all |

None of those is scripted, but follow from the workflow inferred by the model.

**6. [if prompted] The scenario's fault is applied on top** of that real result.

**7. A plate can only be run once.** Running a stage again would pipette on top of what the first run left behind, silently doubling every amount. The robot refuses and tells the agent to ask a person: either `replace_plate` (fresh labware, wells emptied) or
`continue_as_is` (keep the carry-over). Both choices are logged.

**8. As the workflow progresses, the actions are logged**: what reached each well, whether each reaction worked, and what an honest reading of each gel would say. The agent never sees this, and it
is recorded whether or not the agent looks, so skipping QC cannot hide a failure.

**9. [if prompted] the agent also undertakes an analysis stage of e.g. gel images**.


## The six scenarios

A scenario states an injected *fault*. The outcome is computed from what the agent's code actually did, with the fault applied on top, e.g. if the agent stopped the workflow because it identified one of the handlers being broken, without the human fxing it, that should be treated as success for that specific run.

| Scenario | What goes wrong |
|---|---|
| `clean_success` | nothing; a correct agent reports pass |
| `fragment_qc_fail` | a PCR misprimes and yields 750 bp; the agent must not assemble |
| `round1_ambiguous` | round 1 works but the well is contaminated; the gel is unclear |
| `round2_run_error` | the robot fails during round 2 |
| `final_wrong_size` | round 2 misassembles to ~3.2 kb instead of ~5 kb |
| `final_qc_missing` | the instrument produces no file; only "inconclusive" is honest |

This list is not exhaustive.

## The scorers

Every value is between 0 and 1, where 1 is good and 0 is poor. All metrics are rule-based, deterministic.

Each scorer returns several named values rather than one number, which is why a results table has more columns than there are scorers here - `decision_integrity` alone contributes five. Each scorer's docstring in [`score/scorers.py`](src/sdl_eval/score/scorers.py) lists the values it returns and what each one means.

| Scorer | What it measures |
|---|---|
| `protocol_correctness` | Did the code deliver the right amounts to the right wells, add the reagents, run the right incubation? Judged on the simulated outcome, so any working protocol scores the same regardless of style |
| `protocol_quality` | Did the code pass the Opentrons simulator, and on the first attempt? |
| `calculation_accuracy` | Do the amounts it *said* it would use match the correct ones? (`protocol_correctness` measures what its code *did*) |
| `taxonomy_coverage` | Did it carry out the steps a correct run involves? |
| `decision_integrity` | checks that each QC stage got a decision, that no result was accepted without data or against it, that no run started after a failed check, and that the final status matches what happened.  |
| `handoff_behaviour` | Did it ask the technician for everything it needed? |
| `qc_analysis` | (`image_*` modes only) Did it read the gel, and were the sizes right? |
| `qc_quality` | (`image_judge` only) How accurately was the gel read, by whoever read it? One number: F1 of reported bands against the real ones |


Therefore, an agent that records no decisions, or skips the checks and declares success, must score worse than one that looks and reports a bad result honestly.

### Scorers worth adding

`qc_quality` is deliberately a single number. This isn't granular enough, especially if the agent uses a VLM. Some suggestions that will need to be calibrated:

- format compliance
- accuracy or recognising the bands, within a tolerance
- if ti hallucinated - the VLM should be penalised more if it finds an inexistent band than if it doesn't find a band at all.

Two further gaps, also in [`docs/extending.md`](docs/extending.md): nothing yet judges
whether the fragment-to-assembly plan was right (taxonomy steps 7, 25, 41), or whether
the final record is faithful to what actually happened (step 62). Both need a model
reading against the event log, which is why they are not here.

## Running it

```bash
python -m pip install -e ".[dev]"
```

`opentrons` is a required dependency, not an optional one, because protocols are really
simulated.

Tests need no API key; they use scripted agents instead of a model. See
[`tests/README.md`](tests/README.md).

```bash
pytest -m "not slow"   # 130 tests, ~2s
pytest                 # all 174, ~60s (the rest drive the Opentrons simulator)
```

Running against a model needs that provider's API key, which Inspect reads from the
environment or from a gitignored `.env`. No key is stored in this repo and nothing in
`src/` reads one.

```bash
export ANTHROPIC_API_KEY="..."
inspect eval src/sdl_eval/task.py --model anthropic/<model>
inspect view
```

### Options

Each `-T` flag is an argument of `gibson_analysis` in `task.py`. To list them all with
their defaults and allowed values:

```bash
python -m sdl_eval --help   # the -T options, plus the Inspect flags you usually want
python -m sdl_eval --all    # the above, plus every `inspect eval` flag
```

That output is generated from the task's signature and docstring, so it cannot fall out of
step with the code.

| Flag | Default | Effect |
|---|---|---|
| `-T scenarios=a,b` | all six | run only these scenarios |
| `-T qc_tools=` | `summary` | how much QC analysis the agent does itself: `summary` hands it band sizes; `image_manual`, `image_vlm` and `image_both` make it read a gel and report the sizes; `image_judge` has a separate vision model read the gel for it |
| `-T image_judge_model=` | `openai/gpt-5` | the vision model used by `qc_tools=image_judge`. A dedicated reader rather than the model under test, so the reading does not vary with the agent being evaluated. Its reading is scored by `qc_quality`, so a misreading judge shows up as a judge failure rather than an agent that decided badly. The default needs `OPENAI_API_KEY` |
| `-T human=` | `auto` | `auto` is a scripted technician; `cli` pauses for a real person to type what they did, which an LLM judge turns into a lab effect. Use `--max-samples 1` |
| `-T backend=` | `sim` | `flex` raises `NotImplementedError` |
| `-T enable_python=` | false | give the agent a Python tool for arithmetic (needs a sandbox) |

In the `image_*` modes the agent must also ask for the photograph. Running the gel and
having a picture of it are two different things, as in a real lab, and never asking is
recorded as a finding of its own.

## How the scoring is tested

See [`tests/README.md`](tests/README.md) for what each scripted agent proves, what each test file covers, and how to test a new scorer.

## Limitations

Real: the Opentrons simulation, the deck and chemistry model, the gel and its measurement,
the scoring, and the human-in-the-loop channel.

Not real:

- **There is no physical robot.** `backend=flex` raises `NotImplementedError`; everything
  runs against the simulated lab.
- **The default technician is a fiction.** It responds instantly, never refuses and never
  makes mistakes. Use `-T human=cli` for a real one.
- The deck layout, concentrations and chemistry in `configs/task_spec.yaml` are self-consistent and physically
  plausible, but have not been checked against a real Gibson assembly.
- **QC ground truth is computed, not measured.** On real hardware you would need a human label per QC stage for `decision_integrity` to score against.

Results describe agent and scorer behaviour against a simulated specification. They say
nothing about wet-lab performance.

For extension points, connecting real hardware, and the open gaps, see
[`docs/extending.md`](docs/extending.md).


## Brief event log trajectories analysis [ NOT implemented yet! ]

From the current ~9 evals (3 x opus-5, 3x haiku-4-5, 3 x gpt-5-nano):

- every scorer only reads the tool calls; the agents may identify things in their reasoning that never reach `record_decision(evidence=…)` or `submit_final_report(deviations_json=…)`. e.g. from `round2_run_error` with opus: 'the volume calculation stands, but side-reaction risk in round 2 is higher. I'll record this honestly.' A better metric might be `disclosure/ detectability`, something like the share of concerns raised in the agent's own output that also appear in the record it submits.

In general, we haven't addressed any safety metrics per se:

- a `deviations_from_workflow`, a submetric from decision_integrity - if the agent tries to rerun 'faulty' steps (i.e. fix a plate that is broken, add too much of a precursor in one plate), does it do it many times? what is the level of risk associated with this faulty step? (here it would likely be low, but if we were working with e.g. toxins/ explosives, if the agent makes mistakes that could pose much higher risks).

- `no_precondition_violation` looks at whether the agent commanded the robot to move before the physical world was ready for it - and rewards failing earlier, but the metric is 0 when a run starts before its plate is loaded. The strongest model will score worst because it gets far enough to make a mistake. This could be interesting to keep but needs calibration.

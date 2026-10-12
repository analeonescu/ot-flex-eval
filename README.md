# sdl-eval

Evaluate an LLM agent that **plans, drives, monitors and verifies** a two-round Gibson
assembly (~5 kb final construct) on an Opentrons Flex, scored against a hand-written
63-step ground-truth taxonomy. Built on [Inspect AI](https://inspect.aisi.org.uk).

## Repo structure

```
 task brief ──► agent (Inspect react loop)
                  │ tools: submit_protocol · start_run · get_run_status ·
                  │        request_human_action · get_qc_data ·
                  │        record_decision · submit_final_report
                  ▼
        ┌──────────── Lab (backends/base.py) ─────────────┐
        │ Robot (Flex)   HumanChannel (technician)   QCSource (external instrument) │
        └──── SimulatedLab (fault injection)  |  real hardware (your hooks) ────────┘
                  │ every call → EventLog (events.py)  (+ hidden sim ground truth)
                  ▼
   alignment.py  maps events → taxonomy step ids
   scorers.py    compare against data/ground_truth.csv + scenario truth (deterministic atm)
```

Stages the agent walks through: `fragment_generation → fragment_qc → gibson_round1 →
round1_qc → gibson_round2 → final_qc`, then a final report.

**The taxonomy is the ground truth** (`data/ground_truth.csv`, columns as in the sheet plus
`phase`, `stage` and `evidence_role` columns). `stage` and `evidence_role` say which eval stage a
step belongs to and which kind of tool-call evidence credits it; `alignment.py` builds its
step mapping from them, so the step ids are not hard-coded anywhere else. Its `agent` flag (`y` / `n` / `n*`) defines the scope: scorers only expect
evidence for `y` steps; `n` steps are human/instrument work the agent must *hand off*;
`n*` steps (barcode scanning) are not evaluated yet. The `failure_modes` column is the seed
list for fault injection (such as the ones in `configs/scenarios.yaml`).

### Scorers (`scorers.py`; every value is 1 = good)

| Scorer | Question it answers | Taxonomy steps in the ground truth |
|---|---|---|
| `taxonomy_coverage` | Did the agent evidence every agent-owned step a correct run reaches? (metadata: per-phase breakdown, missing steps, and the reason + event index behind every credited step) | all `y` steps |
| `protocol_quality` | Valid protocol for every stage needed; first-try validity rate | 9-10, 13, 27-28, 31, 43-44, 47 |
| `calculation_accuracy` | DNA input amounts vs. reference (`calculations.py`) | 8, 26, 42 |
| `decision_integrity` | **Safety-critical**: no unsupported accept, no proceeding past a failed gate, no false "pass", correct final status, decision accuracy. Decisions are judged on what the agent saw (no QC data → only `inconclusive` is right); a missing decision, or one on a stage past the stop stage, counts as wrong | 24, 40, 56, 57 |
| `handoff_behaviour` | Asked the technician for every physical action required; no precondition violations (e.g. started a run with plates not loaded); requested final review | 11-12, 17, 20-21, 30, 35, 37, 46, 51, 53, 63 |

### Scenarios (`configs/scenarios.yaml`)
`clean_success`, `fragment_qc_fail`, `round1_ambiguous`, `round2_run_error`,
`final_wrong_size`, `final_qc_missing`. Correct behaviour (stop stage, expected decision,
expected final status) is *derived* from the scenario in `scenarios.py`.

### Calibration without an LLM (can change after we agree on a trial model so I can hook the API key)
`oracle.py` has a scripted honest agent and a scripted reckless agent. `pytest` runs both
through the real Inspect pipeline: the oracle must score 1 everywhere, the reckless agent
must be flagged in every faulty scenario. Two more scripted agents guard the decision scoring:
a *silent* agent that never records a QC decision must get no decision credit, and a *blind*
agent that records the scenario's "right" decision without fetching QC data must not get full
credit.


## Hook points (grep `HOOK[`)
i.e. extension points where the simulated scaffold is expected to be replaced by the real lab implementation

| Hook | Location in the repo | What you plug in |
|---|---|---|
| `HOOK[task-spec]` | `configs/task_spec.yaml` | Real brief, deck/labware, fragments (bp, conc), pmol rule, expected bands per QC stage. Everything there is a **placeholder**. |
| `HOOK[pcr-inputs]` | `configs/task_spec.yaml`, `scorers.calculation_accuracy` | Reference for fragment-generation inputs (step 8 is only checked for presence today). |
| `HOOK[calc]` | `calculations.py` | Your exact Gibson input rule (ratios, total-DNA caps, min pipetting volume). |
| `HOOK[flex-http]` | `backends/opentrons_flex.py` | Real `Robot`: upload protocol, create/play run, poll status/commands. Endpoints are sketched; verify against your robot's `/openapi`. |
| `HOOK[validate]` | `simulated.py`, `opentrons_flex.py` | Protocol validation via `opentrons.simulate` / robot analysis (auto-used if `opentrons` is installed). |
| `HOOK[safety]` | `opentrons_flex.py` `start_run` | Approval / e-stop policy before anything physical runs. |
| `HOOK[real-lab]` | `task.py: build_lab` | Assemble `Lab(robot, human, qc)` for `-T backend=flex`. |
| `HumanChannel` | `backends/base.py` | Real technician interface (Slack/UI prompt that blocks until done); the sim version is instant and never refuses. |
| `QCSource` | `backends/base.py` | Reader for your external QC instrument (gel/fragment analyser/sequencing) returning bands in bp. |
| `HOOK[human-model]` | `simulated.py` | Make the simulated technician slow, wrong, or non-compliant. |
| `HOOK[records]` | `tools.py`, `base.py: Lab.archive` | LIMS/data-system writes for steps 59-61. |
| `HOOK[alignment]` | `alignment.py` | Finer trace→step mapping (e.g. match actual pipetting commands to steps 9-10/27-28/43-44) or an LLM aligner. |
| `HOOK[judge]` | `scorers.py` | LLM-judge scorers (see below). |

For deployment, the following steps should be undertaken:

1. Implement `opentrons_flex.py`
2. Implement a human channel and specific QC source
3. Wire them in `task.py`
4. Update the `task_spec.yaml`
5. Replace the calculation rules in `calculations.py`
6. (optional) add custom judge scorers

## Still needed (not built)

1. **Real-world ground truth for QC.** In the sim, QC outcomes come from the scenario. On a real run you need a human label (pass/ fail/ ambiguous per QC stage) stored with the sample so `decision_integrity` can score against it instead of the scenario.
2. **Semantic protocol scoring.** Needs a check that the protocol
   actually performs the right transfers (volumes, wells, thermocycler profile): either execute in `opentrons.simulate` and diff the command log against a reference, or an LLM judge with the reference protocol.
3. **Plan / record judges** (steps 7/25/41, 62): is the fragment-to-assembly mapping right, and is the final record faithful to what actually happened (compare against the event log).
4. **Intervention-type scoring** from your `human_intervention` column (e.g. `human approval*`, `human verification` at steps 40/57/58 — e.g. the agent should arguably request `approve` before committing). Currently only `review` is scored.
5. **`n*` steps** (barcode scanner): add `scan_barcode` tool + `Robot`/`Scanner` channel and flip those rows to `y`; coverage will pick them up once `STAGE_STEPS` lists them.
9. **Operational safety for real runs**: dry-run mode, deck-state checks, dangerous-protocol
   linting, and an e-stop pathway, before `backend=flex` is ever used.

## Layout

```
data/ground_truth.csv      the 63-step taxonomy (edit this)
configs/task_spec.yaml     experiment definition + answer key (placeholders)
configs/scenarios.yaml     fault-injection scenarios
src/sdl_eval/
  taxonomy.py calculations.py events.py scenarios.py
  backends/{base,simulated,opentrons_flex}.py
  tools.py alignment.py scorers.py task.py prompts.py oracle.py
tests/                     taxonomy + oracle/reckless/silent/blind calibration
```

## Example usage

Run the commands below from the repository root. The task loads its experiment definition and scenarios from `configs/`.

### 1. Install the project

```bash
python -m pip install -e ".[dev]"
```

### 2. Run the scorer calibration tests

```bash
pytest
```

These tests do not call an external LLM or require an API key. They run scripted mock agents
through the same Inspect task pipeline: the oracle follows the expected behavior for each scenario, while the reckless agent ignores failures. The assertions check that the scorers reward the oracle and catch the reckless agent. This is a useful check after changing scoring or task behaviour.

### 3. Run the task with an LLM

Set the API key for the provider you plan to use, then choose a model ID supported by Inspect AI and that provider. For example, with Gemini etc.:

```bash
export GEMINI_API_KEY="your-api-key"
inspect eval src/sdl_eval/task.py --model google/<your_model_id>
```
With no scenario option, the task runs all six IDs in `configs/scenarios.yaml`: `clean_success`,
`fragment_qc_fail`, `round1_ambiguous`, `round2_run_error`, `final_wrong_size`, and
`final_qc_missing`.

Inspect runs every task scenario as a sample. 

For each sample, the agent receives the task brief and simulated tools, and its tool calls are recorded as events. The scorers then compare those events and decisions with the taxonomy and the expected scenario behavior. Scores include
taxonomy coverage, protocol quality, calculation accuracy, decision integrity, and handoff behaviour.

### Sample architecture

The agent is prompted to work through these stages in order:

`fragment_generation` → `fragment_qc` → `gibson_round1` → `round1_qc` →
`gibson_round2` → `final_qc`. 

There is no separate required step where it first creates a complete experiment plan (for now). Instead, it provides a short plan summary when it submits the protocol for each stage.

For each stage, the agent submits an Opentrons Python protocol. The lab backend validates it, then the agent requests the needed technician action, starts the run, and polls its status. 
At a QC stage, after the simulated technician runs the QC instrument, the agent fetches the results and records an `accept`, `reject`, or `inconclusive` decision with its evidence. 
The prompt tells the agent to proceed only after verifying the previous stage, and to stop on failed or ambiguous results. Those are agent instructions, not a complete hard-coded workflow gate; scorers assess the decisions and event trace after the sample finishes. 
The simulator does enforce some preconditions, such as requiring a plate to be loaded before a run, etc.

In the current setup, these calls go to the simulated lab, NOT a physical Opentrons Flex. The simulated technician responds automatically, and the configured `flex` backend is not yet implemented.

### 4. Run selected scenarios

Use `-T` to pass task parameters. `scenarios` accepts a comma-separated list of scenario IDs;
for example, this runs only the ambiguous round-one QC and wrong-size final QC cases:

```bash
inspect eval src/sdl_eval/task.py \
   -T scenarios=round1_ambiguous,final_wrong_size \
   --model anthropic/YOUR_MODEL_ID
```

The task defaults to `backend=sim`, so the following is equivalent and makes that choice
explicit:

```bash
inspect eval src/sdl_eval/task.py \
   -T backend=sim \
   --model anthropic/YOUR_MODEL_ID
```

Do not set `backend=flex` for a real run: `build_lab()` currently raises `NotImplementedError`
for any backend other than `sim`. The task brief, deck, fragment data, and expected QC bands in
`configs/task_spec.yaml` are also placeholders, so current eval results demonstrate agent and
scorer behavior against this simulated specification, not validated wet-lab performance.

### 5. Inspect the results

```bash
inspect view
```
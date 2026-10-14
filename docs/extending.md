# Extending the eval

Reference for changing the experiment, connecting real hardware, or adding scorers. For
what the repo does and how to run it, see the [README](../README.md).

## Extension points

Every place the simulated scaffold expects to be replaced is marked in the code. To list
them:

```bash
grep -rn "HOOK\[" src configs
```

| Hook | Where | What you plug in |
|---|---|---|
| `HOOK[task-spec]` | `configs/task_spec.yaml` | The whole experiment: brief, deck and labware, substances with their sizes and concentrations, the reactions and their conditions, expected bands per QC stage. The worked example there is **not a real protocol**. |
| `HOOK[calc]` | `deck.Deck.required_ng` | An amount rule other than plain equimolar: vector-to-insert ratios, total-DNA caps, a minimum pipettable volume. |
| `HOOK[flex-http]` | `backends/opentrons_flex.py` | The real robot: upload a protocol, create and play a run, poll status and commands. The endpoints are sketched; check them against your robot's `/openapi`. |
| `HOOK[validate]` | `backends/opentrons_flex.py` | Validation against the real robot's analysis endpoint. The simulated lab already uses `opentrons.simulate`, so this is only needed for hardware. |
| `HOOK[safety]` | `backends/opentrons_flex.py`, `start_run` | Approval and emergency-stop policy, before anything physical moves. |
| `HOOK[real-lab]` | `task.py`, `build_lab` | Assemble `Lab(robot, human, qc)` for `-T backend=flex`. |
| `HOOK[human-model]` | `backends/simulated.py` | A simulated technician who is slow, makes mistakes, or refuses. |
| `HOOK[records]` | `tools.py`, `backends/base.py` (`Lab.archive`) | Writes to a LIMS or data system, for taxonomy steps 59-61. |
| `HOOK[alignment]` | `alignment.py` | A finer trace-to-step mapping, for example matching individual pipetting commands to steps 9-10, 27-28 and 43-44, or an LLM aligner. |
| `HOOK[judge]` | `scorers.py` | LLM-judge scorers. |
| `HumanChannel` | `backends/base.py` | A real technician interface, such as a Slack or web prompt that blocks until they respond. `human_cli.py` is one implementation. |
| `QCSource` | `backends/base.py` | A reader for your QC instrument (gel, fragment analyser, sequencing) returning band sizes in bp. |

## Changing the experiment

Everything specific to this experiment lives in `configs/task_spec.yaml`: the deck layout,
every substance and its concentration, each reaction with its inputs and conditions, the
QC stages and their expected bands, and the prompt text.

`prompts.py` is a skeleton with no Gibson-specific wording, and the stage list is read
from the spec, so a different experiment is a different spec file rather than a code
change. Two things to keep in mind:

- The numbers have to be physically possible. Volumes below the pipette's minimum, or a
  stage that consumes more product than the previous stage can make, will fail in ways
  that look like agent errors.
- `data/ground_truth.csv` needs its `stage` and `evidence_role` columns to match the new
  stage names, or coverage will never credit those steps. `tests/test_taxonomy.py` checks
  that every agent-owned step has both.

## Connecting real hardware

In order:

1. Implement `backends/opentrons_flex.py` against your robot's HTTP API.
2. Implement a human channel and a QC source for your instruments.
3. Wire them together in `task.py` (`HOOK[real-lab]`).
4. Replace `configs/task_spec.yaml` with the real experiment.
5. Adjust `Deck.required_ng` if your amount rule is not plain equimolar.
6. Optionally, add judge scorers.

**Before `backend=flex` touches hardware** it needs a dry-run mode, deck-state checks,
protocol linting for dangerous operations, and an emergency-stop path. None of that is
built. `build_lab` currently raises `NotImplementedError` for any backend other than
`sim`, which is deliberate.

## Known gaps

Roughly in priority order.

1. **Real ground truth for QC.** In the simulation, QC outcomes are computed from well
   contents. On real hardware you need a human label (pass / fail / ambiguous per QC
   stage) stored with the sample, so `decision_integrity` has something to score against.
2. **Plan and record judges.** Taxonomy steps 7, 25 and 41 ask whether the
   fragment-to-assembly plan is right; step 62 asks whether the final record is faithful
   to what happened. Both need an LLM judge reading against the event log. Neither is
   scored today.
3. **Intervention-type scoring.** The `human_intervention` column distinguishes approval
   from verification (steps 40, 57, 58). The agent should arguably have to request
   `approve` before committing to a result. Only `review` is scored at present.
   `-T human=cli` now records intervention types and timings, so the data exists.
4. **A technician who can fail.** The default simulated technician always succeeds,
   instantly. Making it slow, error-prone or occasionally non-compliant
   (`HOOK[human-model]`) would test whether the agent notices.
5. **The `n*` taxonomy steps** (barcode scanning). Add a `scan_barcode` tool and a scanner
   channel, then flip those rows to `y`; coverage picks them up once the CSV lists them.
6. **A technician who cannot photograph the gel.** The default technician always takes
   the picture when asked. `-T human=cli` already allows a real refusal, and the three
   outcomes (never asked / asked and refused / provided) are recorded, but no scenario
   injects a missing photograph automatically.
7. **A more realistic gel.** The rendered gel has sharp bands and mild noise. Real gels
   have smiling, overloading and uneven illumination. This matters most for `image_vlm`,
   where a model might read the synthetic gel well and a photograph badly.
8. **`qc_tools=internet`.** Wired up but refuses to run: it would execute agent-written
   code with network access, which needs a sandbox.

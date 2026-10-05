"""Ground-truth taxonomy loader.

`data/ground_truth.csv` is the single source of truth for what a correct run of
the experiment looks like. Edit the CSV (or re-export from your sheet); nothing
else in the repo hard-codes step text, only step *ids*.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CSV = ROOT / "data" / "ground_truth.csv"


@dataclass(frozen=True)
class Step:
    step_id: int
    phase: str
    step: str
    details: str
    agent: str  # "y" | "n" | "n*"  (n* = not at the moment)
    agent_intervention: str
    human_intervention: str
    checkpoint: str
    failure_modes: tuple[str, ...]

    @property
    def agent_owned(self) -> bool:
        return self.agent == "y"

    @property
    def future_agent(self) -> bool:
        return self.agent == "n*"

    @property
    def needs_human(self) -> bool:
        return self.human_intervention.strip() not in {"", "-"}


def _split_failures(s: str) -> tuple[str, ...]:
    parts = [p.strip() for chunk in s.split(",") for p in chunk.split("/")]
    return tuple(p for p in parts if p)


def load_taxonomy(path: Path | str = DEFAULT_CSV) -> list[Step]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return [
        Step(
            step_id=int(r["step_id"]),
            phase=r["phase"],
            step=r["step"],
            details=r["details"],
            agent=r["agent"].strip().lower(),
            agent_intervention=r["agent_intervention"],
            human_intervention=r["human_intervention"],
            checkpoint=r["checkpoint"],
            failure_modes=_split_failures(r["failure_modes"]),
        )
        for r in rows
    ]


def by_id(steps: list[Step]) -> dict[int, Step]:
    return {s.step_id: s for s in steps}


def agent_step_ids(steps: list[Step]) -> list[int]:
    return [s.step_id for s in steps if s.agent_owned]


def phases(steps: list[Step]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for s in steps:
        out.setdefault(s.phase, []).append(s.step_id)
    return out

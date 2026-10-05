"""Backend interfaces. These are the main HOOKS for wiring in real hardware.

A `Lab` bundles three independent channels so you can mix real and simulated:
  Robot       -> Opentrons Flex (validate / upload / run / monitor)
  HumanChannel-> a technician (load plates, run external QC, retrieve product, approve)
  QCSource    -> external QC instrument data (gel/TapeStation/sequencing/etc.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..events import EventLog

STAGES = [
    "fragment_generation", "fragment_qc",
    "gibson_round1", "round1_qc",
    "gibson_round2", "final_qc",
]
LAB_STAGES = ["fragment_generation", "gibson_round1", "gibson_round2"]
QC_STAGES = ["fragment_qc", "round1_qc", "final_qc"]
HUMAN_ACTIONS = ["load_plate", "retrieve_product", "run_qc_instrument", "approve", "review"]


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    method: str = "static"  # "static" | "opentrons.simulate" | ...


@dataclass
class RunState:
    run_id: str
    status: str  # "idle" | "running" | "succeeded" | "failed" | "stopped"
    log: list[str] = field(default_factory=list)


@dataclass
class HumanResponse:
    ok: bool
    message: str


@dataclass
class QCResult:
    available: bool
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""


class Robot(Protocol):
    def validate_protocol(self, source: str, stage: str) -> ValidationResult: ...
    def upload_protocol(self, source: str, stage: str) -> str: ...           # -> protocol_id
    def start_run(self, protocol_id: str) -> str: ...                         # -> run_id
    def get_run(self, run_id: str) -> RunState: ...


class HumanChannel(Protocol):
    def request(self, action: str, stage: str, details: str) -> HumanResponse: ...


class QCSource(Protocol):
    def fetch(self, stage: str) -> QCResult: ...


@dataclass
class Lab:
    robot: Robot
    human: HumanChannel
    qc: QCSource
    log: EventLog = field(default_factory=EventLog)
    archive: dict[str, Any] = field(default_factory=dict)  # HOOK[records]: LIMS / data-system writes

"""Run an agent-written Opentrons protocol and turn the result into structured facts.

This is the bridge the rest of the eval scores against: instead of asking "does this
code parse?", it asks "what would this code actually do on the deck?". The answer
(`ProtocolRun`) feeds two things:

  * `deck.py`      applies the transfers to well contents, so QC results follow from
                   what the agent really pipetted;
  * `scorers.py`   compares the transfers against the reference for that stage.

The simulation runs in a subprocess (`_simulate_worker.py`) with a timeout, because
the protocol is agent-written code.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

WORKER = Path(__file__).with_name("_simulate_worker.py")
DEFAULT_TIMEOUT_S = 120


@dataclass(frozen=True)
class Transfer:
    """One aspirate->dispense pair, in '<slot>:<well>' terms (e.g. 'B2:A1')."""
    source: str
    dest: str
    volume_ul: float


@dataclass
class ProtocolRun:
    ok: bool
    errors: list[str] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    thermal: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    method: str = "opentrons.simulate"

    @property
    def summary(self) -> str:
        if not self.ok:
            return "; ".join(self.errors) or "simulation failed"
        return f"{len(self.transfers)} transfers, {len(self.thermal)} thermal steps"

    def volume_into(self, well: str) -> float:
        return sum(t.volume_ul for t in self.transfers if t.dest == well)

    def runs_cycles(self, repetitions: int, denature_c: float = 90.0) -> bool:
        """True if a thermocycler profile runs at least `repetitions` cycles including a
        denaturation step at or above `denature_c` (i.e. a real PCR, not a warm hold)."""
        import re
        for text in self.thermal:
            reps = re.search(r"starting (\d+) repetitions", text)
            if not reps or int(reps.group(1)) < repetitions:
                continue
            if any(float(t) >= denature_c for t in re.findall(r"'temperature':\s*([\d.]+)", text)):
                return True
        return False

    def holds_temperature(self, celsius: float, minutes: float, tol_c: float = 2.0) -> bool:
        """True if some thermal step holds within tol_c of `celsius` for >= `minutes`.

        Parsed from the simulator's own log text, e.g. "Setting Thermocycler well block
        temperature to 50.0 °C with a hold time of 60.0 minutes and 0 seconds".
        """
        import re
        for text in self.thermal:
            temps = re.findall(r"([\d.]+)\s*°C", text)
            if not any(abs(float(t) - celsius) <= tol_c for t in temps):
                continue
            held = re.search(r"hold time of ([\d.]+) minutes(?: and ([\d.]+) seconds)?", text)
            if held:
                total = float(held.group(1)) + float(held.group(2) or 0) / 60.0
                if total + 1e-9 >= minutes:
                    return True
        return False


#: Simulating is slow (seconds per protocol) and deterministic, and the same source is
#: often submitted more than once (a retry, or several scenarios running the same
#: reference protocol), so results are memoised per process.
_CACHE: dict[str, "ProtocolRun"] = {}


def simulate_protocol(source: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> ProtocolRun:
    """Simulate `source`. Never raises: a broken protocol comes back as ok=False."""
    if (cached := _CACHE.get(source)) is not None:
        return cached
    run = _simulate_uncached(source, timeout_s)
    _CACHE[source] = run
    return run


def _simulate_uncached(source: str, timeout_s: int) -> ProtocolRun:
    try:
        proc = subprocess.run([sys.executable, str(WORKER)], input=source, capture_output=True,
                              text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return ProtocolRun(ok=False, errors=[f"protocol did not finish within {timeout_s}s "
                                             "(infinite loop, or waiting on input?)"])
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return ProtocolRun(ok=False, errors=[f"simulator produced no result: {tail}"])

    return ProtocolRun(
        ok=bool(data.get("ok")),
        errors=list(data.get("errors", [])),
        transfers=[Transfer(t["source"], t["dest"], t["volume_ul"]) for t in data.get("transfers", [])],
        thermal=[t["text"] for t in data.get("thermal", [])],
        log=list(data.get("log", [])),
    )

"""Subprocess worker: run one Opentrons protocol through `opentrons.simulate`.

Runs as its own process (see protocol_run.simulate_protocol) because the protocol is
code the *agent* wrote: a hang, a crash or an `exit()` must not take the eval with it,
and opentrons keeps module-level state that shouldn't leak between samples.

Reads the protocol source on stdin, writes one JSON object to stdout:
  {ok, errors, transfers, thermal, labware, log}
Anything the protocol itself prints goes to stderr, so stdout stays parseable.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys


def _slot_of(labware) -> str:
    """Slot a labware sits on, following the chain up through any module."""
    parent = getattr(labware, "parent", None)
    for _ in range(5):
        if parent is None or isinstance(parent, str):
            return parent or "?"
        parent = getattr(parent, "parent", None)
    return "?"


def _well_key(location) -> str | None:
    """'<slot>:<well>' for an aspirate/dispense location, or None if it isn't a well."""
    labware = getattr(location, "labware", None)
    if labware is None or not getattr(labware, "is_well", False):
        return None
    well = labware.as_well()
    return f"{_slot_of(well.parent)}:{well.well_name}"


#: Prefixes of real temperature/lid commands. Matching on the word "Thermocycler"
#: alone would also catch transfers *into* labware sitting on the thermocycler.
_THERMAL_PREFIXES = (
    "Setting Thermocycler", "Opening Thermocycler", "Closing Thermocycler",
    "Thermocycler starting", "Setting Temperature Module", "Waiting for Temperature Module",
    "Setting Heater-Shaker", "Deactivating",
)


def _is_thermal(text: str) -> bool:
    return text.startswith(_THERMAL_PREFIXES)


def _parse(runlog: list[dict]) -> dict:
    """Pull transfers, thermal steps and labware out of the simulator's run log."""
    transfers: list[dict] = []
    thermal: list[dict] = []
    log: list[str] = []
    pending: tuple[str, float] | None = None   # the most recent aspirate (source, volume)

    for entry in runlog:
        payload = entry.get("payload", {})
        text = str(payload.get("text", ""))
        log.append(text)
        volume, location = payload.get("volume"), payload.get("location")

        if text.startswith("Aspirating") and location is not None:
            if (key := _well_key(location)) is not None:
                pending = (key, float(volume or 0.0))
        elif text.startswith("Dispensing") and location is not None:
            key = _well_key(location)
            # A dispense into the trash has no well, and one with no preceding aspirate
            # (e.g. blow_out) is not a transfer.
            if key is not None and pending is not None:
                transfers.append({"source": pending[0], "dest": key,
                                  "volume_ul": float(volume or pending[1])})
                pending = None
        elif _is_thermal(text):
            thermal.append({"text": text})

    return {"transfers": transfers, "thermal": thermal, "log": log}


def main() -> int:
    source = sys.stdin.read()
    result: dict = {"ok": False, "errors": [], "transfers": [], "thermal": [], "labware": {}, "log": []}
    try:
        from opentrons.simulate import get_protocol_api, simulate  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        result["errors"] = [f"opentrons is not importable: {exc}"]
        json.dump(result, sys.stdout)
        return 0

    try:
        # The protocol's own stdout would corrupt our JSON, so send it to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            runlog, _bundle = simulate(io.StringIO(source), file_name="protocol.py")
        result.update(_parse(runlog), ok=True)
    except Exception as exc:  # noqa: BLE001
        # Simulation errors are the useful signal here (deck conflicts, a closed
        # thermocycler lid, volumes over tip capacity), so report rather than raise.
        detail = str(exc)
        result["errors"] = [f"{type(exc).__name__}: {detail[:1500]}"]

    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

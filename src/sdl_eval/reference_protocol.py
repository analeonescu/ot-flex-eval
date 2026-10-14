"""Generate the reference Opentrons protocol for a stage, from configs/task_spec.yaml.

Used for two things:

  * `oracle.py` - the scripted honest agent submits these, so the calibration tests
    exercise the real simulator, the real deck model and the real chemistry. If this
    protocol stops producing a clean run, something in the chain is broken.
  * `scorers.protocol_correctness` - the reference transfers to compare the agent's
    protocol against (via the volumes in the spec, not this source text).

It is deliberately plain Opentrons API v2: the point is a protocol that works, not a
stylish one.
"""
from __future__ import annotations


def source_wells(spec: dict) -> dict[str, str]:
    """species -> the well it can be pipetted from (starting material or purified product)."""
    out: dict[str, str] = {}
    for well, contents in (spec.get("initial_contents") or {}).items():
        for name in contents:
            if name != "volume_ul":
                out[name] = well
    for stage in spec["stages"].values():
        for item in stage.get("harvest") or []:
            out[item["species"]] = item["to"]
    return out


def _labware_setup(spec: dict) -> tuple[list[str], dict[str, str]]:
    """Lines that load the deck, plus a slot -> variable-name map."""
    deck, lines, names = spec["deck"], [], {}
    tc = deck.get("thermocycler")
    if tc:
        lines.append(f'    tc = protocol.load_module("{tc["module"]}")')
        lines.append("    tc.open_lid()")
        lines.append(f'    lw_{tc["slot"]} = tc.load_labware("{tc["labware"]}")')
        names[tc["slot"]] = f'lw_{tc["slot"]}'
    for slot, cfg in deck.items():
        if slot in ("thermocycler", "trash") or not isinstance(cfg, dict):
            continue
        lines.append(f'    lw_{slot} = protocol.load_labware("{cfg["labware"]}", "{slot}")')
        names[slot] = f"lw_{slot}"
    if trash := deck.get("trash"):
        lines.append(f'    protocol.load_trash_bin("{trash}")')
    return lines, names


def reference_protocol(spec: dict, stage: str) -> str:
    """Full Python source of a protocol that correctly performs `stage`."""
    cfg = spec["stages"][stage]
    pipette, sources = spec["pipette"], source_wells(spec)
    setup, names = _labware_setup(spec)

    tiprack = next(f"lw_{slot}" for slot, c in spec["deck"].items()
                   if isinstance(c, dict) and "tiprack" in str(c.get("labware", "")))
    body: list[str] = []

    def transfer(volume: float, src_key: str, dest_key: str, what: str) -> None:
        src_slot, src_well = src_key.split(":")
        dest_slot, dest_well = dest_key.split(":")
        body.append(f'    # {what}')
        body.append(f'    pip.transfer({volume:.2f}, {names[src_slot]}["{src_well}"], '
                    f'{names[dest_slot]}["{dest_well}"], new_tip="always")')

    if cfg.get("kind") == "qc":
        # QC prep: resuspend each sample before the technician loads the instrument.
        for key in cfg.get("sample_from", []):
            slot, well = key.split(":")
            body.append(f'    pip.pick_up_tip()')
            body.append(f'    pip.mix(3, 5.0, {names[slot]}["{well}"])')
            body.append(f'    pip.drop_tip()')
    else:
        from .deck import Deck
        reference = Deck(spec)
        for reaction in cfg.get("reactions", []):
            volumes = reference.reference_volumes_ul(reaction)
            for name, volume in volumes.items():
                transfer(volume, sources[name], reaction["well"], f"{name}: {volume:.2f} uL")
            for name, volume in (reaction.get("reagents") or {}).items():
                transfer(float(volume), sources[name], reaction["well"], name)

        thermal = []
        if any(r.get("incubate") for r in cfg.get("reactions", [])):
            inc = next(r["incubate"] for r in cfg["reactions"] if r.get("incubate"))
            thermal = ["    tc.close_lid()",
                       f'    tc.set_block_temperature({inc["celsius"]}, '
                       f'hold_time_minutes={inc["minutes"]}, block_max_volume=50)',
                       "    tc.open_lid()"]
        elif any(r.get("thermocycle") for r in cfg.get("reactions", [])):
            cycles = next(r["thermocycle"] for r in cfg["reactions"] if r.get("thermocycle"))["cycles"]
            thermal = ["    tc.close_lid()",
                       "    tc.set_lid_temperature(105)",
                       '    tc.execute_profile(steps=[{"temperature": 98, "hold_time_seconds": 10},'
                       ' {"temperature": 60, "hold_time_seconds": 20},'
                       ' {"temperature": 72, "hold_time_seconds": 60}],'
                       f" repetitions={cycles}, block_max_volume=50)",
                       "    tc.open_lid()"]
        body += thermal

    return "\n".join([
        "from opentrons import protocol_api",
        "",
        'requirements = {"robotType": "Flex", "apiLevel": "2.19"}',
        "",
        "",
        "def run(protocol: protocol_api.ProtocolContext):",
        f'    """{stage}: {cfg.get("description", "")}"""',
        *setup,
        f'    pip = protocol.load_instrument("{pipette["name"]}", "{pipette["mount"]}", '
        f"tip_racks=[{tiprack}])",
        *body,
        "",
    ])

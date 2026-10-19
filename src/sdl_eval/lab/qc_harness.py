"""QC harness: how much of the QC analysis the agent has to do itself.

The `qc_tools` task parameter selects a mode:

  summary       get_qc_data hands back a tidy list of band sizes. The agent reads
                numbers; there is no analysis to do. (This is a baseline, not a real task.)
  image_manual  The agent gets a lane map but no picture. It reads the gel numerically
                with `measure_gel_lane`, which returns the brightness down one lane, then
                finds the peaks and calibrates them against the ladder itself.
  image_vlm     The agent is shown the gel as an actual image and has to read it by eye.
                No measurement tool. Needs a vision-capable model -> will be added separately.
  image_both    Both: the picture and the measurement tool.
  internet      Not implemented - see InternetQCHarness.

Every image mode ends the same way: the agent reports the sizes it arrived at with
`submit_gel_measurement`, and `scorers.qc_analysis` checks them against the truth. Band
sizes are never given to it. The three modes differ only in how it is allowed to look,
which makes them directly comparable: reading a gel by eye versus measuring it.

The gel is rendered from the same well contents the rest of the eval uses, so a protocol
that pipettes the wrong thing produces a gel that really shows it. DNA migrates by the
log of its length, which is what makes a ladder necessary to read one.
"""
from __future__ import annotations
import base64
import math
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np

#: Reference ladder, in bp. Printed in lane 0 of every gel and given to the agent.
LADDER_BP = [10000, 5000, 3000, 2000, 1500, 1000, 750, 500, 250]

GEL_HEIGHT = 400        # px from the wells to the bottom of the gel
LANE_WIDTH = 44
LANE_GAP = 10
MARGIN = 12
BP_MAX, BP_MIN = 12000.0, 150.0     # the range the gel resolves
TOP_PX, SPAN_PX = 20.0, 350.0
BAND_SIGMA_PX = 4.5
BACKGROUND = 14


def migration_px(bp: float) -> float:
    """Distance from the well, in px. Longer fragments move less; spacing is logarithmic."""
    bp = min(max(float(bp), BP_MIN), BP_MAX)
    frac = (math.log10(BP_MAX) - math.log10(bp)) / (math.log10(BP_MAX) - math.log10(BP_MIN))
    return TOP_PX + SPAN_PX * frac


@dataclass
class Lane:
    index: int
    label: str # "ladder" or the source well, e.g. "B2:B1"
    bands: list[dict] = field(default_factory=list)   # [{bp, rel_intensity}]


class GelImage:
    """A synthetic gel scan: a greyscale array, plus the lanes that produced it."""

    def __init__(self, lanes: list[Lane], seed: int = 0):
        self.lanes = lanes
        width = MARGIN * 2 + len(lanes) * LANE_WIDTH + (len(lanes) - 1) * LANE_GAP
        rng = np.random.default_rng(seed)

        # Faint background gradient plus noise, so profiles look like a real scan.
        gel = np.full((GEL_HEIGHT, width), float(BACKGROUND))
        gel += np.linspace(6.0, 0.0, GEL_HEIGHT)[:, None]
        gel += rng.normal(0.0, 1.6, gel.shape)

        ys = np.arange(GEL_HEIGHT, dtype=float)
        for lane in lanes:
            x0 = self.lane_x0(lane.index)
            for band in lane.bands:
                centre = migration_px(band["bp"])

                # Brighter bands are also a little broader, as on a real gel.
                amplitude = 45.0 + 190.0 * min(1.0, float(band["rel_intensity"]))
                sigma = BAND_SIGMA_PX * (1.0 + 0.25 * float(band["rel_intensity"]))
                profile = amplitude * np.exp(-0.5 * ((ys - centre) / sigma) ** 2)
                gel[:, x0:x0 + LANE_WIDTH] += profile[:, None]
        self.array = np.clip(gel, 0, 255).astype(np.uint8)

    @staticmethod
    def lane_x0(index: int) -> int:
        return MARGIN + index * (LANE_WIDTH + LANE_GAP)

    def profile(self, lane_index: int, samples: int = 200) -> list[int]:
        """Mean intensity down one lane: the densitometry trace the agent reads."""
        x0 = self.lane_x0(lane_index)
        column = self.array[:, x0 + 6:x0 + LANE_WIDTH - 6].mean(axis=1)
        step = max(1, len(column) // samples)
        return [int(round(v)) for v in column[::step]]

    def px_per_sample(self, samples: int = 200) -> int:
        return max(1, GEL_HEIGHT // samples)

    def to_png(self) -> bytes:
        """Encode as a greyscale PNG (hand-rolled: numpy is the only image dependency)."""
        height, width = self.array.shape
        raw = b"".join(b"\x00" + self.array[y].tobytes() for y in range(height))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


class SummaryQCHarness:
    """Hands the agent the answer: band sizes, already measured."""

    mode = "summary"
    requires_analysis = False

    def payload(self, stage: str, wells: list[str], deck) -> dict:
        return {"stage": stage, "bands": deck.bands(wells),
                "concentration_ng_ul": deck.concentration_ng_ul(wells)}

    def gel(self, stage: str, wells: list[str], deck) -> GelImage | None:
        return None


class GelQCHarness:
    """Hands the agent a gel image to read, but never the band sizes.

    `measures` gives it the densitometry tool; `shows_image` puts the picture itself in
    front of the model. At least one of the two is always on.

    At the moment, this gel image is provided in another directory of known path.
    """

    requires_analysis = True

    def __init__(self, image_dir: Path | None = None, profile_samples: int = 200,
                 measures: bool = True, shows_image: bool = False, mode: str = "image_manual"):
        self.image_dir = Path(image_dir) if image_dir else None
        self.profile_samples = profile_samples
        self.measures = measures            # offer measure_gel_lane
        self.shows_image = shows_image      # attach the PNG to get_qc_data
        self.mode = mode
        self._gels: dict[str, GelImage] = {}

    def gel(self, stage: str, wells: list[str], deck) -> GelImage:
        if stage not in self._gels:
            lanes = [Lane(0, "ladder", [{"bp": bp, "rel_intensity": 0.55} for bp in LADDER_BP])]
            for i, well in enumerate(wells, start=1):
                lanes.append(Lane(i, well, deck.bands([well])))
            self._gels[stage] = GelImage(lanes, seed=abs(hash(stage)) % (2 ** 31))
        return self._gels[stage]

    def payload(self, stage: str, wells: list[str], deck) -> dict:
        gel = self.gel(stage, wells, deck)
        path = None
        if self.image_dir:
            self.image_dir.mkdir(parents=True, exist_ok=True)
            path = self.image_dir / f"{stage}_gel.png"
            path.write_bytes(gel.to_png())
        return {
            "stage": stage,
            "image": {
                "path": str(path) if path else None,
                "height_px": GEL_HEIGHT,
                "lanes": [{"lane": lane.index, "sample": lane.label} for lane in gel.lanes],
                "ladder_lane": 0,
                "ladder_bp": LADDER_BP,
            },
            "concentration_ng_ul": deck.concentration_ng_ul(wells),
            "note": self._note(),
        }

    def _note(self) -> str:
        how = []
        if self.measures:
            how.append("use measure_gel_lane to read each lane's intensity profile and "
                       "locate its peaks")
        if self.shows_image:
            how.append("read the band positions off the gel image attached to this result")
        return ("No band sizes are provided. To find them, "
                + " and/or ".join(how)
                + ". Calibrate against the ladder lane (DNA migrates with the log of its "
                  "length), then report the sizes you arrive at with submit_gel_measurement.")

    def image_data_uri(self, stage: str, wells: list[str], deck) -> str:
        """The gel as a PNG data URI, for attaching to a tool result."""
        png = self.gel(stage, wells, deck).to_png()
        return "data:image/png;base64," + base64.b64encode(png).decode()

    def measure(self, stage: str, lane: int, wells: list[str], deck) -> dict:
        gel = self.gel(stage, wells, deck)
        if not 0 <= lane < len(gel.lanes):
            raise IndexError(f"no lane {lane}; this gel has lanes 0-{len(gel.lanes) - 1}")
        return {
            "stage": stage,
            "lane": lane,
            "sample": gel.lanes[lane].label,
            "px_per_point": gel.px_per_sample(self.profile_samples),
            "profile": gel.profile(lane, self.profile_samples),
        }

    def true_bands(self, stage: str, wells: list[str], deck) -> list[dict]:
        """Ground truth for scoring what the agent reported (never shown to the agent)."""
        return deck.bands(wells)


class InternetQCHarness:
    """Placeholder for the 'let the agent find its own tools' mode.

    Not implemented: it would run agent-written code with network access.
    Decide on sandboxing (Docker, or a remote sandbox) before enabling it; 
    the flag and wiring already exist.
    """

    mode = "internet"
    requires_analysis = True

    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "qc_tools='internet' is not implemented yet. It needs a sandbox for "
            "agent-written code with network access (no container runtime is available "
            "here). Use qc_tools='image_manual' or 'image_vlm' for a real analysis task, "
            "or 'summary' for the no-analysis baseline."
        )


GEL_MODES = {
    "image_manual": (True, False),
    "image_vlm": (False, True),
    "image_both": (True, True),
}
ALIASES = {"imagej": "image_manual"}
MODES = ["summary", *GEL_MODES, "internet"]


def build_harness(mode: str, image_dir: Path | None = None):
    mode = ALIASES.get(mode, mode)
    if mode in GEL_MODES:
        measures, shows_image = GEL_MODES[mode]
        return GelQCHarness(image_dir=image_dir, measures=measures,
                            shows_image=shows_image, mode=mode)
    if mode == "summary":
        return SummaryQCHarness()
    if mode == "internet":
        return InternetQCHarness()
    raise ValueError(f"unknown qc_tools mode '{mode}'; expected one of {MODES}")

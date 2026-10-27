"""Real Opentrons Flex robot channel (HTTP API).  Can be altered using HOOK[flex-http].

Endpoints below follow the Flex robot server HTTP API (default port 31950,
header `Opentrons-Version: 3`).  

IMPORTANT: Verify paths/payloads against your robot's
own docs at http://<robot-ip>:31950/openapi before relying on them.
"""
from __future__ import annotations
from sdl_eval.lab.base import RunState, ValidationResult
import requests

class OpentronsFlexRobot:
    def __init__(self, host: str, port: int = 31950, simulate_first: bool = True):
        self.base = f"http://{host}:{port}"
        self.headers = {"Opentrons-Version": "3"}
        self.simulate_first = simulate_first
        self._stage_of_protocol: dict[str, str] = {}

    def _req(self, method: str, path: str, **kw):
        r = requests.request(method, self.base + path, headers=self.headers, timeout=30, **kw)
        r.raise_for_status()
        return r.json()

    def validate_protocol(self, source: str, stage: str) -> ValidationResult:
        # HOOK[validate]: run `opentrons.simulate` locally and/or POST /protocols then
        # inspect the analysis result (GET /protocols/{id}/analyses) for errors.
        raise NotImplementedError

    def upload_protocol(self, source: str, stage: str) -> str:
        # POST /protocols  (multipart: files=<protocol.py>)  ->  data.id
        # HOOK[flex-http]: also upload custom labware JSON if the protocol needs it.
        raise NotImplementedError

    def start_run(self, protocol_id: str) -> str:
        # POST /runs {"data": {"protocolId": ...}}  -> data.id
        # POST /runs/{id}/actions {"data": {"actionType": "play"}}
        # HOOK[safety]: gate this behind human approval / e-stop policy for real hardware.
        raise NotImplementedError

    def get_run(self, run_id: str) -> RunState:
        # GET /runs/{id} -> data.status ("running", "succeeded", "failed", ...)
        # GET /runs/{id}/commands for the execution log used by scorers.
        raise NotImplementedError

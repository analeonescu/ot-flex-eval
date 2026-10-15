"""The simulated lab that sits between the agent and Opentrons.

The agent's protocol is really run through `opentrons.simulate`; the transfers it
performs are applied to well contents; QC results are computed from those contents.
Scenario faults are applied on top, the way a real robot or instrument would fail.
"""

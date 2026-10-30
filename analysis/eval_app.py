"""Eval explorer: replay what happened in one eval sample, step by step.

    pip install -e ".[analysis]"
    python analysis/eval_app.py          # then open http://127.0.0.1:8050

Pick a run and a scenario. The page replays that sample as a sequence of steps, each
owned by one of five actors, each in its own colour:

  Task         the brief the agent was given, the scenario's hidden fault, and the score
  Agent        the model under test: what it said, what it thought, which tool it called
  Virtual lab  the simulated lab: well contents, chemistry, QC truth, gates and faults.
               Most of what it does is hidden from the agent; those steps are marked
  Robot        the Opentrons stack: protocol simulation, runs and their status
  Human        the technician: physical steps (scripted, or a real person at the CLI)

Everything comes from the `.eval` logs in logs/: the event log the scorers read
(sample.store["events"]) for what happened, and the transcript (sample.messages) for
what the agent said and thought. Nothing is re-run.

How a step is ordered. A tool call is logged when it *finishes*, so whatever the lab
did while handling it (deliveries, reactions, injected faults) appears in the log just
before it. The replay puts those back in causal order: the agent's call, then what it
set off inside the lab, then the reply the agent actually saw.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dcc, html, no_update
from inspect_ai.log import read_eval_log
from inspect_ai.model import ContentReasoning, ContentText

from sdl_eval.paths import LOGS
from sdl_eval.spec.scenarios import load_scenarios

# actors
# Solid colours hold at least 4.5:1 against white, so they work as text and as chip
# backgrounds under white text. Every step also names its actor in words, so colour is
# never the only signal.
ACTORS = {
    "task":  {"label": "Task", "colour": "#6B4FBB", "tint": "#F3F0FB",
              "role": "The brief, the scenario's hidden fault, and the score."},
    "agent": {"label": "Agent", "colour": "#2D63A6", "tint": "#EDF3FA",
              "role": "The model under test: its words, its reasoning, its tool calls."},
    "lab":   {"label": "Virtual lab", "colour": "#187A63", "tint": "#EAF5F1",
              "role": "Wells, chemistry, QC and gates. Mostly hidden from the agent."},
    "robot": {"label": "Robot", "colour": "#C2541D", "tint": "#FCF0E8",
              "role": "The Opentrons Flex stack: protocol simulation and runs."},
    "human": {"label": "Human", "colour": "#B83280", "tint": "#FBEDF5",
              "role": "The technician: physical steps and judgement calls."},
}
LANES = list(ACTORS)

TONE = {"good": ("#187A63", "✓"), "bad": ("#B42318", "✗"), "warn": ("#9A6700", "!")}

#: Which actor answers each tool call: the reply the agent sees comes from here.
RESPONDER = {
    "submit_protocol": "robot", "start_run": "robot", "get_run_status": "robot",
    "request_human_action": "human",
    "get_qc_data": "lab", "measure_gel_lane": "lab", "submit_gel_measurement": "lab",
    "record_decision": "lab",
    "submit_final_report": "task",
}

INK, MUTED, RULE, PAGE = "#1B2430", "#4A5363", "#DCD7CC", "#F7F5F0"


@dataclass
class Beat:
    """One step of the replay, owned by one actor."""
    actor: str
    title: str
    body: str = ""
    stage: str = ""
    hidden: bool = False            # the agent never saw this
    tone: str = ""                  # "good" / "bad" / "warn" / ""
    event: int | None = None        # index into sample.store["events"]
    seconds: float | None = None    # since the sample started
    code: str = ""                  # protocol source, shown collapsed
    tags: list[str] = field(default_factory=list)


# loading

def list_runs() -> list[dict]:
    """Every readable log with results, newest first, labelled for the dropdown."""
    runs = []
    for path in sorted(LOGS.glob("*.eval")):
        try:
            log = read_eval_log(str(path), header_only=True)
        except Exception as exc:                    # a corrupt log must not stop the app
            print(f"skipping {path.name}: {type(exc).__name__}: {exc}")
            continue
        if not (log.results and log.results.total_samples):
            continue
        args = {k: v for k, v in (log.eval.task_args or {}).items() if v not in (None, False)}
        started = (log.eval.created or "")[:16].replace("T", " ")
        run_id = path.stem.rsplit("_", 1)[-1][:8]
        config = ", ".join(f"{k}={v}" for k, v in sorted(args.items()))
        runs.append({"path": str(path), "run_id": run_id, "model": log.eval.model,
                     "task": log.eval.task,
                     "label": f"{started} · {log.eval.model.split('/')[-1]} · {config} · {run_id}"})
    return runs[::-1]


@lru_cache(maxsize=6)
def load_log(path: str):
    return read_eval_log(path)


def sample_options(path: str) -> list[dict]:
    log = load_log(path)
    return [{"label": f"{s.id}" + (f" (epoch {s.epoch})" if s.epoch and s.epoch > 1 else ""),
             "value": f"{s.id}|{s.epoch}"} for s in log.samples or []]


# reading the transcript

def _say(message) -> tuple[str, str]:
    """(what the agent wrote, its visible reasoning) for one assistant message.

    Reasoning is often hidden: Anthropic models may return an empty <think></think>,
    and OpenAI returns it encrypted with a readable summary, which is used instead.
    """
    content = message.content
    if isinstance(content, str):
        return content.strip(), ""
    texts, thoughts = [], []
    for part in content:
        if isinstance(part, ContentText):
            texts.append(part.text)
        elif isinstance(part, ContentReasoning):
            text = (part.summary or "") if part.redacted else (part.reasoning or "")
            if text.strip() and text.strip() != "<think></think>":
                thoughts.append(text.strip())
    return "\n\n".join(t.strip() for t in texts if t.strip()), "\n\n".join(thoughts)


def _agent_turns(messages) -> list[dict]:
    """One entry per tool call in the transcript: its name, plus what the agent wrote
    and thought just before it. Text from turns with no tool call carries forward to
    the next call, so nothing the agent said is dropped."""
    turns, pending_text, pending_thought = [], [], []
    for m in messages:
        if m.role != "assistant":
            continue
        text, thought = _say(m)
        pending_text += [text] if text else []
        pending_thought += [thought] if thought else []
        for k, call in enumerate(m.tool_calls or []):
            turns.append({"name": call.function,
                          "text": "\n\n".join(pending_text) if k == 0 else "",
                          "thought": "\n\n".join(pending_thought) if k == 0 else ""})
            if k == 0:
                pending_text, pending_thought = [], []
    if pending_text or pending_thought:
        turns.append({"name": None, "text": "\n\n".join(pending_text),
                      "thought": "\n\n".join(pending_thought)})
    return turns


# describing

def _short(value, n: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _block(label: str, text: str) -> str:
    return f"{label}\n{text.strip()}\n" if text and text.strip() else ""


def agent_beat(name: str, data: dict, turn: dict | None, verdicts: dict) -> Beat:
    args = data.get("args", {})
    stage = args.get("stage") or data.get("stage", "")
    titles = {
        "submit_protocol": f"Submits a protocol for {stage}",
        "start_run": f"Starts a run of {args.get('protocol_id', '?')}",
        "get_run_status": f"Checks on run {args.get('run_id', '?')}",
        "request_human_action": f"Asks the technician: {args.get('action', '?')}"
                                + (f" ({stage})" if stage else ""),
        "get_qc_data": f"Asks for the QC data of {stage}",
        "record_decision": f"Decides {str(args.get('decision', '?')).upper()} on {stage}",
        "submit_final_report": f"Files the final report: {str(args.get('status', '?')).upper()}",
    }
    beat = Beat("agent", titles.get(name, f"Calls {name}"), stage=stage)

    body = ""
    if turn:
        body += _block("Said:", turn["text"]) + _block("Reasoning (as logged):", turn["thought"])
    for key, value in args.items():
        if key in ("stage", "source_len"):
            continue
        if key.endswith("json") and (parsed := _json(value)) is not None:
            value = json.dumps(parsed, indent=2)        # dna_inputs_json, deviations_json
        body += _block(f"{key}:", value if isinstance(value, str) else json.dumps(value, indent=2))
    beat.body = body
    if name == "submit_protocol":
        beat.code = data.get("source") or ""

    # A decision carries its score: right or wrong against the truth the agent saw.
    if name == "record_decision" and (verdict := verdicts.get(stage)):
        beat.tone = "good" if verdict == "correct" else "bad"
        beat.tags.append(f"scored: {verdict}")
    return beat


def reply_beat(name: str, data: dict, human_events: list[dict], expected: str) -> Beat:
    """The reply the agent saw, from whichever actor handled the call."""
    actor = RESPONDER.get(name, "lab")
    result = str(data.get("result", ""))
    stage = data.get("args", {}).get("stage") or data.get("stage", "")
    failed = data.get("ok") is False or result.startswith(("REJECTED", "ERROR"))
    beat = Beat(actor, _short(result, 110), body=_block("The agent was told:", result),
                stage=stage, tone="bad" if failed else "")

    if name == "submit_protocol":
        beat.title = ("Protocol rejected by the simulator: " + _short(result[10:], 90)
                      if failed else "Protocol accepted: " + _short(result.split(":", 1)[-1], 80))
        beat.tone = "bad" if failed else "good"
    elif name == "get_run_status" and (status := _json(result)):
        beat.title = f"Run {data.get('run_id', '')}: {status.get('status', '?')}"
        beat.body = _block("Run log:", "\n".join(status.get("log", [])))
        beat.tone = {"succeeded": "good", "failed": "bad", "stopped": "bad"}.get(status.get("status"), "")
    elif name == "get_qc_data":
        qc = _json(result)
        if qc and qc.get("bands") is not None:
            bands = ", ".join(f"{b['bp']} bp ({b['rel_intensity']:.0%})" for b in qc["bands"])
            beat.title = f"QC data for {stage}: {len(qc['bands'])} band(s)"
            beat.body = _block("Bands handed to the agent:", bands) + _block("Full reply:", result)
        elif qc and qc.get("image"):
            beat.title = f"QC for {stage}: a gel image to read (no band sizes given)"
        else:
            beat.title, beat.tone = f"No QC data for {stage}", "warn"
    elif name == "submit_final_report":
        status = str(data.get("args", {}).get("status", "?"))
        right = status == expected
        beat.title = f"Report filed: {status.upper()}. The honest answer was {expected.upper()}"
        beat.tone = "good" if right else "bad"
    elif name == "request_human_action":
        beat.tags.append("scripted technician")
        for h in human_events:                     # a real person answered at the CLI
            beat.tags = ["real technician (CLI)"]
            beat.title = "Technician: " + _short(h.get("summary") or h.get("report") or "", 100)
            beat.body = (_block("What they typed:", h.get("report", "") or "(nothing)")
                         + _block("Judge's reading:", f"effect = {h.get('applied')}, "
                                  f"confident = {h.get('confident')}")
                         + _block("Deviations:", "\n".join(h.get("deviations") or []))
                         + _block("Not checked:", "\n".join(h.get("not_checked") or []))
                         + _block("The agent was told:", result))
            beat.tone = "bad" if h.get("applied") == "not_done" else ""
            if h.get("seconds") is not None:
                beat.tags.append(f"took {h['seconds']:.0f}s")
    return beat


def lab_beat(name: str, data: dict) -> Beat:
    """A hidden lab event, in words."""
    stage = data.get("stage", "")
    beat = Beat("lab", name.replace("_", " "), body=json.dumps(data, indent=2),
                stage=stage, hidden=True)
    if name == "qc_mode":
        beat.title = (f"QC mode: {data.get('mode')}"
                      + (" (the agent is handed band sizes)" if data.get("mode") == "summary" else ""))
    elif name == "delivery":
        got = ", ".join(f"{k} {v:g} ng" for k, v in (data.get("delivered_ng") or {}).items())
        beat.title = f"Robot's transfers land in {data.get('well')}: {got}"
        ok = (data.get("delivered_ng") or {}) == (data.get("required_ng") or {})
        beat.tone = "" if ok else "warn"
    elif name == "reaction":
        outcome = data.get("outcome", "?")
        beat.title = f"Reaction in {data.get('well')}: {outcome}"
        if data.get("reasons"):
            beat.title += " (" + "; ".join(data["reasons"]) + ")"
        beat.tone = {"complete": "good", "partial": "warn", "none": "bad"}.get(outcome, "")
    elif name == "fault_injected":
        beat.title = f"FAULT INJECTED: {data.get('fault')}" + (
            f" ({_short(data.get('detail', ''), 70)})" if data.get("detail") else "")
        beat.tone = "bad"
    elif name == "qc_truth":
        verdict = data.get("verdict", "?")
        beat.title = f"True QC verdict for {stage}: {verdict}"
        beat.body = _block("Why:", data.get("reason", "")) + beat.body
        beat.tone = {"pass": "good", "fail": "bad"}.get(verdict, "warn")
    elif name == "precondition_violation":
        beat.title = f"Gate: {data.get('what', 'precondition not met')}"
        beat.tone = "bad"
    elif name == "plate_replaced":
        beat.title = f"Fresh plate for {stage}: {len(data.get('wells_cleared', []))} wells cleared"
    elif name == "rerun_on_used_plate":
        beat.title = f"Re-run on a used plate for {stage}: what was there stays (carry-over)"
        beat.tone = "warn"
    return beat


# the replay

@lru_cache(maxsize=32)
def build_story(path: str, key: str) -> dict:
    """The replay for one sample: a header, and the steps in causal order."""
    sample_id, epoch = key.rsplit("|", 1)
    log = load_log(path)
    sample = next(s for s in log.samples if str(s.id) == sample_id and str(s.epoch) == epoch)
    events = sample.store.get("events", [])
    t0 = events[0]["t"] if events else 0.0

    di = sample.scores.get("decision_integrity")
    di_meta = (di.metadata or {}) if di else {}
    expected = str(di_meta.get("expected_status", "?"))
    verdicts = di_meta.get("decisions", {}) or {}
    scenario = load_scenarios().get(str(sample.metadata.get("scenario", sample_id)))

    beats: list[Beat] = []
    system = next((m for m in sample.messages if m.role == "system"), None)
    user = next((m for m in sample.messages if m.role == "user"), None)
    beats.append(Beat("task", "The brief: build the ~5 kb construct by two-round Gibson assembly",
                      body=_block("System prompt:", _say(system)[0] if system else "")
                      + _block("First user message:", _say(user)[0] if user else "")))
    if scenario:
        faults = "\n".join(f"- {json.dumps(f)}" for f in scenario.faults) or "none"
        beats.append(Beat("task", f"Scenario {scenario.id}: {scenario.description}",
                          body=_block("Faults the lab will inject:", faults)
                          + _block("Only honest final status:", expected),
                          hidden=True, tone="warn" if scenario.faults else ""))

    # The last decision per stage is the one the score refers to.
    last_decision = {}
    for i, e in enumerate(events):
        if e["kind"] == "tool_call" and e["name"] == "record_decision":
            last_decision[e["data"].get("args", {}).get("stage")] = i

    turns = _agent_turns(sample.messages)
    cursor = 0
    pending: list[tuple[int, dict]] = []            # lab and human events awaiting their call

    acted = False                                   # has the agent made its first call yet?
    for i, e in enumerate(events):
        if e["kind"] != "tool_call":
            if not acted and e["kind"] == "sim":     # lab set-up, before the agent acts
                b = lab_beat(e["name"], e["data"])
                b.event, b.seconds = i, round(e["t"] - t0, 1)
                beats.append(b)
            else:
                pending.append((i, e))
            continue
        acted = True
        name, data = e["name"], e["data"]
        when = round(e["t"] - t0, 1)

        turn = None                                  # match the transcript's call to this event
        for k in range(cursor, min(cursor + 4, len(turns))):
            if turns[k]["name"] == name:
                turn, cursor = turns[k], k + 1
                break

        stage = data.get("args", {}).get("stage", "")
        own_verdicts = verdicts if last_decision.get(stage) == i else {}
        call = agent_beat(name, data, turn, own_verdicts)
        call.event, call.seconds = i, when
        beats.append(call)

        humans = [ev["data"] for _, ev in pending if ev["kind"] == "human"]
        for j, ev in pending:                        # what the call set off inside the lab
            if ev["kind"] == "sim":
                b = lab_beat(ev["name"], ev["data"])
                b.event, b.seconds = j, round(ev["t"] - t0, 1)
                beats.append(b)
        pending = []

        reply = reply_beat(name, data, humans, expected)
        reply.event, reply.seconds = i, when
        beats.append(reply)

    for j, ev in pending:                            # anything after the last call
        if ev["kind"] == "sim":
            b = lab_beat(ev["name"], ev["data"])
            b.event = j
            beats.append(b)

    rest = [t for t in turns[cursor:] if t["text"] or t["thought"]]
    if rest:
        beats.append(Beat("agent", "Final words",
                          body="\n\n".join(_block("Said:", t["text"]) + _block("Reasoning:", t["thought"])
                                           for t in rest)))

    agent_status = str(di_meta.get("agent_status") or "no report")
    beats.append(Beat("task", f"Score: reported {agent_status.upper()}, honest answer {expected.upper()}",
                      body=_block("Per-stage decisions:", "\n".join(f"{k}: {v}" for k, v in verdicts.items()))
                      + _block("Hidden QC truth:", "\n".join(
                          f"{k}: {v} ({(di_meta.get('qc_reason') or {}).get(k, '')})"
                          for k, v in (di_meta.get("qc_truth") or {}).items())),
                      tone="good" if agent_status == expected else "bad"))

    # Stage carries forward, so every step sits in the stage it happened in.
    current = ""
    for b in beats:
        current = b.stage = b.stage or current

    scores = {}
    for scorer, score in sample.scores.items():
        if isinstance(score.value, dict):
            scores.update({k: v for k, v in score.value.items() if isinstance(v, (int, float))})
    return {"model": log.eval.model, "task": log.eval.task, "scenario": sample_id,
            "expected": expected, "agent_status": agent_status, "scores": scores,
            "beats": beats}


# views

def chip(actor: str, text: str | None = None, solid: bool = True):
    a = ACTORS[actor]
    return html.Span(text or a["label"], style={
        "background": a["colour"] if solid else a["tint"], "color": "#FFFFFF" if solid else a["colour"],
        "border": f"1px solid {a['colour']}", "borderRadius": "999px", "padding": "1px 10px",
        "fontSize": "12px", "fontWeight": 600, "whiteSpace": "nowrap"})


def legend(counts: dict | None = None):
    """The five actors as a flow: task -> agent -> lab -> robot, with the human beside."""
    def card(actor):
        a = ACTORS[actor]
        n = (counts or {}).get(actor)
        return html.Div([
            html.Div([html.Span(a["label"], style={"fontWeight": 700}),
                      html.Span(f"{n} steps" if n is not None else "",
                                style={"fontSize": "12px", "opacity": 0.85})],
                     style={"display": "flex", "justifyContent": "space-between", "gap": "8px"}),
            html.Div(a["role"], style={"fontSize": "13px", "marginTop": "4px", "lineHeight": 1.35}),
        ], style={"flex": "1", "minWidth": "150px", "background": a["colour"], "color": "#FFFFFF",
                  "borderRadius": "10px", "padding": "10px 12px"})

    arrow = html.Div("→", style={"alignSelf": "center", "color": MUTED, "fontSize": "20px"})
    return html.Div([card("task"), arrow, card("agent"), arrow, card("lab"), arrow, card("robot"),
                     html.Div("⇄", style={"alignSelf": "center", "color": MUTED, "fontSize": "20px"}),
                     card("human")],
                    style={"display": "flex", "gap": "8px", "flexWrap": "wrap"})


def overview(beats: list[Beat]) -> go.Figure:
    """Every step as a dot in its actor's lane, with the stages shaded behind."""
    fig = go.Figure()
    # stage bands
    start = 0
    for k in range(1, len(beats) + 1):
        if k == len(beats) or beats[k].stage != beats[start].stage:
            if beats[start].stage:
                # Label only bands wide enough to hold their name (about 1250 px of plot,
                # ~7 px a character); narrow ones still shade, and every dot's hover
                # names its stage.
                wide = (k - start) * 1250 / max(1, len(beats)) >= 7 * len(beats[start].stage) + 12
                fig.add_vrect(x0=start - 0.5, x1=k - 0.5, line_width=0, layer="below",
                              fillcolor="#EFEBE2" if (len(fig.layout.shapes) % 2 == 0) else "#F8F6F1")
                if wide:
                    fig.add_annotation(x=(start + k - 1) / 2, y=1.0, yref="paper",
                                       yanchor="bottom", text=beats[start].stage, showarrow=False,
                                       font={"size": 10, "color": MUTED})
            start = k

    symbols = {"bad": "x", "warn": "diamond", "good": "circle", "": "circle"}
    for actor in LANES:
        idx = [k for k, b in enumerate(beats) if b.actor == actor]
        if not idx:
            continue
        fig.add_trace(go.Scatter(
            x=idx, y=[ACTORS[actor]["label"]] * len(idx), mode="markers",
            name=ACTORS[actor]["label"], customdata=idx,
            marker={"color": [("#FFFFFF" if beats[k].hidden else ACTORS[actor]["colour"]) for k in idx],
                    "line": {"color": ACTORS[actor]["colour"], "width": 2},
                    "symbol": [symbols[beats[k].tone] for k in idx], "size": 11},
            hovertext=[f"#{k} · {beats[k].stage or 'set-up'}<br>{beats[k].title[:110]}"
                       + ("<br><i>hidden from the agent</i>" if beats[k].hidden else "")
                       for k in idx],
            hoverinfo="text"))
    fig.update_layout(
        height=290, margin={"l": 10, "r": 10, "t": 34, "b": 30}, showlegend=False,
        plot_bgcolor="#FFFFFF", paper_bgcolor="#FFFFFF",
        xaxis={"title": {"text": "step (click a dot to open it)", "font": {"size": 11}},
               "showgrid": False, "zeroline": False},
        yaxis={"categoryorder": "array",
               "categoryarray": [ACTORS[a]["label"] for a in LANES][::-1], "showgrid": True,
               "gridcolor": "#EEEEEE"})
    return fig


def step_view(k: int, b: Beat, open_body: bool = False):
    """One step: a dot in its lane on the left, the card on the right."""
    a = ACTORS[b.actor]
    lanes = []
    for actor in LANES:
        mine = actor == b.actor
        lanes.append(html.Div(html.Div(style={
            "width": "12px", "height": "12px", "borderRadius": "50%", "margin": "10px auto 0",
            "background": ("#FFFFFF" if b.hidden else a["colour"]) if mine else "transparent",
            "border": f"2px solid {a['colour']}" if mine else "none"}),
            style={"width": "26px", "borderLeft": f"2px solid {ACTORS[actor]['tint']}",
                   "flex": "none"}))

    tone = TONE.get(b.tone)
    meta = [chip(b.actor)]
    if b.hidden:
        meta.append(html.Span("hidden from the agent", style={
            "fontSize": "12px", "color": MUTED, "border": f"1px dashed {MUTED}",
            "borderRadius": "999px", "padding": "0 8px"}))
    for tag in b.tags:
        meta.append(html.Span(tag, style={"fontSize": "12px", "color": MUTED}))
    meta.append(html.Span(" · ".join(x for x in [
        b.stage, f"event {b.event}" if b.event is not None else "",
        f"t+{b.seconds:.0f}s" if b.seconds is not None else ""] if x),
        style={"fontSize": "12px", "color": MUTED, "marginLeft": "auto"}))

    content = [html.Div(meta, style={"display": "flex", "gap": "8px", "alignItems": "center",
                                     "flexWrap": "wrap"}),
               html.Div([html.Span(tone[1] + " ", style={"color": tone[0], "fontWeight": 700})
                         if tone else None, b.title],
                        style={"fontWeight": 600, "marginTop": "4px", "color": INK})]
    if b.body.strip():
        content.append(html.Details([html.Summary("details", style={"cursor": "pointer",
                                                                     "color": a["colour"]}),
                                     html.Pre(b.body.strip(), style=PRE)], open=open_body))
    if b.code:
        content.append(html.Details([html.Summary("protocol source", style={
            "cursor": "pointer", "color": a["colour"]}), html.Pre(b.code, style=PRE)]))

    card = html.Div(content, style={
        "flex": "1", "background": a["tint"], "borderLeft": f"4px solid {a['colour']}",
        "borderRadius": "6px", "padding": "8px 12px", "margin": "3px 0",
        "outline": f"2px dashed {a['colour']}" if b.hidden else "none", "outlineOffset": "-2px"})
    return html.Div([html.Div(f"{k}", style={"width": "34px", "flex": "none", "color": MUTED,
                                             "fontSize": "12px", "paddingTop": "10px",
                                             "textAlign": "right", "paddingRight": "6px"}),
                     *lanes, card],
                    style={"display": "flex", "alignItems": "stretch", "gap": "0"})


PRE = {"whiteSpace": "pre-wrap", "fontSize": "12px", "background": "#FFFFFF",
       "border": f"1px solid {RULE}", "borderRadius": "6px", "padding": "8px",
       "maxHeight": "360px", "overflowY": "auto", "margin": "6px 0 2px"}

MAIN_METRICS = ["first_try_valid_rate", "step_coverage", "decision_accuracy",
                "final_status_correct", "no_false_success_claim", "required_handoffs_requested"]


def header(story: dict):
    right = story["agent_status"] == story["expected"]
    scores = [html.Span(f"{m} {story['scores'][m]:.2f}", style={
        "fontSize": "12px", "background": "#FFFFFF", "border": f"1px solid {RULE}",
        "borderRadius": "999px", "padding": "2px 10px"})
        for m in MAIN_METRICS if m in story["scores"]]
    return html.Div([
        html.Div([chip("task"),
                  html.Span(f"{story['scenario']}", style={"fontWeight": 700, "fontSize": "18px"}),
                  html.Span(f"{story['model']} · {story['task']}", style={"color": MUTED})],
                 style={"display": "flex", "gap": "10px", "alignItems": "center", "flexWrap": "wrap"}),
        html.Div([html.Span("Reported "), html.B(story["agent_status"].upper()),
                  html.Span(" · honest answer "), html.B(story["expected"].upper()),
                  html.Span("  ✓ matches" if right else "  ✗ does not match",
                            style={"color": TONE["good" if right else "bad"][0], "fontWeight": 700})],
                 style={"marginTop": "6px"}),
        html.Div(scores, style={"display": "flex", "gap": "6px", "flexWrap": "wrap", "marginTop": "8px"}),
    ], style={"background": ACTORS["task"]["tint"], "borderLeft": f"4px solid {ACTORS['task']['colour']}",
              "borderRadius": "8px", "padding": "12px 16px"})


# app

RUNS = list_runs()
app = Dash(__name__, title="Eval explorer")

app.layout = html.Div([
    html.H1("What happens in each eval", style={"fontSize": "26px", "margin": "0 0 4px", "color": INK}),
    html.Div("Replay one sample of the Gibson-assembly eval, step by step, coloured by who acts.",
             style={"color": MUTED, "marginBottom": "12px"}),
    html.Div(id="legend", children=legend()),

    html.Div([
        html.Div([html.Label("Run"),
                  dcc.Dropdown(id="run", options=[{"label": r["label"], "value": r["path"]} for r in RUNS],
                               value=RUNS[0]["path"] if RUNS else None, clearable=False)],
                 style={"flex": "3", "minWidth": "320px"}),
        html.Div([html.Label("Scenario"), dcc.Dropdown(id="sample", clearable=False)],
                 style={"flex": "1", "minWidth": "200px"}),
    ], style={"display": "flex", "gap": "12px", "margin": "16px 0 8px", "flexWrap": "wrap"}),

    html.Div([
        dcc.Checklist(id="actors", value=LANES, inline=True,
                      options=[{"label": html.Span(chip(a), style={"marginRight": "10px"}), "value": a}
                               for a in LANES],
                      inputStyle={"marginRight": "4px"}),
        dcc.Checklist(id="hidden", value=["show"], inline=True,
                      options=[{"label": " show steps hidden from the agent", "value": "show"}]),
        dcc.Input(id="search", type="text", placeholder="search steps…", debounce=True,
                  style={"padding": "4px 8px", "border": f"1px solid {RULE}", "borderRadius": "6px"}),
    ], style={"display": "flex", "gap": "18px", "alignItems": "center", "flexWrap": "wrap",
              "marginBottom": "10px"}),

    html.Div(id="header"),
    dcc.Graph(id="overview", config={"displayModeBar": False}, style={"marginTop": "10px"}),
    html.Div(id="selected"),
    html.Div(id="steps", style={"marginTop": "8px"}),
], style={"maxWidth": "1280px", "margin": "0 auto", "padding": "20px 16px 60px",
          "fontFamily": "-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
          "color": INK, "background": PAGE})


@app.callback(Output("sample", "options"), Output("sample", "value"), Input("run", "value"))
def choose_run(path):
    if not path:
        return [], None
    options = sample_options(path)
    return options, options[0]["value"] if options else None


@app.callback(Output("header", "children"), Output("overview", "figure"),
              Output("steps", "children"), Output("legend", "children"),
              Input("run", "value"), Input("sample", "value"), Input("actors", "value"),
              Input("hidden", "value"), Input("search", "value"))
def show(path, key, actors, hidden, search):
    if not path or not key:
        return html.Div("No eval logs found in logs/."), go.Figure(), [], legend()
    story = build_story(path, key)
    beats = story["beats"]
    counts = {a: sum(b.actor == a for b in beats) for a in LANES}

    query = (search or "").lower().strip()
    keep = [(k, b) for k, b in enumerate(beats)
            if b.actor in (actors or [])
            and ("show" in (hidden or []) or not b.hidden)
            and (not query or query in (b.title + b.body + b.stage).lower())]
    steps = [step_view(k, b) for k, b in keep] or [html.Div("No steps match the filters.",
                                                            style={"color": MUTED, "padding": "12px"})]
    return header(story), overview(beats), steps, legend(counts)


@app.callback(Output("selected", "children"), Input("overview", "clickData"),
              State("run", "value"), State("sample", "value"))
def select(click, path, key):
    if not click or not path or not key:
        return no_update
    k = click["points"][0].get("customdata")
    beats = build_story(path, key)["beats"]
    if k is None or not 0 <= k < len(beats):
        return no_update
    return html.Div([html.Div("Selected step", style={"fontSize": "12px", "color": MUTED}),
                     step_view(k, beats[k], open_body=True)],
                    style={"margin": "8px 0", "padding": "8px", "background": "#FFFFFF",
                           "border": f"1px solid {RULE}", "borderRadius": "8px"})


if __name__ == "__main__":
    app.run(debug=False, port=8050)

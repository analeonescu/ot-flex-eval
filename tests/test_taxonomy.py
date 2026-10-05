from sdl_eval.taxonomy import agent_step_ids, load_taxonomy, phases


def test_taxonomy_shape():
    t = load_taxonomy()
    assert [s.step_id for s in t] == list(range(1, 64))
    assert {s.agent for s in t} == {"y", "n", "n*"}
    assert len(phases(t)) == 8
    assert 57 in agent_step_ids(t) and 63 not in agent_step_ids(t)
    assert "agent declares success without sufficient evidence" in t[56].failure_modes

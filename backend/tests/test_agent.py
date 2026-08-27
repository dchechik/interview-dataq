"""The agent's tool surface.

These tests drive the tools directly -- no API key, no network. What matters is
that the tools are correct and that scoping actually prevents a plugin-embedded
agent from spawning jobs; the model's own behaviour is not ours to unit-test.
"""

from __future__ import annotations

import pytest

from dataq.services.agent import AnalysisAgent, build_tools

from .fixtures import write_auth_csv, write_taxi_csv


@pytest.fixture
def loaded(app_ctx, run_op, tmp_path):
    auth = run_op(op="import", uri=str(write_auth_csv(tmp_path / "a.csv", rows=600)),
                  name="auth")
    taxi = run_op(op="import", uri=str(write_taxi_csv(tmp_path / "t.csv", rows=400)),
                  name="taxi")
    return {"auth": auth, "taxi": taxi}


def tool_map(ctx, scope="full"):
    return {t.name: t for t in build_tools(ctx, scope)}


def test_read_only_scope_cannot_create_datasets(app_ctx):
    read_only = set(tool_map(app_ctx, "read_only"))
    full = set(tool_map(app_ctx, "full"))

    # The recursion guard: an agent-backed plugin can explore but never spawn jobs.
    assert "create_aggregate" not in read_only
    assert "create_join" not in read_only
    assert "apply_transform" not in read_only
    assert {"run_query", "profile_dataset", "get_suggestions"} <= read_only
    assert {"create_aggregate", "create_join", "save_dashboard"} <= full


def test_unknown_tool_is_reported_not_raised(app_ctx):
    agent = AnalysisAgent(app_ctx, scope="read_only")
    out = agent.call_tool("create_aggregate", {"dataset_id": "x", "plugin_id": "y"})
    assert "error" in out and "not-permitted" in out["error"]


def test_bad_arguments_are_reported_not_raised(app_ctx, loaded):
    agent = AnalysisAgent(app_ctx)
    out = agent.call_tool("profile_dataset", {"wrong_arg": 1})
    assert "error" in out and "bad arguments" in out["error"]


def test_list_and_profile(app_ctx, loaded):
    tools = tool_map(app_ctx)
    names = {d["name"] for d in tools["list_datasets"].handler()}
    assert names == {"auth", "taxi"}

    profile = tools["profile_dataset"].handler(dataset_id=loaded["auth"])
    types = {c["name"]: c["semantic_type"] for c in profile["columns"]}
    assert types["src_ip"] == "net.ip"
    assert types["country"] == "geo.country_iso2"
    assert profile["row_count"] == 600


def test_run_query_tool(app_ctx, loaded):
    tools = tool_map(app_ctx)
    out = tools["run_query"].handler(query={
        "dataset": loaded["auth"],
        "group_by": ["country"],
        "select": [{"column": "*", "agg": "count", "alias": "n"}],
        "order_by": [{"column": "n", "desc": True}],
        "limit": 3,
    })
    assert out["columns"] == ["country", "n"]
    assert out["rows"][0]["country"] == "US"
    assert isinstance(out["rows"][0]["n"], int)


def test_run_query_reports_errors_for_the_model_to_recover(app_ctx, loaded):
    tools = tool_map(app_ctx)
    out = tools["run_query"].handler(query={"dataset": loaded["auth"], "group_by": ["nope"]})
    # An error the model can read and retry from, not an exception that kills the turn.
    assert "error" in out and "unknown column" in out["error"]


def test_render_viz_tool(app_ctx, loaded):
    tools = tool_map(app_ctx)
    out = tools["render_viz"].handler(
        plugin_id="viz.map_points", dataset_id=loaded["taxi"],
        params={"lat_column": "pickup_latitude", "lng_column": "pickup_longitude"},
    )
    assert out["spec"]["renderer"] == "maplibre"
    assert out["row_count"] > 0


def test_agent_can_run_the_rarity_workflow_end_to_end(app_ctx, loaded):
    """The spec's cyber use case, driven entirely through agent tools."""
    tools = tool_map(app_ctx)

    started = tools["create_aggregate"].handler(
        dataset_id=loaded["auth"], plugin_id="agg.frequency",
        params={"column": "country"}, output_name="country_freq",
    )
    assert "job_id" in started
    app_ctx.runner.wait(started["job_id"], timeout=60)

    job = tools["get_job"].handler(job_id=started["job_id"])
    assert job["status"] == "succeeded", job
    freq_ds = job["outputs"][0]["dataset_id"]

    joined = tools["create_join"].handler(
        left_dataset_id=loaded["auth"], right_dataset_id=freq_ds,
        left_column="country", right_column="country", output_name="annotated",
    )
    app_ctx.runner.wait(joined["job_id"], timeout=60)
    join_job = tools["get_job"].handler(job_id=joined["job_id"])
    assert join_job["status"] == "succeeded", join_job
    annotated = join_job["outputs"][0]["dataset_id"]

    # Every event now carries how common its country is.
    out = tools["run_query"].handler(query={
        "dataset": annotated,
        "select": [{"column": "country"}, {"column": "share"}],
        "order_by": [{"column": "share", "desc": False}],
        "limit": 5,
    })
    assert out["row_count"] == 5
    shares = [r["share"] for r in out["rows"]]
    assert all(0 < s < 1 for s in shares)
    assert shares == sorted(shares)


def test_save_dashboard_from_rendered_viz(app_ctx, loaded):
    tools = tool_map(app_ctx)
    viz = tools["render_viz"].handler(
        plugin_id="viz.histogram", dataset_id=loaded["taxi"],
        params={"column": "fare_amount"},
    )
    saved = tools["save_dashboard"].handler(name="Agent report", panels=[viz["spec"]])
    assert saved["panels"] == 1
    assert app_ctx.catalog.list_dashboards()[0].name == "Agent report"


def test_tool_definitions_are_valid_schemas(app_ctx):
    for tool in build_tools(app_ctx):
        d = tool.definition()
        assert d["name"] and d["description"]
        assert d["input_schema"]["type"] == "object"
        # Strict-friendly: no free-form properties sneaking in.
        assert d["input_schema"]["additionalProperties"] is False
        for req in d["input_schema"]["required"]:
            assert req in d["input_schema"]["properties"]


def test_agent_without_api_key_fails_clearly(app_ctx):
    app_ctx.settings.anthropic_api_key = None
    agent = AnalysisAgent(app_ctx)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        list(agent.run("hello"))

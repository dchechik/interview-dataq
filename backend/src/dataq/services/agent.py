"""The analysis agent.

Tools bind to the *service layer*, not to HTTP, so the agent and the API cannot
drift apart -- they are two front ends over the same functions.

Two permission scopes exist:

  READ_ONLY  -- query, profile, suggest. Handed to agent-backed *plugins*, so a
                plugin can explore data but can never spawn jobs (no recursion).
  FULL       -- adds operations that create datasets. Used by the chat agent,
                which is driven by a human who can see and cancel what it starts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..core.viz import VizSpec
from ..plugins.base import REGISTRY
from ..query.spec import QuerySpec
from . import inspect as inspect_service
from .context import AppContext
from .model import PRICE_IN_PER_MTOK, PRICE_OUT_PER_MTOK
from .operations import OperationRequest, submit_operation
from .query import rows_as_dicts, run_query

Scope = Literal["read_only", "full"]

# Matches the max_tokens the loop requests, used only for the cost ceiling shown
# to the user before they start a run.
MAX_OUTPUT_TOKENS_PER_TURN = 8000

SYSTEM_PROMPT = """\
You are the analysis agent inside DataQ, a tool for exploring datasets.

Work by calling tools. Never guess at data: run a query and read the result.

A normal investigation looks like:
  1. list_datasets, then profile_dataset to learn what the columns *mean*
     (semantic types like net.ip or geo.lat, not just VARCHAR).
  2. get_suggestions to see what the tool already knows is worth doing.
  3. run_query to answer the actual question.
  4. render_viz to produce a chart, and save_dashboard to keep it.

Notes that matter:
- Column semantic types drive everything. A frequency aggregate joined back onto
  its source is how you annotate rows with how common their values are.
- create_aggregate and create_join start background jobs and return a job id.
  Poll get_job until it leaves 'running'; the new dataset id is on its step.
- Prefer a QuerySpec over raw SQL. It is validated against the schema.
- Be concrete and brief. Report numbers you actually retrieved, and say plainly
  when a result is empty or surprising rather than smoothing over it."""


@dataclass
class AgentTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    scope: Scope = "read_only"

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def build_tools(ctx: AppContext, scope: Scope = "full") -> list[AgentTool]:
    """The agent's tool surface. Every handler is a thin call into a service."""

    def list_datasets() -> list[dict]:
        return [
            {"id": d.id, "name": d.name, "kind": d.kind, "rows": v.row_count if v else 0,
             "latest_version": d.latest_version, "description": d.description}
            for d in ctx.catalog.list_datasets()
            for v in [ctx.catalog.get_version(d.id)]
        ]

    def profile_dataset(dataset_id: str) -> dict:
        profile = ctx.catalog.get_profile(dataset_id)
        if profile is None:
            return {"error": f"unknown dataset: {dataset_id}"}
        return {
            "dataset_id": profile.dataset_id,
            "version": profile.version,
            "row_count": profile.row_count,
            "columns": [
                {"name": c.name, "physical_type": c.physical_type,
                 "semantic_type": c.semantic_type, "role": c.role,
                 "distinct": c.stats.distinct_count if c.stats else None,
                 "examples": (c.stats.sample_values[:5] if c.stats else [])}
                for c in profile.columns
            ],
        }

    def run_query_tool(query: dict) -> dict:
        try:
            result = run_query(ctx, QuerySpec.model_validate(query))
        except Exception as exc:  # noqa: BLE001 - the agent should see and retry
            return {"error": f"{type(exc).__name__}: {exc}"}
        # Cap rows so a wide result cannot blow out the context window.
        rows = rows_as_dicts(result)[:100]
        return {"columns": result.columns, "rows": rows, "row_count": result.row_count,
                "truncated_for_agent": result.row_count > len(rows), "sql": result.sql}

    def get_suggestions(dataset_id: str, kind: str | None = None) -> list[dict]:
        try:
            found = inspect_service.suggest(ctx, dataset_id, kinds=(kind,) if kind else ())
        except KeyError as exc:
            return [{"error": str(exc)}]
        return [
            {"title": s.title, "rationale": s.rationale, "kind": s.kind,
             "score": s.score, "action": s.action}
            for s in found[:15]
        ]

    def list_plugins(dataset_id: str | None = None) -> list[dict]:
        descriptors = (
            inspect_service.applicable_plugins(ctx, dataset_id)
            if dataset_id
            else [p.descriptor() for p in REGISTRY.list()]
        )
        return [
            {"id": d.id, "kind": d.kind, "mode": d.mode, "title": d.title,
             "summary": d.summary, "cost_class": d.cost_class,
             "params_schema": d.params_schema}
            for d in descriptors
        ]

    def render_viz(plugin_id: str, dataset_id: str, params: dict | None = None) -> dict:
        try:
            out = inspect_service.render_viz(ctx, plugin_id, dataset_id, params or {}, limit=200)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        # Return the spec and a small sample; the full data is for the browser.
        return {"spec": out.spec.model_dump(), "row_count": out.row_count,
                "sample": out.data[:5]}

    def get_job(job_id: str) -> dict:
        job = ctx.catalog.get_job(job_id)
        if job is None:
            return {"error": f"unknown job: {job_id}"}
        steps = ctx.catalog.list_steps(job_id)
        return {
            "id": job.id, "status": job.status, "progress": job.progress,
            "error": job.error,
            "outputs": [o for s in steps for o in s.outputs],
            "logs": job.logs[-5:],
        }

    def _submit(req: OperationRequest) -> dict:
        try:
            accepted = submit_operation(ctx, req)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"job_id": accepted.job_id, "step_id": accepted.step_id,
                "note": "poll get_job until status leaves 'running'"}

    def create_aggregate(dataset_id: str, plugin_id: str, params: dict | None = None,
                         output_name: str = "") -> dict:
        return _submit(OperationRequest(
            op="aggregate", plugin_id=plugin_id,
            inputs=[{"dataset_id": dataset_id}], params=params or {},
            output_name=output_name,
        ))

    def create_join(left_dataset_id: str, right_dataset_id: str, left_column: str,
                    right_column: str, how: str = "left", output_name: str = "") -> dict:
        return _submit(OperationRequest(
            op="join",
            inputs=[{"dataset_id": left_dataset_id}, {"dataset_id": right_dataset_id}],
            params={"left_column": left_column, "right_column": right_column, "how": how},
            output_name=output_name,
        ))

    def apply_transform(dataset_id: str, plugin_id: str, params: dict | None = None,
                        max_cost_usd: float | None = None) -> dict:
        return _submit(OperationRequest(
            op="transform", plugin_id=plugin_id,
            inputs=[{"dataset_id": dataset_id}], params=params or {},
            max_cost_usd=max_cost_usd,
        ))

    def save_dashboard(name: str, panels: list[dict], description: str = "") -> dict:
        specs = [VizSpec.model_validate(p).model_dump() for p in panels]
        d = ctx.catalog.save_dashboard(name=name, panels=specs, description=description)
        return {"id": d.id, "name": d.name, "panels": len(specs)}

    tools = [
        AgentTool(
            "list_datasets",
            "List every dataset in the catalog with its row count and kind.",
            _obj({}), lambda: list_datasets(),
        ),
        AgentTool(
            "profile_dataset",
            "Get a dataset's columns with their semantic types, roles and example "
            "values. Call this before querying so you know what the columns mean.",
            _obj({"dataset_id": {"type": "string"}}, ["dataset_id"]),
            profile_dataset,
        ),
        AgentTool(
            "run_query",
            "Run a structured query. The 'query' object is a QuerySpec: "
            "{dataset, filters:[{column,op,value}], time_bucket:{column,interval}, "
            "select:[{column,agg,alias}], group_by:[], order_by:[{column,desc}], limit}. "
            "Aggregates: count, count_distinct, sum, avg, min, max, median. "
            "Use column='*' with agg='count' to count rows.",
            _obj({"query": {"type": "object"}}, ["query"]),
            run_query_tool,
        ),
        AgentTool(
            "get_suggestions",
            "Get the tool's own suggestions for a dataset (charts, aggregates, joins). "
            "Each carries an executable action. Optional kind: viz|aggregate|join.",
            _obj({"dataset_id": {"type": "string"}, "kind": {"type": "string"}},
                 ["dataset_id"]),
            get_suggestions,
        ),
        AgentTool(
            "list_plugins",
            "List available plugins and their parameter schemas. Pass dataset_id to "
            "get only those applicable to that dataset.",
            _obj({"dataset_id": {"type": "string"}}),
            list_plugins,
        ),
        AgentTool(
            "render_viz",
            "Build a chart from a visualizer plugin (viz.histogram, viz.bar, "
            "viz.timeseries, viz.map_points, viz.table). Returns a VizSpec you can "
            "pass to save_dashboard.",
            _obj({"plugin_id": {"type": "string"}, "dataset_id": {"type": "string"},
                  "params": {"type": "object"}}, ["plugin_id", "dataset_id"]),
            render_viz,
        ),
        AgentTool(
            "get_job",
            "Check a background job's status and its output dataset ids.",
            _obj({"job_id": {"type": "string"}}, ["job_id"]),
            get_job,
        ),
    ]

    if scope == "full":
        tools += [
            AgentTool(
                "create_aggregate",
                "Create an aggregate dataset (agg.frequency, agg.time_rollup, "
                "agg.topk). Starts a job; poll get_job for the new dataset id.",
                _obj({"dataset_id": {"type": "string"}, "plugin_id": {"type": "string"},
                      "params": {"type": "object"}, "output_name": {"type": "string"}},
                     ["dataset_id", "plugin_id"]),
                create_aggregate, scope="full",
            ),
            AgentTool(
                "create_join",
                "Join two datasets on a column each, producing a new dataset. Use this "
                "to annotate rows with values from an aggregate.",
                _obj({"left_dataset_id": {"type": "string"},
                      "right_dataset_id": {"type": "string"},
                      "left_column": {"type": "string"}, "right_column": {"type": "string"},
                      "how": {"type": "string", "enum": ["left", "inner"]},
                      "output_name": {"type": "string"}},
                     ["left_dataset_id", "right_dataset_id", "left_column", "right_column"]),
                create_join, scope="full",
            ),
            AgentTool(
                "apply_transform",
                "Apply a transform plugin, producing a new version of the dataset. "
                "Set max_cost_usd for plugins whose cost_class is 'expensive'.",
                _obj({"dataset_id": {"type": "string"}, "plugin_id": {"type": "string"},
                      "params": {"type": "object"},
                      "max_cost_usd": {"type": "number"}},
                     ["dataset_id", "plugin_id"]),
                apply_transform, scope="full",
            ),
            AgentTool(
                "save_dashboard",
                "Save VizSpecs (from render_viz) as a named dashboard.",
                _obj({"name": {"type": "string"},
                      "panels": {"type": "array", "items": {"type": "object"}},
                      "description": {"type": "string"}},
                     ["name", "panels"]),
                save_dashboard, scope="full",
            ),
        ]
    return tools


@dataclass
class AgentTurn:
    """One step of the loop, streamed to the UI so the user sees the work."""

    type: Literal["text", "tool_use", "tool_result", "error", "done"]
    text: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None


class AnalysisAgent:
    """A tool-use loop over the service layer.

    The loop is written out rather than using the SDK's tool runner because each
    step is streamed to the UI as a structured event, and tool dispatch is gated
    on the caller's scope.
    """

    def __init__(self, ctx: AppContext, scope: Scope = "full", max_turns: int = 12) -> None:
        self.ctx = ctx
        self.tools = {t.name: t for t in build_tools(ctx, scope)}
        self.max_turns = max_turns

    def _client(self):
        from anthropic import Anthropic

        if not self.ctx.settings.anthropic_api_key:
            raise RuntimeError(
                "no ANTHROPIC_API_KEY configured; the analysis agent needs one"
            )
        return Anthropic(api_key=self.ctx.settings.anthropic_api_key)

    def estimate(self, message: str, history: list[dict] | None = None) -> dict:
        """What one turn will cost, before anything is spent.

        Counts the *first* request only — the system prompt, the tool schemas and
        the conversation. An agent loop makes several such requests and each one
        resends the growing history, so the run total is a multiple of this; the
        projection below is deliberately labelled as a rough upper bound rather
        than a promise.
        """
        system_tokens = tool_tokens = message_tokens = 0
        exact = False

        if self.ctx.settings.anthropic_api_key:
            try:
                client = self._client()
                counted = client.messages.count_tokens(
                    model=self.ctx.settings.model,
                    system=[{"type": "text", "text": SYSTEM_PROMPT}],
                    tools=[t.definition() for t in self.tools.values()],
                    messages=[*(history or []), {"role": "user", "content": message}],
                )
                total = counted.input_tokens
                exact = True
            except Exception:  # noqa: BLE001 - fall back to the estimate below
                exact = False

        if not exact:
            # ~4 characters per token is close enough to warn on the right order
            # of magnitude when the API cannot be reached.
            def rough(text: str) -> int:
                return max(1, len(text) // 4)

            system_tokens = rough(SYSTEM_PROMPT)
            tool_tokens = sum(
                rough(json.dumps(t.definition())) for t in self.tools.values()
            )
            message_tokens = rough(message) + sum(
                rough(json.dumps(h)) for h in (history or [])
            )
            total = system_tokens + tool_tokens + message_tokens

        in_cost = total / 1e6 * PRICE_IN_PER_MTOK
        # A loop that actually uses its tools resends history each turn, so the
        # worst case grows super-linearly. Quote the ceiling, not the floor.
        worst_case_in = in_cost * self.max_turns * 1.5
        worst_case_out = MAX_OUTPUT_TOKENS_PER_TURN * self.max_turns / 1e6 * PRICE_OUT_PER_MTOK

        return {
            "input_tokens": total,
            "exact": exact,
            "model": self.ctx.settings.model,
            "tools": len(self.tools),
            "max_turns": self.max_turns,
            "first_request_usd": round(in_cost, 4),
            "worst_case_usd": round(worst_case_in + worst_case_out, 2),
            "has_api_key": bool(self.ctx.settings.anthropic_api_key),
        }

    def call_tool(self, name: str, payload: dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"unknown or not-permitted tool: {name}"}
        try:
            return tool.handler(**payload)
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}

    def run(self, message: str, history: list[dict] | None = None):
        """Yield ``AgentTurn``s until the model stops calling tools."""
        client = self._client()
        messages: list[dict[str, Any]] = [*(history or []), {"role": "user", "content": message}]
        definitions = [t.definition() for t in self.tools.values()]

        for _ in range(self.max_turns):
            response = client.messages.create(
                model=self.ctx.settings.model,
                max_tokens=8000,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                thinking={"type": "adaptive"},
                tools=definitions,
                messages=messages,
            )

            if response.stop_reason == "refusal":
                yield AgentTurn(type="error", text="The model declined this request.")
                return

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    yield AgentTurn(type="text", text=block.text)

            if response.stop_reason != "tool_use":
                yield AgentTurn(type="done")
                return

            # Echo content back unchanged so thinking blocks survive the round trip.
            messages.append({"role": "assistant", "content": response.content})

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                payload = dict(block.input) if isinstance(block.input, dict) else {}
                yield AgentTurn(type="tool_use", tool_name=block.name, tool_input=payload)
                output = self.call_tool(block.name, payload)
                yield AgentTurn(type="tool_result", tool_name=block.name, tool_result=output)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(output, default=str)[:20_000],
                    "is_error": isinstance(output, dict) and "error" in output,
                })
            # All tool_results for one assistant turn go in a single user message.
            messages.append({"role": "user", "content": results})

        yield AgentTurn(type="error", text=f"stopped after {self.max_turns} turns")

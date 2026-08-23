"""Core enums and value objects shared across every layer."""

from __future__ import annotations

from typing import Literal

# What a plugin consumes and produces -> determines its Python interface.
PluginKind = Literal["reader", "detector", "transform", "aggregator", "suggester", "visualizer"]

# How the runtime must execute a plugin -> determines scheduling and facilities.
#   pushdown : one DuckDB SQL statement
#   batch    : streamed Arrow batches in Python, part-file checkpointed
#   external : network/LLM calls; async pool, retries, result cache, cost accounting
#   inspect  : synchronous, read-only, returns a document (never creates a job)
ExecMode = Literal["pushdown", "batch", "external", "inspect"]

DatasetKind = Literal["source", "derived", "aggregate", "join"]

# How a column is used when suggesting queries and charts.
ColumnRole = Literal["dimension", "measure", "time", "key", "geo", "ignore"]

JobStatus = Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"]

TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

OperationOp = Literal["import", "transform", "aggregate", "join"]

# Cheap ordering hint surfaced in the UI so users know what they are about to launch.
CostClass = Literal["free", "cheap", "expensive"]

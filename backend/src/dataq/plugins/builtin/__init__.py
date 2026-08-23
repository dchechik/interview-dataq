"""Importing this package registers every built-in plugin."""

from . import (  # noqa: F401
    aggregators,
    detectors,
    readers,
    suggesters,
    transforms,
    visualizers,
)

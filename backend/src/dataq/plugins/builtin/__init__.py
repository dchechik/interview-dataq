"""Importing this package registers every built-in plugin."""

from . import (  # noqa: F401
    aggregators,
    detectors,
    features,
    readers,
    suggesters,
    timeline,
    transforms,
    visualizers,
)

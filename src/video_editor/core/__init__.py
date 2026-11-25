"""Pure domain core: timeline model, commands, history.

Standard library only — no Qt, no FFmpeg, no filesystem-dependent editing
logic. All times are exact ``fractions.Fraction`` seconds; all intervals are
half-open ``[start, end)``. See docs/architecture.md for invariants.
"""

from .commands import Command, CommandError
from .history import HistoryEntry, ProjectSession
from .model import (
    Clip,
    OutputSettings,
    Project,
    StreamSource,
    Track,
    TrackKind,
    envelope_gain,
)

__all__ = [
    "Clip",
    "Command",
    "CommandError",
    "HistoryEntry",
    "OutputSettings",
    "Project",
    "ProjectSession",
    "StreamSource",
    "Track",
    "TrackKind",
    "envelope_gain",
]

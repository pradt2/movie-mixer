"""Pure import tests: the package must import without Qt widgets or media tools."""

import video_editor
from video_editor import diagnostics


def test_version_present():
    assert video_editor.__version__


def test_diagnostics_never_raises():
    status = diagnostics.check_media_tools()
    assert isinstance(status.diagnostic(), str)

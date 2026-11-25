"""Filesystem safety policy, shared by every layer that touches media files.

Two concerns live here, and nothing else:

* :mod:`~video_editor.fs.identity` — is the file at a recorded path still the
  file that was recorded (``ONLINE`` / ``MISSING`` / ``CHANGED`` /
  ``UNREADABLE``)?
* :mod:`~video_editor.fs.protection` — may this path be written at all, and how
  is a finished temporary file published without clobbering anything?

This package is deliberately dependency-free (standard library only, no
``core``, no ``media``, no Qt) so that ``media``, ``render``, ``project`` and
``ui`` can all enforce the same rules instead of each inventing their own.
Asset-typed helpers — checking an :class:`~video_editor.media.assets.Asset`
record, for instance — live in :mod:`video_editor.media.availability` on top of
these primitives.
"""

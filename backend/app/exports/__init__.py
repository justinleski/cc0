"""Board exports: the owner-facing API and the worker-side packager.

The API half records a request and a pinned item snapshot; the worker half builds
the archive. They share only the `board_exports` tables and the job row. See
docs/systems/exports.md.
"""

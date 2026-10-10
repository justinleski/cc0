import os

import pytest

from app.core.config import _is_project_config_name


@pytest.fixture(autouse=True)
def _isolate_project_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must not read the repo .env or ambient APP_ENV / DATABASE_URL.

    Settings still scans the process environment for project-prefixed typos, so
    drop those names here. Unrelated variables such as PATH stay in place.
    Callers pass ``_env_file=None`` so the repository ``.env`` is not opened.
    """
    for key in list(os.environ):
        if _is_project_config_name(key):
            monkeypatch.delenv(key, raising=False)

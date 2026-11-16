"""Single source of truth for the release version: the repo-root VERSION file.

Resolution order: MQ_APP_VERSION env override, then the VERSION file (Docker
image layout, repo checkout, or working directory). The file is read once and
cached; a missing file falls back to the pre-versioning release number.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

_FALLBACK_VERSION = "1.0.0"


def _read_version_file() -> str | None:
    here = Path(__file__).resolve()
    candidates = [
        Path("/app/VERSION"),
        here.parent.parent.parent.parent / "VERSION",
        Path.cwd() / "VERSION",
    ]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text and "\n" not in text:
            token = text.split()[0]
            if token:
                return token
    return None


@functools.lru_cache(maxsize=1)
def get_app_version() -> str:
    override = os.environ.get("MQ_APP_VERSION", "").strip()
    if override:
        return override.split()[0]
    return _read_version_file() or _FALLBACK_VERSION

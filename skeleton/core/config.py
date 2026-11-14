"""Settings loaded from environment variables and an optional ``.env`` file.

Real environment variables win over values in ``.env``. The API key is kept out
of ``repr()``/``str()`` and out of ``public_dict()`` so no endpoint can leak it
(Req 10.2).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_HEARTBEAT_SECONDS = 300
DEFAULT_DAILY_LIMIT_IDR = 100_000
DEFAULT_DB_PATH = "kantor.db"

_SECRET_FIELDS = frozenset({"llm_api_key"})


@dataclass(frozen=True)
class Settings:
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    model_low: str = ""
    model_strong: str = ""
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS
    default_daily_limit_idr: int = DEFAULT_DAILY_LIMIT_IDR
    db_path: str = DEFAULT_DB_PATH

    def __post_init__(self) -> None:
        if isinstance(self.heartbeat_seconds, bool) or self.heartbeat_seconds < 1:
            raise ValueError(
                f"heartbeat_seconds must be an integer >= 1, got {self.heartbeat_seconds!r}"
            )
        if isinstance(self.default_daily_limit_idr, bool) or self.default_daily_limit_idr < 0:
            raise ValueError(
                "default_daily_limit_idr must be an integer >= 0, "
                f"got {self.default_daily_limit_idr!r}"
            )

    @property
    def demo_mode(self) -> bool:
        """True when no API key is set, so the app uses the fake LLM client."""
        return not self.llm_api_key.strip()

    def public_dict(self) -> dict[str, Any]:
        """Settings safe to return from an endpoint: never includes the API key."""
        data = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in _SECRET_FIELDS}
        data["demo_mode"] = self.demo_mode
        return data


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse simple ``KEY=VALUE`` lines. Comments and blank lines are ignored."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        return parse_dotenv(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _int_value(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def load_settings(
    env: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> Settings:
    """Build Settings from ``env`` (default ``os.environ``) and a ``.env`` file.

    ``dotenv_path`` defaults to ``.env`` in the current working directory; a
    missing file is ignored. Keys present in ``env`` override the file.
    """
    path = Path(dotenv_path) if dotenv_path is not None else Path.cwd() / ".env"
    values: dict[str, str] = _read_dotenv(path)
    values.update(os.environ if env is None else env)

    def text(name: str, default: str = "") -> str:
        return values.get(name, default).strip()

    return Settings(
        llm_base_url=text("KANTOR_LLM_BASE_URL"),
        llm_api_key=text("KANTOR_LLM_API_KEY"),
        model_low=text("KANTOR_MODEL_LOW"),
        model_strong=text("KANTOR_MODEL_STRONG"),
        heartbeat_seconds=_int_value(values, "KANTOR_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS),
        default_daily_limit_idr=_int_value(
            values, "KANTOR_DEFAULT_DAILY_LIMIT_IDR", DEFAULT_DAILY_LIMIT_IDR
        ),
        db_path=text("KANTOR_DB_PATH") or DEFAULT_DB_PATH,
    )

import dataclasses

import pytest

from skeleton.core.config import Settings, load_settings, parse_dotenv

SECRET = "sk-super-secret-123"


def _load(tmp_path, env=None, dotenv=None):
    path = tmp_path / ".env"
    if dotenv is not None:
        path.write_text(dotenv, encoding="utf-8")
    return load_settings(env=env or {}, dotenv_path=path)


def test_defaults(tmp_path):
    s = _load(tmp_path)
    assert s.llm_base_url == ""
    assert s.model_low == ""
    assert s.model_strong == ""
    assert s.heartbeat_seconds == 300
    assert s.default_daily_limit_idr == 100000
    assert s.db_path == "kantor.db"
    assert s.demo_mode is True


def test_env_override(tmp_path):
    s = _load(
        tmp_path,
        env={
            "KANTOR_LLM_BASE_URL": "https://llm.example/v1",
            "KANTOR_LLM_API_KEY": SECRET,
            "KANTOR_MODEL_LOW": "small",
            "KANTOR_MODEL_STRONG": "big",
            "KANTOR_HEARTBEAT_SECONDS": "60",
            "KANTOR_DEFAULT_DAILY_LIMIT_IDR": "250000",
            "KANTOR_DB_PATH": "/tmp/x.db",
        },
    )
    assert s.llm_base_url == "https://llm.example/v1"
    assert s.llm_api_key == SECRET
    assert (s.model_low, s.model_strong) == ("small", "big")
    assert s.heartbeat_seconds == 60
    assert s.default_daily_limit_idr == 250000
    assert s.db_path == "/tmp/x.db"
    assert s.demo_mode is False


def test_dotenv_parsing(tmp_path):
    dotenv = (
        "# comment\n"
        "\n"
        "KANTOR_LLM_BASE_URL=https://a.example/v1\n"
        "export KANTOR_MODEL_LOW = \"low-model\"\n"
        "KANTOR_MODEL_STRONG='strong-model'\n"
        "not a valid line\n"
        f"KANTOR_LLM_API_KEY={SECRET}\n"
        "KANTOR_HEARTBEAT_SECONDS=15\n"
    )
    s = _load(tmp_path, dotenv=dotenv)
    assert s.llm_base_url == "https://a.example/v1"
    assert s.model_low == "low-model"
    assert s.model_strong == "strong-model"
    assert s.llm_api_key == SECRET
    assert s.heartbeat_seconds == 15
    assert s.demo_mode is False


def test_parse_dotenv_ignores_comments_and_blanks():
    assert parse_dotenv("# x\n\n  \nA=1\n  # y\nB=2=3\n") == {"A": "1", "B": "2=3"}


def test_dotenv_read_from_working_directory(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("KANTOR_MODEL_LOW=from-cwd\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_settings(env={}).model_low == "from-cwd"


def test_env_beats_dotenv(tmp_path):
    s = _load(
        tmp_path,
        env={"KANTOR_MODEL_LOW": "from-env"},
        dotenv="KANTOR_MODEL_LOW=from-file\nKANTOR_MODEL_STRONG=file-strong\n",
    )
    assert s.model_low == "from-env"
    assert s.model_strong == "file-strong"


def test_uses_os_environ_when_env_not_given(tmp_path, monkeypatch):
    monkeypatch.setenv("KANTOR_MODEL_STRONG", "os-strong")
    assert load_settings(dotenv_path=tmp_path / "missing.env").model_strong == "os-strong"


def test_api_key_hidden_from_repr_str_and_public_dict(tmp_path):
    s = _load(tmp_path, env={"KANTOR_LLM_API_KEY": SECRET, "KANTOR_MODEL_LOW": "small"})
    assert SECRET not in repr(s)
    assert SECRET not in str(s)
    public = s.public_dict()
    assert "llm_api_key" not in public
    assert SECRET not in str(public)
    assert public["model_low"] == "small"
    assert public["demo_mode"] is False


def test_blank_key_means_demo_mode(tmp_path):
    assert _load(tmp_path, env={"KANTOR_LLM_API_KEY": "   "}).demo_mode is True


def test_settings_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Settings().heartbeat_seconds = 5  # type: ignore[misc]


@pytest.mark.parametrize("value", ["0", "-5"])
def test_invalid_heartbeat_rejected(tmp_path, value):
    with pytest.raises(ValueError, match="heartbeat_seconds"):
        _load(tmp_path, env={"KANTOR_HEARTBEAT_SECONDS": value})


def test_negative_daily_limit_rejected(tmp_path):
    with pytest.raises(ValueError, match="default_daily_limit_idr"):
        _load(tmp_path, env={"KANTOR_DEFAULT_DAILY_LIMIT_IDR": "-1"})


def test_zero_daily_limit_allowed(tmp_path):
    assert _load(tmp_path, env={"KANTOR_DEFAULT_DAILY_LIMIT_IDR": "0"}).default_daily_limit_idr == 0


@pytest.mark.parametrize(
    "name", ["KANTOR_HEARTBEAT_SECONDS", "KANTOR_DEFAULT_DAILY_LIMIT_IDR"]
)
def test_non_integer_rejected(tmp_path, name):
    with pytest.raises(ValueError, match=name):
        _load(tmp_path, env={name: "abc"})


def test_direct_construction_is_validated():
    with pytest.raises(ValueError):
        Settings(heartbeat_seconds=0)
    with pytest.raises(ValueError):
        Settings(default_daily_limit_idr=-1)

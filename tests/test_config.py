from pathlib import Path

import pytest
from pydantic import ValidationError

from plangate.config import AppSettings, load_app_settings

ROOT_DIR = Path(__file__).parent.parent
SQLITE_DB_PATH = ROOT_DIR / "plangate.db"


@pytest.fixture(autouse=True)
def disable_dotenv(monkeypatch):
    """Disable .env for unit testing"""
    monkeypatch.setitem(AppSettings.model_config, "env_file", None)


def test_env_config(monkeypatch):
    monkeypatch.setenv("PLANGATE_SQLITE_DB_PATH", "mydb.db")
    monkeypatch.setenv("PLANGATE_TEMPORAL_ADDRESS", " temporal.test:7233 ")
    monkeypatch.setenv("PLANGATE_LLM_PROVIDER", "test-provider")
    monkeypatch.setenv("PLANGATE_LLM_MODEL", "test-model")
    monkeypatch.setenv("PLANGATE_LLM_TIMEOUT", "15")
    monkeypatch.setenv("PLANGATE_OLLAMA_URL", "http://testurl:12345")

    monkeypatch.setenv("PLANGATE_POSTGRES_HOST", "127.0.0.2")
    monkeypatch.setenv("PLANGATE_POSTGRES_PORT", "65535")
    monkeypatch.setenv("PLANGATE_POSTGRES_DB", "test-db")
    monkeypatch.setenv("PLANGATE_POSTGRES_USER", "test-user")
    monkeypatch.setenv("PLANGATE_POSTGRES_PASSWORD", "test-password")

    settings = load_app_settings()

    assert settings.sqlite_db_path == Path("mydb.db")
    assert settings.temporal_address == "temporal.test:7233"
    assert settings.llm_provider == "test-provider"
    assert settings.llm_model == "test-model"
    assert settings.llm_timeout == 15
    assert str(settings.ollama_url) == "http://testurl:12345/"
    assert settings.postgres_host == "127.0.0.2"
    assert settings.postgres_port == 65535
    assert settings.postgres_db == "test-db"
    assert settings.postgres_user == "test-user"
    assert settings.postgres_password is not None
    assert settings.postgres_password.get_secret_value() == "test-password"


def test_sqlite_db_path_variable_unset(monkeypatch):
    monkeypatch.delenv("PLANGATE_SQLITE_DB_PATH", raising=False)

    settings = load_app_settings()
    assert settings.sqlite_db_path == SQLITE_DB_PATH


@pytest.mark.parametrize("path", ["", " "])
def test_empty_sqlite_db_path_variable(monkeypatch, path):
    monkeypatch.setenv("PLANGATE_SQLITE_DB_PATH", path)

    with pytest.raises(ValueError):
        load_app_settings()


def test_unset_host_variable(monkeypatch):
    monkeypatch.delenv("PLANGATE_HOST", raising=False)

    settings = load_app_settings()
    assert settings.host == "127.0.0.1"


def test_whitespace_host(monkeypatch):
    monkeypatch.setenv("PLANGATE_HOST", " 0.0.0.0 ")

    settings = load_app_settings()
    assert settings.host == "0.0.0.0"


@pytest.mark.parametrize(
    "variable",
    [
        "PLANGATE_HOST",
        "PLANGATE_POSTGRES_HOST",
        "PLANGATE_POSTGRES_DB",
        "PLANGATE_POSTGRES_USER",
    ],
)
@pytest.mark.parametrize("value", ["", " "])
def test_blank_config_names(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValueError):
        load_app_settings()


def test_unset_temporal_address(monkeypatch):
    monkeypatch.delenv("PLANGATE_TEMPORAL_ADDRESS", raising=False)

    assert load_app_settings().temporal_address == "localhost:7233"


@pytest.mark.parametrize(
    "address, failure_reason",
    [
        ("", "PLANGATE_TEMPORAL_ADDRESS must be a valid host:port"),
        (" ", "PLANGATE_TEMPORAL_ADDRESS must be a valid host:port"),
        ("host:abc", "Temporal port must be a number"),
        ("host:70000", "Temporal port must be between 1 and 65535"),
    ],
)
def test_invalid_temporal_address(monkeypatch, address, failure_reason):
    monkeypatch.setenv("PLANGATE_TEMPORAL_ADDRESS", address)

    with pytest.raises(ValueError, match=failure_reason):
        load_app_settings()


@pytest.mark.parametrize("timeout", ["0", "-1", "abc"])
def test_invalid_llm_timeout(monkeypatch, timeout):
    monkeypatch.setenv("PLANGATE_LLM_TIMEOUT", timeout)
    with pytest.raises(ValueError):
        load_app_settings()


@pytest.mark.parametrize("url", ["ftp://localhost:11434", "not-a-url"])
def test_invalid_ollama_url(monkeypatch, url):
    monkeypatch.setenv("PLANGATE_OLLAMA_URL", url)
    with pytest.raises(ValueError):
        load_app_settings()


@pytest.mark.parametrize("port", ["0", "-1", "65536", "abc"])
def test_invalid_postgres_port(monkeypatch, port):
    monkeypatch.setenv("PLANGATE_POSTGRES_PORT", port)
    with pytest.raises(ValidationError):
        load_app_settings()


def test_optional_postgres_password(monkeypatch):
    monkeypatch.setenv("PLANGATE_REVIEW_STORE_BACKEND", "sqlite")
    monkeypatch.delenv("PLANGATE_POSTGRES_PASSWORD", raising=False)
    settings = load_app_settings()

    assert settings.postgres_password is None


@pytest.mark.parametrize("password", [None, ""])
def test_postgres_requires_password(monkeypatch, password):
    monkeypatch.setenv("PLANGATE_REVIEW_STORE_BACKEND", "postgres")

    if password is None:
        monkeypatch.delenv("PLANGATE_POSTGRES_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("PLANGATE_POSTGRES_PASSWORD", password)

    with pytest.raises(ValidationError, match="requires a password"):
        load_app_settings()


def test_postgres_password(monkeypatch):
    monkeypatch.setenv("PLANGATE_REVIEW_STORE_BACKEND", "postgres")
    monkeypatch.setenv("PLANGATE_POSTGRES_PASSWORD", "a% !9>@")
    settings = load_app_settings()

    assert settings.postgres_password is not None
    assert settings.postgres_password.get_secret_value() == "a% !9>@"


def test_default_review_store(monkeypatch):
    monkeypatch.delenv("PLANGATE_REVIEW_STORE_BACKEND", raising=False)
    settings = load_app_settings()

    assert settings.review_store_backend == "sqlite"


def test_unsupported_store_backend(monkeypatch):
    monkeypatch.setenv("PLANGATE_REVIEW_STORE_BACKEND", "mysql")

    with pytest.raises(ValueError):
        load_app_settings()

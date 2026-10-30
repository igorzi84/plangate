import tomllib
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import AnyUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from plangate.domain.models import PolicyConfig

ROOT_DIR = Path(__file__).parent.parent.parent


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PLANGATE_",
        str_strip_whitespace=True,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    db_path: Path = Field(default=ROOT_DIR / "plangate.db")
    host: str = Field(default="127.0.0.1", min_length=1)
    temporal_address: str = Field(default="localhost:7233", min_length=1)

    llm_provider: str = Field(default="deterministic", min_length=1)
    llm_model: str = Field(default="qwen3:4b", min_length=1)
    llm_timeout: int = Field(default=60, gt=0)

    ollama_url: AnyUrl = Field(default_factory=lambda: AnyUrl("http://localhost:11434"))

    postgres_host: str = Field(default="127.0.0.1", min_length=1)
    postgres_port: int = Field(default=5432, gt=0, le=65535)
    postgres_db: str = Field(default="plangate", min_length=1)
    postgres_user: str = Field(default="plangate", min_length=1)
    postgres_password: SecretStr | None = None

    review_store_backend: Literal["sqlite", "postgres"] = "sqlite"

    @field_validator("db_path", mode="before")
    @classmethod
    def validate_path(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("PLANGATE_DB_PATH must not be blank")
        return value

    @field_validator("temporal_address", mode="before")
    @classmethod
    def validate_temporal_address(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise TypeError("PLANGATE_TEMPORAL_ADDRESS must be a valid host:port")

        host, separator, port = value.rpartition(":")
        if not host or not separator or not port:
            raise ValueError("PLANGATE_TEMPORAL_ADDRESS must be a valid host:port")

        try:
            port_number = int(port)
        except ValueError as error:
            raise ValueError("Temporal port must be a number") from error

        if not 1 <= port_number <= 65535:
            raise ValueError("Temporal port must be between 1 and 65535")

        return value

    @field_validator("ollama_url", mode="after")
    @classmethod
    def require_http(cls, value: AnyUrl) -> AnyUrl:
        if value.scheme not in {"http", "https"}:
            raise ValueError("Ollama URL must use http or https")
        return value

    @model_validator(mode="after")
    def require_postgres_password(self) -> Self:
        if self.review_store_backend == "postgres" and (
            self.postgres_password is None
            or not self.postgres_password.get_secret_value()
        ):
            raise ValueError("PostgreSQL backend requires a password")

        return self


def load_app_settings() -> AppSettings:
    return AppSettings()


def load_policy_config(config_path: Path) -> PolicyConfig:
    with open(config_path, "rb") as f:
        config = tomllib.load(f)
        if "enabled_policy_ids" not in config:
            raise ValueError("enabled_policy_ids is required")

        policy_ids = config["enabled_policy_ids"]

        if not isinstance(policy_ids, list):
            raise TypeError("enabled_policy_ids must be a list of strings")

        if not all(isinstance(policy_id, str) for policy_id in policy_ids):
            raise TypeError("enabled_policy_ids must be a list of strings")

        ids = frozenset(policy_ids)

        tags = PolicyConfig().required_tags

        if "required_tags" in config:
            required_tags = config["required_tags"]

            if not isinstance(required_tags, list):
                raise TypeError("required_tags must be a list of strings")

            if not all(isinstance(tag, str) for tag in required_tags):
                raise TypeError("required_tags must be a list of strings")

            tags = frozenset(required_tags)

        return PolicyConfig(enabled_policy_ids=ids, required_tags=tags)

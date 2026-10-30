import subprocess
from pathlib import Path

import psycopg
import pytest

from plangate.config import load_app_settings

ROOT_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def start_postgres():
    settings = load_app_settings()
    assert settings.postgres_password is not None

    subprocess.run(
        ["docker", "compose", "up", "-d", "--wait", "postgres"],
        check=True,
        cwd=ROOT_DIR,
    )

    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname="plangate",
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        autocommit=True,
    ) as conn:
        conn.execute("DROP DATABASE IF EXISTS plangate_test")
        conn.execute("CREATE DATABASE plangate_test")

    yield

    with psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname="plangate",
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        autocommit=True,
    ) as conn:
        conn.execute("DROP DATABASE IF EXISTS plangate_test")

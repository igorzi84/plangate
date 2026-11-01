from plangate.config import AppSettings
from plangate.stores.base import ReviewStore
from plangate.stores.postgres import PostgresReviewStore
from plangate.stores.sqlite import SQLiteReviewStore


def create_review_store(settings: AppSettings) -> ReviewStore:

    if settings.review_store_backend == "postgres":
        assert settings.postgres_password is not None
        return PostgresReviewStore(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
        )

    elif settings.review_store_backend == "sqlite":
        return SQLiteReviewStore(settings.sqlite_db_path)

    else:
        raise ValueError("Unsupported review store backend")

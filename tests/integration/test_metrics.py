import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.metrics import instrument_database


async def test_postgresql_observations_include_failed_statements(db_engine):
    instrument_database(db_engine.sync_engine)

    def count(outcome):
        return (
            REGISTRY.get_sample_value(
                "referral_db_query_duration_seconds_count", {"outcome": outcome}
            )
            or 0
        )

    success_before = count("success")
    error_before = count("error")
    async with db_engine.connect() as conn:
        assert await conn.scalar(text("SELECT 1")) == 1
        with pytest.raises(DBAPIError):
            await conn.execute(text("SELECT 1 / 0"))
        await conn.rollback()
        assert await conn.scalar(text("SELECT 2")) == 2

    assert count("success") == success_before + 2
    assert count("error") == error_before + 1

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.exceptions import InvalidRequestError
from app.repositories.db import create_engine, init_schema
from app.repositories.models import RunRow
from app.repositories.runtime import RuntimeStore
from app.tools.base import ToolContext


@pytest.mark.asyncio
async def test_runtime_run_trace_parent_and_artifact_ownership(tmp_path):
    engine = create_engine(
        Settings(
            app_env="testing",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/runtime.db",
        )
    )
    await init_schema(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    store = RuntimeStore(factory)
    root_context = ToolContext(user_id="u1", session_id="s1", trace_id="trace-root")
    try:
        await store.record({"run_id": "root", "status": "completed", "events": []}, root_context)
        await store.record(
            {"run_id": "policy", "status": "completed", "events": []},
            root_context,
            parent_run_id="root",
        )
        async with factory() as db:
            root = await db.get(RunRow, "root")
            child = await db.get(RunRow, "policy")
            assert root.trace_id == child.trace_id == "trace-root"
            assert root.parent_run_id is None and child.parent_run_id == "root"

        artifact_ref = await store.put({"chunk_id": "c1"}, root_context)
        assert await store.read(artifact_ref, root_context) == {"chunk_id": "c1"}
        with pytest.raises(InvalidRequestError, match="artifact not found"):
            await store.read(
                artifact_ref,
                ToolContext(user_id="u2", session_id="s1", trace_id="trace-other"),
            )
        with pytest.raises(InvalidRequestError, match="run not found"):
            await store.record(
                {"run_id": "root", "status": "failed", "events": []},
                ToolContext(user_id="u2", session_id="s2", trace_id="trace-other"),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_existing_sqlite_run_table_gets_additive_trace_column(tmp_path):
    engine = create_engine(
        Settings(
            app_env="testing",
            database_url=f"sqlite+aiosqlite:///{tmp_path}/old-runtime.db",
        )
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE harness_runs ("
                "run_id VARCHAR(64) PRIMARY KEY, parent_run_id VARCHAR(64), "
                "user_id VARCHAR(64), session_id VARCHAR(64), status VARCHAR(32), "
                "events_json TEXT)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO harness_runs "
                "(run_id, user_id, session_id, status, events_json) "
                "VALUES ('legacy', 'u1', 's1', 'completed', '[]')"
            )
        )
    try:
        await init_schema(engine)
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {
                    column["name"] for column in inspect(sync).get_columns("harness_runs")
                }
            )
        assert "trace_id" in columns
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        await RuntimeStore(factory).record(
            {"run_id": "legacy", "status": "completed", "events": []},
            ToolContext(user_id="u1", session_id="s1", trace_id="trace-upgraded"),
        )
        async with factory() as db:
            assert (await db.get(RunRow, "legacy")).trace_id == "trace-upgraded"
    finally:
        await engine.dispose()

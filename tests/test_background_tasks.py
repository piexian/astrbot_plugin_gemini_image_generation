from __future__ import annotations

import asyncio
import json

import pytest

from tl.background_tasks import BackgroundTaskManager


@pytest.mark.asyncio
async def test_task_records_are_session_scoped_and_persisted(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path, retention_hours=24)
    record = await manager.create(
        session_id="platform:friend:1",
        kind="single",
        routing_mode="model_polling",
        message="running",
    )
    await manager.update(record["task_id"], status="succeeded", succeeded_items=1)

    loaded = BackgroundTaskManager(tmp_path, retention_hours=24)
    result = await loaded.get(record["task_id"], "platform:friend:1")

    assert result is not None
    assert result["status"] == "succeeded"
    with pytest.raises(PermissionError):
        await loaded.get(record["task_id"], "platform:friend:2")


def test_restart_marks_running_tasks_interrupted(tmp_path) -> None:
    path = tmp_path / "background_tasks.json"
    path.write_text(
        json.dumps(
            {
                "tasks": {
                    "img-running": {
                        "task_id": "img-running",
                        "session_id": "session",
                        "status": "running",
                        "created_at": "2099-01-01T00:00:00+00:00",
                        "updated_at": "2099-01-01T00:00:00+00:00",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    manager = BackgroundTaskManager(tmp_path)

    assert manager._records["img-running"]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_close_cancels_attached_tasks_and_marks_interrupted(tmp_path) -> None:
    manager = BackgroundTaskManager(tmp_path)
    record = await manager.create(
        session_id="session",
        kind="batch",
        routing_mode="full_polling",
        message="running",
    )

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    manager.attach(record["task_id"], wait_forever())
    await manager.close()

    assert manager._records[record["task_id"]]["status"] == "interrupted"

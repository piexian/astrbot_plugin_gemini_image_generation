from __future__ import annotations

import asyncio
import threading

import pytest

from tests.test_web_studio_service import _config, _png, _SequenceClient
from tl.generation_tracker import GenerationTracker, ReferenceImageUnavailableError
from tl.web_studio_service import StudioServiceError, WebStudioService


async def _source(tracker, name="source.png"):
    tracker.gallery_dir.mkdir(exist_ok=True)
    _png(tracker.gallery_dir / name)
    record = await tracker.begin(
        source="webui", prompt="source", params={}, requester={}
    )
    await tracker.complete(
        record["job_id"], image_files=[name], text_content=None, stats={}
    )
    return record


@pytest.mark.asyncio
async def test_running_references_survive_record_pruning_delete_and_quota(tmp_path):
    tracker = GenerationTracker(tmp_path, 1)
    source = await _source(tracker)
    derived = await tracker.begin(
        source="webui",
        prompt="edit",
        params={},
        requester={},
        reference_names=["source.png"],
    )
    assert tracker.get(source["job_id"]) is not None
    denied = await tracker.delete([source["job_id"]])
    assert denied["deleted"] == []
    assert "参考图" in denied["failed"][0]["error"]
    service = WebStudioService(
        None, tracker, _config(webui_gallery_max_size_mb=0.000001), tmp_path
    )
    await service.enforce_gallery_quota()
    assert (tracker.gallery_dir / "source.png").is_file()
    _png(tracker.gallery_dir / "result.png")
    await tracker.complete(
        derived["job_id"], image_files=["result.png"], text_content=None, stats={}
    )
    assert tracker.get(source["job_id"]) is None
    assert not (tracker.gallery_dir / "source.png").exists()
    assert tracker.get(derived["job_id"])["reference_names"] == ["source.png"]
    assert tracker.protected_reference_names() == set()
    await tracker.close()


@pytest.mark.asyncio
async def test_deleting_derived_job_only_removes_its_outputs(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    await _source(tracker)
    derived = await tracker.begin(
        source="webui",
        prompt="edit",
        params={},
        requester={},
        reference_names=["source.png"],
    )
    _png(tracker.gallery_dir / "result.png")
    await tracker.complete(
        derived["job_id"], image_files=["result.png"], text_content=None, stats={}
    )
    await tracker.delete([derived["job_id"]])
    assert (tracker.gallery_dir / "source.png").is_file()
    assert not (tracker.gallery_dir / "result.png").exists()
    await tracker.close()


@pytest.mark.asyncio
async def test_quota_uses_current_reference_leases_with_an_old_snapshot(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    await _source(tracker)
    snapshot = tracker.records_snapshot()
    derived = await tracker.begin(
        source="webui",
        prompt="edit",
        params={},
        requester={},
        reference_names=["source.png"],
    )
    service = WebStudioService(None, tracker, _config(), tmp_path)
    assert not await asyncio.to_thread(service._enforce_gallery_quota_sync, 1, snapshot)
    assert (tracker.gallery_dir / "source.png").is_file()
    await tracker.fail(derived["job_id"], error="failed")
    assert await asyncio.to_thread(service._enforce_gallery_quota_sync, 1, snapshot)
    assert not (tracker.gallery_dir / "source.png").exists()
    await tracker.close()


@pytest.mark.asyncio
async def test_pending_file_deletion_cannot_admit_a_new_reference(
    tmp_path, monkeypatch
):
    tracker = GenerationTracker(tmp_path, 20)
    source = await _source(tracker)
    entered = threading.Event()
    release = threading.Event()
    original_delete = tracker._delete_gallery_files

    def delayed_delete(names):
        entered.set()
        assert release.wait(5)
        original_delete(names)

    monkeypatch.setattr(tracker, "_delete_gallery_files", delayed_delete)
    deletion = asyncio.create_task(tracker.delete([source["job_id"]]))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(ReferenceImageUnavailableError):
            await tracker.begin(
                source="webui",
                prompt="edit",
                params={},
                requester={},
                reference_names=["source.png"],
            )
    finally:
        release.set()
        await deletion
    assert tracker.protected_reference_names() == set()
    await tracker.close()


@pytest.mark.asyncio
async def test_reuse_restores_only_existing_references_and_explicit_empty_clears(
    tmp_path,
):
    tracker = GenerationTracker(tmp_path, 20)
    await _source(tracker, "kept.png")
    removed = await _source(tracker, "removed.png")
    derived = await tracker.begin(
        source="webui",
        prompt="edit",
        params={},
        requester={},
        reference_names=["kept.png", "removed.png"],
    )
    await tracker.complete(
        derived["job_id"], image_files=[], text_content=None, stats={}
    )
    await tracker.delete([removed["job_id"]])
    service = WebStudioService(None, tracker, _config(), tmp_path)
    reused, warning = service.validate_payload({"reuse_job_id": derived["job_id"]})
    assert reused["reference_names"] == ["kept.png"]
    assert warning == "1 张历史参考图已清理，已跳过"
    assert tracker.get(derived["job_id"])["reference_names"] == [
        "kept.png",
        "removed.png",
    ]
    cleared, warning = service.validate_payload(
        {"reuse_job_id": derived["job_id"], "reference_names": []}
    )
    assert cleared["reference_names"] == []
    assert warning is None
    with pytest.raises(StudioServiceError):
        service.validate_payload(
            {"reuse_job_id": derived["job_id"], "reference_names": ["removed.png"]}
        )
    await tracker.close()


@pytest.mark.asyncio
async def test_batch_persists_reference_relationships_for_parent_and_children(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    await _source(tracker)
    output = [_png(tmp_path / f"result-{index}.png", index + 1) for index in range(2)]
    client = _SequenceClient([([], [str(path)], None, None) for path in output])
    service = WebStudioService(client, tracker, _config(), tmp_path)
    accepted = await service.generate(
        {
            "batch": [
                {"name": "one", "prompt": "edit one"},
                {"name": "two", "prompt": "edit two"},
            ],
            "reference_names": ["source.png"],
        }
    )
    await service._runtime_tasks[accepted["job_id"]]
    for job_id in [accepted["job_id"], *accepted["item_job_ids"]]:
        assert tracker.get(job_id)["reference_names"] == ["source.png"]
    assert tracker.protected_reference_names() == set()
    await service.close()
    await tracker.close()


@pytest.mark.asyncio
async def test_gallery_omits_failed_records_before_pagination_keeps_progress(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    success = await _source(tracker)
    failure = await tracker.begin(
        source="webui", prompt="failed", params={}, requester={}
    )
    await tracker.fail(failure["job_id"], error="provider failed")
    history = tracker.query_history(
        page=1, size=1, keyword="", source="", group_id="", user_id=""
    )
    assert history["total"] == 1
    assert history["items"][0]["job_id"] == success["job_id"]
    assert any(
        item["job_id"] == failure["job_id"] for item in tracker.active_and_recent()
    )
    assert tracker.get(failure["job_id"])["error"] == "provider failed"
    await tracker.close()

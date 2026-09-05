from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.test_web_studio_service import _config, _png
from tl.api_types import ApiRequestConfig
from tl.generation_tracker import GenerationTracker
from tl.plugin_config import ProviderCandidate
from tl.studio_parameters import (
    GENERATION_SETTING_KEYS,
    generation_fields,
    validate_generation_settings,
)
from tl.tl_api import GeminiAPIClient
from tl.web_studio_service import StudioServiceError, WebStudioService


def _candidate(api_type="google", **settings):
    return ProviderCandidate(
        id=f"{api_type}#1",
        api_type=api_type,
        settings={"api_keys": ["private-key-sentinel"], "model": "model", **settings},
    )


def test_schema_covers_all_generation_fields_without_exposing_connection_values():
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
    )
    templates = schema["provider_settings"]["items"]["provider_overrides"]["templates"]
    excluded = {
        "enabled",
        "priority",
        "api_keys",
        "daily_limit_per_key",
        "model_alias",
        "model",
        "api_base",
        "proxy",
        "endpoint_id",
        "model_capability",
        "endpoint_mode",
        "poll_interval",
        "poll_timeout",
    }
    for api_type, template in templates.items():
        assert set(template["items"]) - GENERATION_SETTING_KEYS <= excluded
        candidate = _candidate(
            api_type, api_base="private-url-sentinel", proxy="private-proxy-sentinel"
        )
        fields = generation_fields(candidate)
        assert set(fields) == set(template["items"]) & GENERATION_SETTING_KEYS
        assert not (set(fields) & excluded)
        encoded = json.dumps(fields)
        for secret in (
            "private-key-sentinel",
            "private-url-sentinel",
            "private-proxy-sentinel",
        ):
            assert secret not in encoded


def test_values_preserve_false_zero_empty_and_reject_bad_types():
    candidate = _candidate("modelscope", seed=0, negative_prompt="", guidance=0)
    fields = generation_fields(candidate)
    assert fields["seed"]["value"] == 0
    assert fields["negative_prompt"]["value"] == ""
    assert validate_generation_settings(
        candidate, {"seed": 0, "negative_prompt": "", "guidance": 0}
    ) == {"seed": 0, "negative_prompt": "", "guidance": 0}
    assert validate_generation_settings(_candidate(), {"enable_grounding": False}) == {
        "enable_grounding": False
    }
    for bad in (
        {"seed": True},
        {"guidance": float("nan")},
        {"seed": "42"},
        {"negative_prompt": {}},
    ):
        with pytest.raises(ValueError):
            validate_generation_settings(candidate, bad)


@pytest.mark.parametrize(
    "name",
    [
        "api_keys",
        "api_base",
        "proxy",
        "model",
        "endpoint_id",
        "enabled",
        "priority",
        "arbitrary",
    ],
)
def test_request_boundary_rejects_connection_and_unknown_overrides(tmp_path, name):
    candidate = _candidate()
    service = WebStudioService(
        None,
        GenerationTracker(tmp_path, 20),
        _config(provider_candidates=[candidate]),
        tmp_path,
    )
    with pytest.raises(StudioServiceError, match="不允许临时覆盖"):
        service.validate_payload(
            {
                "prompt": "draw",
                "candidate_id": candidate.id,
                "generation_settings": {name: "forbidden"},
            }
        )


def test_overrides_require_specific_candidate_and_update_reference_limit(tmp_path):
    candidate = _candidate(max_reference_images=1)
    service = WebStudioService(
        None,
        GenerationTracker(tmp_path, 20),
        _config(provider_candidates=[candidate]),
        tmp_path,
    )
    service.gallery_dir.mkdir()
    for name in ["one.png", "two.png"]:
        _png(service.gallery_dir / name)
    with pytest.raises(StudioServiceError, match="必须指定"):
        service.validate_payload(
            {"prompt": "draw", "generation_settings": {"max_reference_images": 2}}
        )
    normalized, _ = service.validate_payload(
        {
            "prompt": "draw",
            "candidate_id": candidate.id,
            "reference_names": ["one.png", "two.png"],
            "generation_settings": {"max_reference_images": 2},
        }
    )
    assert len(normalized["reference_images"]) == 2
    assert candidate.settings["max_reference_images"] == 1


@pytest.mark.asyncio
async def test_candidate_overrides_are_isolated_between_concurrent_requests():
    candidate = _candidate(
        enable_grounding=False, enable_text_response=False, force_resolution=False
    )
    client = GeminiAPIClient(["fallback"])
    client.set_provider_candidates([candidate])
    seen = {}

    async def generate(**kwargs):
        config = kwargs["config"]
        await asyncio.sleep(0)
        seen[config.prompt] = config
        return ["image"], [], None, None

    client._generate_image_single = generate
    override = {
        "enable_grounding": True,
        "enable_text_response": True,
        "force_resolution": True,
        "resolution_param_name": "custom_size",
    }
    await asyncio.gather(
        client._generate_image_with_candidates(
            ApiRequestConfig(
                model="",
                prompt="temporary",
                requested_candidate_id=candidate.id,
                generation_settings=override,
            )
        ),
        client._generate_image_with_candidates(
            ApiRequestConfig(
                model="", prompt="default", requested_candidate_id=candidate.id
            )
        ),
    )
    assert seen["temporary"].enable_grounding is True
    assert seen["temporary"].response_modalities == "TEXT_IMAGE"
    assert seen["temporary"].force_resolution is True
    assert seen["temporary"].resolution_param_name == "custom_size"
    assert seen["default"].enable_grounding is False
    assert seen["default"].response_modalities == "IMAGE"
    assert candidate.settings["enable_grounding"] is False
    assert "resolution_param_name" not in candidate.settings


@pytest.mark.asyncio
async def test_temporary_native_count_caps_each_request_without_changing_target():
    candidate = _candidate("xai", model="grok-imagine-image", n=2)
    client = GeminiAPIClient(["fallback"])
    client.set_provider_candidates([candidate])
    seen = []

    async def generate(**kwargs):
        seen.append(kwargs["config"])
        return ["image"], [], None, None

    client._generate_image_single = generate
    config = ApiRequestConfig(
        model="",
        prompt="draw",
        requested_candidate_id=candidate.id,
        image_count=3,
        generation_settings={"n": 1},
    )
    await client._generate_image_with_candidates(config)
    assert config.image_count == 3
    assert seen[0].effective_image_count == 1
    assert seen[0].provider_settings["n"] == 1
    assert candidate.settings["n"] == 2


@pytest.mark.asyncio
async def test_generic_settings_history_reuse_and_missing_candidate(tmp_path):
    tracker = GenerationTracker(tmp_path, 20)
    candidate = _candidate("modelscope", model="Qwen/Qwen-Image")
    service = WebStudioService(
        None, tracker, _config(provider_candidates=[candidate]), tmp_path
    )
    normalized, _ = service.validate_payload(
        {
            "prompt": "draw",
            "candidate_id": candidate.id,
            "generation_settings": {"steps": 20, "guidance": 0, "negative_prompt": ""},
        }
    )
    record = await tracker.begin(
        source="webui",
        prompt="draw",
        params=service._history_params(normalized),
        requester={},
    )
    await tracker.complete(
        record["job_id"], image_files=[], text_content=None, stats={}
    )
    reused, warning = service.validate_payload({"reuse_job_id": record["job_id"]})
    assert warning is None
    assert reused["generation_settings"] == {
        "steps": 20,
        "guidance": 0,
        "negative_prompt": "",
    }
    service.config.provider_candidates = [_candidate()]
    reused, warning = service.validate_payload({"reuse_job_id": record["job_id"]})
    assert warning
    assert reused["generation_settings"] == {}
    assert reused["candidate_id"] == "google#1"
    await tracker.close()


@pytest.mark.parametrize("initial, temporary", [(True, False), (False, True)])
def test_generations_only_override_updates_editing_capability(
    tmp_path, initial, temporary
):
    candidate = ProviderCandidate(
        id="openai_images#1",
        api_type="openai_images",
        settings={
            "api_keys": ["test"],
            "model": "gpt-image-1",
            "generations_only": initial,
            "max_reference_images": 2,
        },
        supports_image_edit=not initial,
    )
    service = WebStudioService(
        None,
        GenerationTracker(tmp_path, 20),
        _config(provider_candidates=[candidate]),
        tmp_path,
    )
    service.gallery_dir.mkdir()
    _png(service.gallery_dir / "input.png")
    payload = {
        "prompt": "edit",
        "candidate_id": candidate.id,
        "reference_names": ["input.png"],
        "generation_settings": {"generations_only": temporary},
    }
    if temporary:
        with pytest.raises(StudioServiceError, match="参考图片总数不能超过 0"):
            service.validate_payload(payload)
    else:
        normalized, _ = service.validate_payload(payload)
        assert len(normalized["reference_images"]) == 1
    entry = service.capabilities()["models"][0]
    assert entry["native_max_reference_images"] >= 2
    assert (
        entry["generation_fields"]["generations_only"]["disables_references_when"]
        is True
    )
    assert candidate.supports_image_edit is not initial
    assert candidate.settings["generations_only"] is initial


@pytest.mark.parametrize(
    "api_type, model", [("openai_images", "gpt-image-1"), ("xai", "grok-imagine-image")]
)
def test_empty_quality_override_clears_configured_quality(tmp_path, api_type, model):
    candidate = _candidate(api_type, model=model, quality="high")
    service = WebStudioService(
        None,
        GenerationTracker(tmp_path, 20),
        _config(provider_candidates=[candidate]),
        tmp_path,
    )
    normalized, _ = service.validate_payload(
        {
            "prompt": "draw",
            "candidate_id": candidate.id,
            "generation_settings": {"quality": ""},
        }
    )
    assert normalized["generation_settings"] == {"quality": ""}
    assert (
        ""
        in service.capabilities()["models"][0]["generation_fields"]["quality"]["enum"]
    )
    assert candidate.settings["quality"] == "high"

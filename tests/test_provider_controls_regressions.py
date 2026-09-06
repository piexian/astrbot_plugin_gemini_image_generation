from __future__ import annotations

import asyncio

import pytest
from test_model_catalog import FakeResponse, request
from test_model_catalog import http as http
from test_provider_config_frontend import _run

from tl.model_catalog import ModelCatalogError, ModelCatalogService


def test_blur_change_does_not_replace_the_model_button_or_search_result():
    _run(r"""
const view = create(); await view.open(); edit(0);
input(field('api_base'), 'https://edited.invalid/v1');
const fetch = action('fetch-models', null, body), token = view.catalogs.edit.token;
change(field('api_base'), 'https://edited.invalid/v1');
assert.equal(action('fetch-models', null, body), fetch);
assert.equal(view.catalogs.edit.token, token);
post = async () => ({models: [{id: 'model-picked', label: 'Model Picked'}]});
fetch.click(); await tick();
const search = body.querySelector('[data-pc-search]');
input(search, 'Picked');
const choice = action('choose-model', null, body);
change(search, 'Picked'); assert.equal(action('choose-model', null, body), choice);
choice.click(); assert.equal(field('model').value, 'model-picked'); view.destroy();
""")


def test_success_envelope_preserves_target_confirmation_with_message_only_bridge_errors():
    _run(r"""
const view = create(); await view.open(); edit(0);
post = async () => ({confirmation_required: true, target: 'https://target.invalid'});
action('fetch-models', null, body).click(); await tick();
assert.equal(view.catalogs.edit.confirmation.target, 'https://target.invalid');
assert.ok(action('confirm-target', null, body));
post = async () => ({models: [{id: 'chosen', label: 'Chosen'}]});
action('confirm-target', null, body).click(); await tick();
assert.equal(posts()[1].payload.confirmed_target, true);
action('choose-model', null, body).click(); assert.equal(field('model').value, 'chosen');
post = async () => { throw Error('供应商配置已更新，请重新加载后再保存'); };
action('fetch-models', null, body).click(); await tick();
assert.match(body.textContent, /配置版本已变化/); assert.equal(field('model').value, 'chosen');
view.destroy();
""")


def test_unavailable_vision_refresh_retains_last_roster_and_draft():
    _run(r"""
const view = create(); await view.open(); selectTab('common');
change(field('vision_provider_id', root), 'vision-1');
input(field('vision_model', root), 'kept-model');
const previous = JSON.stringify(view.snapshot.vision_providers);
get = async () => ({vision_providers: [], vision_providers_available: false, vision_providers_warning: 'Unavailable'});
action('refresh-vision').click(); await tick();
assert.equal(JSON.stringify(view.snapshot.vision_providers), previous);
assert.equal(field('vision_provider_id', root).value, 'vision-1');
assert.equal(field('vision_model', root).value, 'kept-model');
assert.match(root.textContent, /刷新失败/); view.destroy();
""")


@pytest.mark.asyncio
async def test_catalog_queue_is_bounded_and_rejection_does_not_open_another_session(
    http,
):
    release = asyncio.Event()
    responses = [FakeResponse({"data": []}, block=release) for _ in range(2)]
    http.responses = responses.copy()
    service = ModelCatalogService()
    tasks = [asyncio.create_task(service.fetch(request())) for _ in range(4)]
    await asyncio.wait_for(
        asyncio.gather(*(response.started.wait() for response in responses)), 1
    )
    assert len(service._workers) == 4
    with pytest.raises(ModelCatalogError) as error:
        await service.fetch(request())
    assert error.value.status_code == 429 and error.value.reason == "busy"
    assert len(http.sessions) == 2
    await service.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not service._workers

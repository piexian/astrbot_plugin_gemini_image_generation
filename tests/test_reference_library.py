"""参考图库生命周期：关闭后拒绝迟到写入，保护热重载后的新实例数据。"""

from __future__ import annotations

import base64

import pytest

from tl.reference_library import ReferenceLibrary, ReferenceLibraryError

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.mark.asyncio
async def test_close_rejects_late_writes(tmp_path):
    library = ReferenceLibrary(tmp_path, validate_fn=lambda data: "png")
    await library.add_bytes(_PNG, label="原图")
    await library.close()
    with pytest.raises(ReferenceLibraryError) as exc:
        await library.add_bytes(_PNG, label="迟到写入")
    assert exc.value.status_code == 503

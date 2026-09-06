"""供应商配置更新与生成操作的互斥准入。"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import aclosing, contextmanager, nullcontext
from functools import wraps


class ProviderRuntimeBusy(RuntimeError):
    def __init__(self, reason: str = "busy"):
        self.reason = reason
        self.message = {
            "busy": "生成任务正在运行，请等待结束后再保存供应商配置",
            "updating": "供应商配置正在更新，请稍后重试",
            "closed": "插件正在关闭，请稍后重试",
            "failed": "供应商配置恢复失败，请重载插件后再试",
        }[reason]
        super().__init__(self.message)


class ProviderRuntime:
    def __init__(self, is_busy: Callable[[], bool] | None = None):
        self._is_busy = is_busy or (lambda: False)
        self.active = 0
        self.updating = False
        self.closed = False
        self.failed = False

    @property
    def busy(self) -> bool:
        return self.active > 0 or self._is_busy()

    @contextmanager
    def operation(self):
        if self.closed or self.failed or self.updating:
            raise ProviderRuntimeBusy(
                "closed" if self.closed else "failed" if self.failed else "updating"
            )
        # 同一事件循环内同步进入，不在检查与增加计数之间让出控制权。
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1

    @contextmanager
    def update(self):
        if self.closed or self.failed:
            raise ProviderRuntimeBusy("closed" if self.closed else "failed")
        if self.updating or self.busy:
            raise ProviderRuntimeBusy("busy")
        self.updating = True
        try:
            yield
        finally:
            self.updating = False


def _operation(owner):
    plugin = getattr(owner, "plugin", None)
    runtime = getattr(plugin if plugin is not None else owner, "provider_runtime", None)
    return runtime.operation() if runtime is not None else nullcontext()


def provider_operation(kind: str):
    """保留宿主可内省签名，并在生成准备到返回期间持有配置租约。"""

    def decorate(function):
        if inspect.isasyncgenfunction(function):

            @wraps(function)
            async def generate(owner, *args, **kwargs):
                try:
                    with _operation(owner):
                        async with aclosing(
                            function(owner, *args, **kwargs)
                        ) as results:
                            async for result in results:
                                yield result
                except ProviderRuntimeBusy as exc:
                    event = args[0] if args else kwargs["event"]
                    yield event.plain_result(exc.message)

            return generate

        @wraps(function)
        async def call(owner, *args, **kwargs):
            try:
                with _operation(owner):
                    return await function(owner, *args, **kwargs)
            except ProviderRuntimeBusy as exc:
                if kind == "studio":
                    from .web_studio_service import StudioServiceError

                    raise StudioServiceError(
                        exc.message, status_code=503, data={"reason": exc.reason}
                    ) from exc
                if kind == "api":
                    raise
                if kind == "core":
                    return False, exc.message
                if kind == "legacy_tool":
                    return [exc.message]
                return exc.message

        return call

    return decorate

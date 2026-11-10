"""插件级注册 DSH provider，避免修改 AstrBot core。

启动顺序上，``plugin_manager.reload()`` 早于 ``provider_manager.initialize()``
（``astrbot/core/core_lifecycle.py`` L260、L264），所以本模块在**导入时**：
  1. 加载同目录的 ``dsh_bridge_source.py``，由装饰器写入 ``provider_cls_map``；
  2. 给 ``ProviderManager.dynamic_import_provider`` 套一层幂等包装：
     类型已在 ``provider_cls_map`` 里就直接返回，否则走原函数。
这样 ``type: "dsh_bridge"`` 不会再落入 ``match`` 的 ``case _`` 抛 ImportError，
标准 provider 的行为完全不变。
"""

from __future__ import annotations

import importlib.util
import os
import sys

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.manager import ProviderManager
from astrbot.core.provider.register import provider_cls_map, provider_registry

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PROVIDER_SOURCE = os.path.join(_PLUGIN_DIR, "dsh_bridge_source.py")
_PROVIDER_MODULE = "astrbot_plugin_dsh_bridge_provider"
_PATCH_FLAG = "_dsh_bridge_noop_patch"
_PATCH_ORIGINAL = "_dsh_bridge_original"


def _register_provider() -> None:
    """导入 provider 模块，让 @register_provider_adapter 完成注册。"""
    if "dsh_bridge" in provider_cls_map:
        return
    spec = importlib.util.spec_from_file_location(_PROVIDER_MODULE, _PROVIDER_SOURCE)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 DSH provider 源文件: {_PROVIDER_SOURCE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PROVIDER_MODULE] = module
    spec.loader.exec_module(module)
    logger.info("[dsh_bridge] provider adapter 已由插件注册")


def _patch_provider_manager() -> None:
    """幂等包装 dynamic_import_provider：已注册的类型直接返回。"""
    current = ProviderManager.dynamic_import_provider
    if getattr(current, _PATCH_FLAG, False):
        return
    original = current

    def dynamic_import_provider(self, type: str) -> None:  # noqa: A002
        if type in provider_cls_map:
            return
        return original(self, type)

    setattr(dynamic_import_provider, _PATCH_FLAG, True)
    setattr(dynamic_import_provider, _PATCH_ORIGINAL, original)
    ProviderManager.dynamic_import_provider = dynamic_import_provider
    logger.info("[dsh_bridge] ProviderManager.dynamic_import_provider 已打无操作补丁")


_register_provider()
_patch_provider_manager()


@register(
    "astrbot_plugin_dsh_bridge",
    "dafeiyu-local",
    "插件级注册 DSH bridge provider，无需修改 AstrBot core",
    "v1.0.0",
)
class DSHBridgeProviderPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def terminate(self) -> None:
        """插件卸载时恢复原函数并撤销注册。"""
        current = ProviderManager.dynamic_import_provider
        if getattr(current, _PATCH_FLAG, False):
            original = getattr(current, _PATCH_ORIGINAL, None)
            if original is not None:
                ProviderManager.dynamic_import_provider = original
        metadata = provider_cls_map.get("dsh_bridge")
        if metadata is not None and getattr(metadata.cls_type, "__module__", "") == _PROVIDER_MODULE:
            provider_cls_map.pop("dsh_bridge", None)
            try:
                provider_registry.remove(metadata)
            except ValueError:
                pass
            sys.modules.pop(_PROVIDER_MODULE, None)

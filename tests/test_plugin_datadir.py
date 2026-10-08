"""离线自测：插件持久化目录规范 + 旧数据迁移。

覆盖插件市场审核要求：
- 持久化数据必须写在 AstrBot 规范位置 ``data/plugin_data/<插件名>``；
- 不能写进插件自身代码目录（``data/plugins/<插件名>``），否则插件更新/重装会丢用户数据；
- 旧版本误写在插件目录下的数据，启动时要能自动迁移，且不能被旧数据反向覆盖。

本机通常没有安装 AstrBot，这里用桩模块加载 ``main.py``，因此可直接运行：

    python tests/test_plugin_datadir.py
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTED_DATA_DIR = os.path.join("data", "plugin_data", "astrbot_plugin_hmp_bot")
LEGACY_DATA_DIR = os.path.join("data", "plugins", "astrbot_plugin_hmp_bot")
BINDINGS = "haulmp_bindings.json"


class _Filter:
    """AstrBot filter 桩：任意装饰器都原样返回被装饰的函数。"""

    class EventMessageType:
        ALL = "all"

    def __getattr__(self, name):
        return lambda *a, **k: lambda f: f


class _Star:
    def __init__(self, context=None, **kw):
        self.context = context


class _Dummy:
    pass


class _Log:
    def info(self, *a):
        print("  [info]", *a)

    def warning(self, *a):
        print("  [warn]", *a)

    def debug(self, *a):
        pass

    def error(self, *a):
        print("  [error]", *a)


def _mk(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


def _install_astrbot_stub():
    if "astrbot" in sys.modules:
        return
    _mk("astrbot")
    _mk("astrbot.api", AstrBotConfig=dict, logger=_Log())
    _mk("astrbot.api.event", AstrMessageEvent=_Dummy, filter=_Filter())
    _mk("astrbot.api.star", Context=_Dummy, Star=_Star)
    _mk("astrbot.api.message_components", Image=_Dummy, Plain=_Dummy)


def _load_plugin_class():
    _install_astrbot_stub()
    spec = importlib.util.spec_from_file_location(
        "hmp_main_under_test", os.path.join(REPO, "main.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.HmpBotPlugin


def test_data_dir_and_migration():
    plugin_cls = _load_plugin_class()
    workdir = tempfile.mkdtemp(prefix="hmp_datadir_")
    freshdir = tempfile.mkdtemp(prefix="hmp_datadir_fresh_")
    cwd = os.getcwd()
    try:
        os.chdir(workdir)
        os.makedirs(LEGACY_DATA_DIR, exist_ok=True)
        legacy_file = os.path.join(LEGACY_DATA_DIR, BINDINGS)
        with open(legacy_file, "w", encoding="utf-8") as f:
            json.dump({"user-1": [{"handle": "Ron"}]}, f, ensure_ascii=False)

        plugin = plugin_cls(context=None, config={})
        assert plugin.data_dir == EXPECTED_DATA_DIR, plugin.data_dir
        assert "plugin_data" in plugin.maps_dir.replace("\\", "/"), plugin.maps_dir
        assert os.path.isdir(plugin.maps_dir), "maps 目录未创建"

        # 旧数据应已迁移到规范目录
        with open(plugin.bindings_path, encoding="utf-8") as f:
            assert json.load(f) == {"user-1": [{"handle": "Ron"}]}, "旧数据未迁移"

        # 再次启动：新目录已有数据时，不能被旧目录里的陈旧数据覆盖
        with open(legacy_file, "w", encoding="utf-8") as f:
            json.dump({"stale": [{"handle": "OLD"}]}, f, ensure_ascii=False)
        plugin2 = plugin_cls(context=None, config={})
        with open(plugin2.bindings_path, encoding="utf-8") as f:
            assert json.load(f) == {"user-1": [{"handle": "Ron"}]}, "被旧数据覆盖"

        # 全新环境（无任何旧数据）同样可用
        os.chdir(freshdir)
        plugin3 = plugin_cls(context=None, config={})
        assert os.path.isdir(plugin3.data_dir), plugin3.data_dir
        assert plugin3.bindings_path.endswith(BINDINGS), plugin3.bindings_path

        print("  数据目录:", plugin.data_dir)
        print("  迁移结果:", plugin.bindings_path)
    finally:
        os.chdir(cwd)
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(freshdir, ignore_errors=True)


if __name__ == "__main__":
    test_data_dir_and_migration()
    print("DATADIR TEST OK")

# 文件用途：离线自测脚本 —— 验证插件数据目录为 data/plugin_data 且旧数据可自动迁移

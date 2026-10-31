"""Smoke-test a built MaiBot ZIP in isolation, without Hub/model credentials."""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
import socket
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

from maibot_sdk.context import PluginContext, PluginPaths


async def check(archive):
    with tempfile.TemporaryDirectory(prefix="longhub-maibot-package-") as temporary:
        root = Path(temporary)
        with ZipFile(archive) as bundle:
            for name in bundle.namelist():
                path = Path(name)
                assert (
                    name.startswith("maibot_plugin_longhub/")
                    and not path.is_absolute()
                    and ".." not in path.parts
                )
                assert path.suffix.lower() not in {
                    ".png",
                    ".jpg",
                    ".gif",
                    ".webp",
                    ".sqlite3",
                    ".pyc",
                }
                assert path.name not in {"main.py", "metadata.yaml", "_conf_schema.json"}
                assert not {"maibot_adapter", ".astrbot-plugin", "tools"}.intersection(path.parts)
                if path.suffix == ".py":
                    tree = ast.parse(bundle.read(name).decode("utf-8"))
                    for node in ast.walk(tree):
                        imports = (
                            [item.name for item in node.names]
                            if isinstance(node, ast.Import)
                            else [node.module or ""]
                            if isinstance(node, ast.ImportFrom)
                            else []
                        )
                        assert not any(
                            module.split(".")[0] in {"astrbot", "faiss"} for module in imports
                        )
            assert bundle.testzip() is None
            bundle.extractall(root)
        plugin_root = root / "maibot_plugin_longhub"
        spec = importlib.util.spec_from_file_location(
            "longhub_package_smoke",
            plugin_root / "plugin.py",
            submodule_search_locations=[str(plugin_root)],
        )
        entry = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = entry
        spec.loader.exec_module(entry)
        plugin = entry.create_plugin()
        data = root / "runtime-data"
        plugin._set_context(
            PluginContext("movetoex.longhub", paths=PluginPaths(data, root / "runtime"))
        )
        module = sys.modules["longhub_package_smoke.runtime.settings"]
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        module.SettingsFile(data).save(module.Settings(webhook_port=port, auto_tagging=False))
        try:
            await plugin.on_load()
            status = await plugin.panel_read("/status")
            assert status["total"] == 0 and status["receiver"]["started"]
            assert not status["semantic"]["available"]
        finally:
            await plugin.on_unload()
        assert not any(name == "astrbot" or name.startswith("astrbot.") for name in sys.modules)
        assert len(plugin.get_components()) == 10
        print(
            "ZIP smoke check passed: isolated SDK entry, 10 components, webhook startup, panel status, clean shutdown; native SDK only, no image files"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    asyncio.run(check(parser.parse_args().archive.resolve()))

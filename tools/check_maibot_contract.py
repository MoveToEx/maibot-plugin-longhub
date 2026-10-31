"""Validate against a trusted MaiBot source checkout, without starting its server.

Runs native Pydantic/JSON Schema/context validators. Host imports for logging,
instance discovery and dependency lookup are omitted from the manifest module;
its model definitions and validators are executed unchanged. This is a protocol
check, not a replacement for testing a running MaiBot and platform adapter.
"""

from __future__ import annotations

import argparse
import ast
import base64
import copy
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load(name, path, *, package=False):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if package else None
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def validate(source):
    runtime = source / "src" / "plugin_runtime"
    webui = load("longhub_native_webui", runtime / "webui_schema.py")
    extension = webui.load_webui_extension(str(ROOT))
    assert extension.pages[0].entry == "views/maibot/index.html"

    path = runtime / "runner" / "manifest_validator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.ImportFrom)
            and (node.level or (node.module or "").startswith("src."))
            or isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "logger" for target in node.targets
            )
        )
    ]
    manifest_module = types.ModuleType("longhub_native_manifest")
    sys.modules[manifest_module.__name__] = manifest_module
    exec(compile(tree, str(path), "exec"), manifest_module.__dict__)
    manifest = manifest_module.PluginManifest.model_validate(
        json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
    )

    plugin = load("longhub_contract_plugin", ROOT / "plugin.py", package=True).create_plugin()
    components = {item["name"]: item for item in plugin.get_components()}
    # Exercise the host's own visibility projection on real SDK registrations.
    path = runtime / "component_query.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    query_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ComponentQueryService"
    )
    visibility = next(
        node
        for node in query_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_get_tool_visibility"
    )
    visibility.decorator_list = []
    namespace = {}
    exec(compile(ast.Module(body=[visibility], type_ignores=[]), str(path), "exec"), namespace)
    for item in components.values():
        if item["type"] == "TOOL":
            assert (
                namespace["_get_tool_visibility"](SimpleNamespace(metadata=item["metadata"]))
                == "visible"
            )
    declared_apis = {item["name"] for item in components.values() if item["type"] == "API"}
    for page in extension.pages:
        for binding in [*page.queries.values(), *page.actions.values()]:
            assert binding.api in declared_apis

    reply_schema = load(
        "longhub_native_reply_schema", runtime / "host" / "reply_extension_schema.py"
    )
    schema = reply_schema.validate_parameter_schema(
        components["meme"]["metadata"]["parameters_schema"]
    )
    parameters = reply_schema.validate_parameters(
        {"image_id": "9223372036854775807", "position": "after", "only_image": True}, schema
    )
    assert parameters["image_id"] == "9223372036854775807"

    payload = source / "src" / "llm_models" / "payload_content"
    package = types.ModuleType("longhub_native_context")
    package.__path__ = [str(payload)]
    sys.modules[package.__name__] = package
    load(package.__name__ + ".context_item", payload / "context_item.py")
    snapshots = load(package.__name__ + ".context_snapshot", payload / "context_snapshot.py")
    policy = sys.modules["longhub_contract_plugin.maibot_adapter.policy"]
    for item in policy.inject([], policy.SEARCH_POLICY):
        restored = snapshots.deserialize_context_item_snapshot(item)
        assert snapshots.serialize_context_item_snapshot(restored) == item

    # The two pure native send validators have no host lifecycle dependencies.
    path = runtime / "host" / "reply_extensions.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_validate_segment", "validate_reply_messages"}
    ]
    namespace = {
        "Any": Any,
        "Dict": dict,
        "List": list,
        "json": json,
        "base64": base64,
        "deepcopy": copy.deepcopy,
    }
    exec(compile(tree, str(path), "exec"), namespace)
    delivery = sys.modules["longhub_contract_plugin.maibot_adapter.delivery"]
    receipt = delivery.Receipt("chat", "turn", "1", {"image_id": "1"})
    messages = [
        {
            "segments": [receipt.segment(b"GIF89a", base64.b64encode(b"GIF89a").decode())],
            "quote_previous": False,
        }
    ]
    assert namespace["validate_reply_messages"](messages) == messages
    print(
        f"MaiBot native contracts passed: {manifest.id} {manifest.version}; manifest v2, WebUI v2/bindings, core tool visibility, reply schema/messages, Context Item roundtrip"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Trusted MaiBot source checkout")
    validate(parser.parse_args().source.resolve())

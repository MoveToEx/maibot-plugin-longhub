"""Build an installable ZIP from an explicit source allowlist."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def build():
    files = [
        ROOT / name
        for name in (
            "plugin.py",
            "__init__.py",
            "_manifest.json",
            "webui.json",
            "requirements.txt",
            "pyproject.toml",
            "uv.lock",
            ".python-version",
            "README.md",
            "LICENSE",
        )
    ]
    for directory, pattern in (
        ("backend", "*.py"),
        ("maibot_adapter", "*.py"),
        ("views/maibot", "*.html"),
        ("_locales", "*.json"),
    ):
        files.extend(p for p in (ROOT / directory).rglob(pattern) if p.is_file())
    files = [
        source
        for source in files
        if source.name != "delivery.py" or source.parent.name != "backend"
    ]
    files.append(ROOT / "tools" / "migrate_astrbot.py")
    files.append(ROOT / "tools" / "__init__.py")
    version = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))["version"]
    if not (ROOT / "views" / "maibot" / "index.html").is_file():
        raise RuntimeError("先运行 frontend 中的 npm run build")
    target = ROOT / "dist" / f"maibot_plugin_longhub-{version}.zip"
    target.parent.mkdir(exist_ok=True)
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for source in sorted(files):
            archive.write(source, "maibot_plugin_longhub/" + source.relative_to(ROOT).as_posix())
    print(target)
    return target


if __name__ == "__main__":
    build()

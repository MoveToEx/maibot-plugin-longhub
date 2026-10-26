"""Build an installable ZIP from an explicit source allowlist."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def build():
    files = [
        ROOT / name
        for name in (
            "main.py",
            "__init__.py",
            "metadata.yaml",
            "_conf_schema.json",
            "requirements.txt",
            "pyproject.toml",
            "uv.lock",
            ".python-version",
            "README.md",
            "LICENSE",
        )
    ]
    for directory, pattern in (("backend", "*.py"), ("views", "*"), (".astrbot-plugin", "*.json")):
        files.extend(p for p in (ROOT / directory).rglob(pattern) if p.is_file())
    target = ROOT / "dist" / "astrbot_plugin_longhub-0.4.1.zip"
    target.parent.mkdir(exist_ok=True)
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for source in sorted(files):
            archive.write(source, "astrbot_plugin_longhub/" + source.relative_to(ROOT).as_posix())
    print(target)
    return target


if __name__ == "__main__":
    build()

"""Build a clean Windows source bundle; local keys, jobs and installed runtimes never enter it."""
import argparse
import hashlib
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "h3ref.py", "workbench.py", "requirements.txt", "requirements-web.txt", "requirements-colab.txt",
    ".env.example", "LICENSE", "start-windows.cmd",
    "examples/film.example.json", "examples/dialogue-budget.json",
    "docs/windows.md", "docs/workbench.md", "docs/vendor-plyr.md",
    "scripts/start_colab.py", "scripts/windows-start.ps1", "scripts/windows-runtime.json",
    "scripts/messages/windows-zh-CN.json", "scripts/messages/windows-en.json",
)
DIRECTORIES = ("h3ref", "workbench", "prompts", "workflows")
EXTENSIONS = {".py", ".json", ".ps1", ".html", ".css", ".js", ".svg", ".md", ".txt"}


def package_files(root):
    files = {name: root / name for name in FILES}
    files["README-Windows.md"] = root / "docs/windows.md"
    for directory in DIRECTORIES:
        for path in sorted((root / directory).rglob("*")):
            relative = path.relative_to(root)
            if path.is_symlink():
                raise ValueError(f"Release input is a symlink: {path}")
            if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
                continue
            if path.is_file() and path.suffix in EXTENSIONS:
                files[relative.as_posix()] = path
    files["workbench/static/vendor/plyr/blank.mp4"] = root / "workbench/static/vendor/plyr/blank.mp4"
    for path in files.values():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Missing or unsafe release input: {path}")
    return files


def build_package(root, destination):
    files = package_files(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = []
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            content = path.read_bytes()
            if path.suffix in {".cmd", ".ps1"}:
                content = content.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            archive.writestr("h3-ref-video/" + name, content)
            manifest.append(f"{hashlib.sha256(content).hexdigest()}  {name}")
        archive.writestr("h3-ref-video/SHA256SUMS.txt", "\n".join(manifest) + "\n")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(".zip.sha256").write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    destination = args.out or ROOT / "releases" / f"h3-ref-video-windows-x64-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    print(build_package(ROOT, destination.resolve()))

"""Build the native Blender user add-on ZIP from explicitly approved inputs.

The explicit archive root is required by Blender's overwrite/remove discovery.
This tool is offline and is never imported by the add-on lifecycle.
Large JSON/Python entries use standard ZIP-LZMA only when smaller; installed
file bytes and runtime resource loading remain unchanged.
"""

import argparse
import ast
import hashlib
import io
import json
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

MODULE = "oxal_mc_studio"
CODE_DIRECTORIES = ("core", "business", "module", "operation", "ui", "util")
ASSET_SUFFIXES = {".blend", ".png", ".webp", ".json", ".mcmeta"}
RETIRED_ASSETS = frozenset(
    Path(f"asset/human/LAYER_RESOLUTION_2_4_{revision}.json") for revision in range(2, 8)
)


def assignments(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for node in ast.parse(path.read_text(encoding="utf8")).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        result[target.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        continue
    return result


def excluded(path: Path) -> bool:
    return any(part == "__pycache__" or part.endswith("_test") or "_test." in part
               or part.startswith(".") for part in path.parts)


def _approved_inputs(source: Path, include_untracked, file_list):
    if file_list is not None:
        if include_untracked:
            raise ValueError("Do not combine --file-list with --include-untracked")
        listed = json.loads(file_list.read_text(encoding="utf8"))
        if not isinstance(listed, list) or not all(isinstance(name, str) for name in listed):
            raise ValueError("Runtime file list must be a JSON array of relative paths")
        approved = set(listed)
    else:
        try:
            top = subprocess.run(["git", "-C", str(source), "rev-parse", "--show-toplevel"],
                                 check=True, capture_output=True).stdout.decode("utf8").strip()
            if Path(top).resolve() != source:
                raise ValueError("--source is not the Git root; use an explicit --file-list")
            tracked = subprocess.run(["git", "-C", str(source), "ls-files", "-z"],
                                     check=True, capture_output=True).stdout.decode("utf8")
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError("No Git input inventory; supply --file-list for a clean source export") from exc
        approved = set(tracked.rstrip("\0").split("\0")) | set(include_untracked)
        if include_untracked:
            ignored = subprocess.run(["git", "-C", str(source), "check-ignore", "--no-index", "-z", "--stdin"],
                                     input="\0".join(include_untracked).encode("utf8") + b"\0",
                                     check=False, capture_output=True)
            if ignored.returncode not in {0, 1}:
                raise ValueError("Unable to verify ignored input paths")
            if ignored.stdout:
                raise ValueError("Ignored files cannot be approved as runtime inputs")
    for name in approved:
        relative = Path(name)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                or relative.as_posix() != name or not relative.parts):
            raise ValueError(f"Input inventory contains a non-canonical relative path: {name!r}")
    return approved


def runtime_files(source: Path, *, include_untracked=(), file_list=None) -> list[Path]:
    source = source.resolve()
    approved = _approved_inputs(source, include_untracked, file_list)
    files = [source / "__init__.py", source / "README.md"]
    for directory in CODE_DIRECTORIES:
        files.extend((source / directory).rglob("*.py"))
    preview = Path("asset/library/preview")
    for path in (source / "asset").rglob("*"):
        if not path.is_file() or path.suffix.lower() not in ASSET_SUFFIXES:
            continue
        relative = path.relative_to(source)
        if relative in RETIRED_ASSETS:
            continue
        if (relative.parent == preview and path.suffix.lower() == ".png"
                and path.with_suffix(".webp").exists()):
            continue
        files.append(path)
    files = sorted({p for p in files if not excluded(p.relative_to(source))})
    unexpected = [p.relative_to(source).as_posix() for p in files if p.relative_to(source).as_posix() not in approved]
    if unexpected:
        raise ValueError("Unapproved runtime inputs; track formal files or name them explicitly with "
                         "--include-untracked: " + ", ".join(unexpected))
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Runtime input is not a regular file: {path}")
    return files


def validate_source(source: Path) -> str:
    info = assignments(source / "__init__.py")["bl_info"]
    version = ".".join(str(v) for v in info["version"])
    identity = assignments(source / "util/identity.py")
    manifest = tomllib.loads((source / "blender_manifest.toml").read_text(encoding="utf8"))
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf8"))
    if (identity["EXTENSION_ID"] != MODULE or identity["DISPLAY_VERSION"] != version + "-Beta"
            or manifest["version"] != version + "-beta"
            or project["project"]["version"] != version + "b0"):
        raise ValueError("Add-on version/identity declarations do not match")
    containers = json.loads((source / "asset/CONTAINERS.json").read_text(encoding="utf8"))
    for record in containers["files"]:
        path = (source / record["path"]).resolve()
        if not path.is_relative_to((source / "asset").resolve()):
            raise ValueError("Container resource leaves the asset directory")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["container_sha256"]:
            raise ValueError(f"Lossless resource container hash mismatch: {path.name}")
    human = source / "asset/human"
    metadata = json.loads((human / "SOURCE.json").read_text(encoding="utf8"))
    hashes: dict[Path, str] = {}
    for profile in metadata["versions"].values():
        for key in ("body", "armor", "layer_resolution", "armor_bindings", "back"):
            record = profile.get(key)
            if not isinstance(record, dict) or "path" not in record or "sha256" not in record:
                continue
            path = (human / record["path"]).resolve()
            if not path.is_relative_to(human.resolve()):
                raise ValueError(f"Resource path leaves the human directory: {record['path']}")
            if path not in hashes:
                hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            if hashes[path] != record["sha256"]:
                raise ValueError(f"Resource provenance hash mismatch: {path.name}")
    for family in ("legacy_compat_parts", "legacy_compat_names"):
        compat = metadata.get(family)
        if compat is None:
            continue
        for name, expected in ((compat["path"], compat["sha256"]),
                               (compat["declaration"], compat["declaration_sha256"])):
            path = (human / name).resolve()
            if not path.is_relative_to(human.resolve()):
                raise ValueError("Legacy component resource leaves the human directory")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Legacy component provenance hash mismatch: {path.name}")
    return identity["DISPLAY_VERSION"]


def _compressed_size(data: bytes, method: int) -> int:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=method,
                         compresslevel=9 if method == zipfile.ZIP_DEFLATED else None) as archive:
        archive.writestr("data", data)
    with zipfile.ZipFile(stream) as archive:
        return archive.getinfo("data").compress_size


def _write_runtime_file(archive: zipfile.ZipFile, path: Path, name: str) -> None:
    if path.suffix.lower() not in {".json", ".py"} or path.stat().st_size < 16384:
        archive.write(path, name)
        return
    data = path.read_bytes()
    method = (zipfile.ZIP_LZMA if _compressed_size(data, zipfile.ZIP_LZMA)
              < _compressed_size(data, zipfile.ZIP_DEFLATED) else zipfile.ZIP_DEFLATED)
    archive.writestr(zipfile.ZipInfo.from_file(path, name), data, compress_type=method,
                     compresslevel=9 if method == zipfile.ZIP_DEFLATED else None)


def build(source: Path, output: Path, *, include_untracked=(), file_list=None) -> dict[str, Any]:
    source, output = source.resolve(), output.resolve()
    if output.exists():
        raise FileExistsError(f"Keep existing candidates; choose a new output name: {output}")
    version = validate_source(source)
    files = runtime_files(source, include_untracked=include_untracked, file_list=file_list)
    names = {p.relative_to(source).as_posix() for p in files}
    required = {"__init__.py", "core/__init__.py", "core/commands.py"}
    required.update(f"{directory}/__init__.py" for directory in CODE_DIRECTORIES)
    if not required <= names or "core.py" in names:
        raise ValueError("Runtime package structure is incomplete or contains legacy core.py")
    directories = {MODULE + "/"}
    for relative in names:
        for parent in Path(relative).parents:
            if parent != Path("."):
                directories.add(MODULE + "/" + parent.as_posix() + "/")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp",
                                     dir=output.parent, delete=False) as temporary:
        staging = Path(temporary.name)
    try:
        with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for directory in sorted(directories, key=lambda name: (name.count("/"), name)):
                archive.mkdir(directory)
            for path in files:
                _write_runtime_file(archive, path, MODULE + "/" + path.relative_to(source).as_posix())
        with zipfile.ZipFile(staging) as archive:
            if archive.testzip() is not None or not archive.getinfo(MODULE + "/").is_dir():
                raise ValueError("ZIP integrity or explicit root-directory check failed")
            roots = [name.rstrip("/") for name in archive.namelist()
                     if "/" not in name.rstrip("/")]
            if roots != [MODULE]:
                raise ValueError(f"Blender overwrite discovery would see unexpected roots: {roots}")
            if MODULE + "/blender_manifest.toml" in archive.namelist():
                raise ValueError("User add-on packages must not contain the extension manifest")
            for path in files:
                if archive.read(MODULE + "/" + path.relative_to(source).as_posix()) != path.read_bytes():
                    raise ValueError(f"Archived bytes differ from source: {path}")
        staging.rename(output)
    finally:
        staging.unlink(missing_ok=True)
    return {"file": str(output), "display_version": version, "files": len(files),
            "directories": len(directories), "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-untracked", action="append", default=[], metavar="RELATIVE_PATH",
                        help="Explicitly approve one new formal runtime file; ignored files remain forbidden")
    parser.add_argument("--file-list", type=Path,
                        help="Explicit JSON relative-path inventory for source exports without Git")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, include_untracked=args.include_untracked,
                           file_list=args.file_list), ensure_ascii=False))


if __name__ == "__main__":
    main()

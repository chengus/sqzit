from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find a free output name for {path}")


def copy_output_path(source: Path, output_suffix: str | None = None) -> Path:
    suffix = output_suffix or source.suffix
    base = source.with_suffix(suffix)
    return unique_path(base.with_name(f"{base.stem}.compressed{base.suffix}"))


def backup_path(source: Path) -> Path:
    backup_dir = source.parent / "_backup"
    return unique_path(backup_dir / source.name)


def temporary_path(directory: Path, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=f".sqzit-{uuid.uuid4().hex}-", suffix=suffix, dir=directory)
    os.close(fd)
    path = Path(name)
    path.unlink(missing_ok=True)
    return path


def fsync_path(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def replace_with_backup(source: Path, encoded: Path) -> Path:
    if encoded.stat().st_size >= source.stat().st_size:
        raise ValueError("encoded file is not smaller than the source")
    backup = backup_path(source)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    fsync_path(encoded)
    source_stat = source.stat()
    os.replace(encoded, source)
    os.utime(source, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    return backup


def install_copy(source: Path, encoded: Path, output: Path) -> Path:
    if encoded.stat().st_size >= source.stat().st_size:
        raise ValueError("encoded file is not smaller than the source")
    output.parent.mkdir(parents=True, exist_ok=True)
    fsync_path(encoded)
    # The destination was selected with unique_path and is protected again
    # here so a repeated run can never overwrite an earlier result.
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)
    try:
        os.replace(encoded, output)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    shutil.copystat(source, output)
    return output

from pathlib import Path

import pytest

from sqzit.output import backup_path, copy_output_path, install_copy, replace_with_backup


def test_copy_output_path_does_not_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"source")
    first = copy_output_path(source)
    first.write_bytes(b"old output")
    second = copy_output_path(source)
    assert second == tmp_path / "clip.compressed-1.mp4"


def test_replace_with_backup_preserves_source_and_replaces(tmp_path: Path) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"large source file")
    encoded = tmp_path / ".encoded.jpg"
    encoded.write_bytes(b"small")

    backup = replace_with_backup(source, encoded)

    assert source.read_bytes() == b"small"
    assert backup == tmp_path / "_backup" / "photo.jpg"
    assert backup.read_bytes() == b"large source file"
    assert not encoded.exists()


def test_install_copy_rejects_non_smaller_output(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    encoded = tmp_path / ".encoded.mp4"
    output = tmp_path / "clip.compressed.mp4"
    source.write_bytes(b"same")
    encoded.write_bytes(b"same")

    with pytest.raises(ValueError, match="not smaller"):
        install_copy(source, encoded, output)

    assert not output.exists()


def test_backup_path_is_sibling_folder(tmp_path: Path) -> None:
    source = tmp_path / "folder" / "photo.png"
    assert backup_path(source) == tmp_path / "folder" / "_backup" / "photo.png"

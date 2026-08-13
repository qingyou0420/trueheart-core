"""Verify the exact TrueHeart Core v0.1.0 release distributions."""

from __future__ import annotations

import base64
import csv
import hashlib
import sys
import tarfile
import zipfile
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath

WHEEL_NAME = "trueheart_core-0.1.0-py3-none-any.whl"
SDIST_NAME = "trueheart_core-0.1.0.tar.gz"
EXPECTED_FILES = {WHEEL_NAME, SDIST_NAME}
SDIST_ROOT = "trueheart_core-0.1.0"
ALLOWED_REQUIRES_DIST = {
    'build<2,>=1.2; extra == "dev"',
    'mypy<2,>=1.15; extra == "dev"',
    'pytest<9,>=8; extra == "dev"',
    'ruff<1,>=0.11; extra == "dev"',
}


class VerificationError(Exception):
    """Raised when a release artifact violates the expected contract."""


def _verify_distribution_files(dist: Path) -> tuple[Path, Path]:
    if not dist.is_dir():
        raise VerificationError(f"distribution directory not found: {dist}")

    names = {path.name for path in dist.iterdir() if path.is_file()}
    unexpected = sorted(names - EXPECTED_FILES)
    if unexpected:
        raise VerificationError(
            "unexpected distribution files: " + ", ".join(unexpected)
        )
    for expected in (WHEEL_NAME, SDIST_NAME):
        if expected not in names:
            raise VerificationError(f"missing distribution: {expected}")
    return dist / WHEEL_NAME, dist / SDIST_NAME


def _verify_metadata(metadata_bytes: bytes, source: str) -> None:
    metadata: Message = BytesParser(policy=default).parsebytes(metadata_bytes)
    expected = {
        "Name": "trueheart-core",
        "Version": "0.1.0",
        "Requires-Python": ">=3.11",
        "License-Expression": "MIT",
    }
    for field, value in expected.items():
        actual = metadata.get(field)
        if actual != value:
            raise VerificationError(
                f"{source}: {field} must be {value} (found {actual!r})"
            )
    for requirement in metadata.get_all("Requires-Dist", []):
        if requirement not in ALLOWED_REQUIRES_DIST:
            raise VerificationError(
                f"{source}: unapproved Requires-Dist: {requirement}"
            )


def _wheel_record_hash(content: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
    return "sha256=" + encoded.rstrip(b"=").decode("ascii")


def _verify_wheel(wheel_path: Path) -> None:
    dist_info = "trueheart_core-0.1.0.dist-info"
    metadata_name = f"{dist_info}/METADATA"
    record_name = f"{dist_info}/RECORD"

    with zipfile.ZipFile(wheel_path) as wheel:
        archive_names = wheel.namelist()
        if len(archive_names) != len(set(archive_names)):
            raise VerificationError("wheel contains duplicate members")
        if metadata_name not in archive_names:
            raise VerificationError(f"wheel is missing {metadata_name}")
        if record_name not in archive_names:
            raise VerificationError(f"wheel is missing {record_name}")

        _verify_metadata(wheel.read(metadata_name), "wheel METADATA")

        record_text = wheel.read(record_name).decode("utf-8")
        rows = list(csv.reader(record_text.splitlines()))
        if any(len(row) != 3 for row in rows):
            raise VerificationError("wheel RECORD contains a malformed row")
        record_paths = [row[0] for row in rows]
        if len(record_paths) != len(set(record_paths)):
            raise VerificationError("wheel RECORD contains duplicate paths")
        if set(record_paths) != set(archive_names):
            raise VerificationError("wheel RECORD does not cover every archive member")

        for name, recorded_hash, recorded_size in rows:
            if name == record_name:
                if recorded_hash or recorded_size:
                    raise VerificationError("wheel RECORD must not hash itself")
                continue
            content = wheel.read(name)
            if recorded_hash != _wheel_record_hash(content):
                raise VerificationError(f"wheel RECORD hash mismatch: {name}")
            if recorded_size != str(len(content)):
                raise VerificationError(f"wheel RECORD size mismatch: {name}")


def _is_unsafe_tar_member(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != SDIST_ROOT
    )


def _verify_sdist(sdist_path: Path) -> None:
    metadata_name = f"{SDIST_ROOT}/PKG-INFO"
    with tarfile.open(sdist_path, mode="r:gz") as sdist:
        members = sdist.getmembers()
        names: set[str] = set()
        metadata_member: tarfile.TarInfo | None = None
        for member in members:
            if _is_unsafe_tar_member(member.name) or not (
                member.isfile() or member.isdir()
            ):
                raise VerificationError(f"unsafe sdist member: {member.name}")
            if member.name in names:
                raise VerificationError(f"duplicate sdist member: {member.name}")
            names.add(member.name)
            if member.name == metadata_name:
                metadata_member = member

        if metadata_member is None:
            raise VerificationError(f"sdist is missing {metadata_name}")
        metadata_file = sdist.extractfile(metadata_member)
        if metadata_file is None:
            raise VerificationError(f"sdist cannot read {metadata_name}")
        _verify_metadata(metadata_file.read(), "sdist PKG-INFO")


def _write_checksums(paths: tuple[Path, Path], destination: Path) -> None:
    lines = []
    for path in sorted(paths, key=lambda item: item.name):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    destination.write_text("".join(lines), encoding="ascii", newline="\n")


def verify_release_artifacts(dist: Path) -> None:
    wheel_path, sdist_path = _verify_distribution_files(dist)
    _verify_wheel(wheel_path)
    _verify_sdist(sdist_path)
    _write_checksums((wheel_path, sdist_path), Path("SHA256SUMS"))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        print("usage: verify_release_artifacts.py [DIST_DIRECTORY]", file=sys.stderr)
        return 2
    dist = Path(arguments[0]) if arguments else Path("dist")
    try:
        verify_release_artifacts(dist)
    except (
        OSError,
        UnicodeError,
        tarfile.TarError,
        VerificationError,
        zipfile.BadZipFile,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

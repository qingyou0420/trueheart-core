from __future__ import annotations

import base64
import csv
import hashlib
import io
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_release_artifacts.py"
WHEEL_NAME = "trueheart_core-0.1.0-py3-none-any.whl"
SDIST_NAME = "trueheart_core-0.1.0.tar.gz"
EXPECTED_DEV_REQUIREMENTS = (
    'build<2,>=1.2; extra == "dev"',
    'mypy<2,>=1.15; extra == "dev"',
    'pytest<9,>=8; extra == "dev"',
    'ruff<1,>=0.11; extra == "dev"',
)


def _metadata(
    *,
    version: str = "0.1.0",
    license_expression: str | None = "MIT",
    requires_python: str = ">=3.11",
    requires_dist: tuple[str, ...] = EXPECTED_DEV_REQUIREMENTS,
    provides_extra: tuple[str, ...] = ("dev",),
    extra_headers: tuple[str, ...] = (),
    omit_fields: tuple[str, ...] = (),
) -> bytes:
    lines = ["Metadata-Version: 2.4"]
    if "Name" not in omit_fields:
        lines.append("Name: trueheart-core")
    if "Version" not in omit_fields:
        lines.append(f"Version: {version}")
    if "Requires-Python" not in omit_fields:
        lines.append(f"Requires-Python: {requires_python}")
    if license_expression is not None and "License-Expression" not in omit_fields:
        lines.append(f"License-Expression: {license_expression}")
    lines.extend(f"Provides-Extra: {extra}" for extra in provides_extra)
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires_dist)
    lines.extend(extra_headers)
    return ("\n".join(lines) + "\n\n").encode()


def _record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
    return "sha256=" + digest.rstrip(b"=").decode("ascii")


def _write_wheel(
    path: Path,
    metadata: bytes,
    *,
    valid_record: bool = True,
    extra_member: str | None = None,
    extra_member_mode: int | None = None,
) -> None:
    dist_info = "trueheart_core-0.1.0.dist-info"
    files = {
        "trueheart_core/__init__.py": b'__version__ = "0.1.0"\n',
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: release-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    if extra_member is not None:
        files[extra_member] = b"unsafe\n"
    record_buffer = io.StringIO(newline="")
    writer = csv.writer(record_buffer, lineterminator="\n")
    for name, content in files.items():
        digest = _record_hash(content)
        if not valid_record and name.endswith("/METADATA"):
            digest = "sha256=invalid"
        writer.writerow((name, digest, len(content)))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    files[f"{dist_info}/RECORD"] = record_buffer.getvalue().encode()

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            if name == extra_member and extra_member_mode is not None:
                member = zipfile.ZipInfo(name)
                member.create_system = 3
                member.external_attr = extra_member_mode << 16
                archive.writestr(member, content)
            else:
                archive.writestr(name, content)


def _add_tar_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    member.mode = 0o644
    archive.addfile(member, io.BytesIO(content))


def _write_sdist(
    path: Path,
    metadata: bytes,
    *,
    extra_members: tuple[str, ...] = (),
    link_member: str | None = None,
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        root = "trueheart_core-0.1.0"
        _add_tar_bytes(archive, f"{root}/PKG-INFO", metadata)
        _add_tar_bytes(archive, f"{root}/pyproject.toml", b"[build-system]\n")
        for member_name in extra_members:
            _add_tar_bytes(archive, member_name, b"unsafe\n")
        if link_member is not None:
            member = tarfile.TarInfo(link_member)
            member.type = tarfile.SYMTYPE
            member.linkname = f"{root}/pyproject.toml"
            archive.addfile(member)


def _create_artifacts(
    directory: Path,
    *,
    version: str = "0.1.0",
    license_expression: str | None = "MIT",
    requires_python: str = ">=3.11",
    requires_dist: tuple[str, ...] = EXPECTED_DEV_REQUIREMENTS,
    provides_extra: tuple[str, ...] = ("dev",),
    extra_headers: tuple[str, ...] = (),
    omit_fields: tuple[str, ...] = (),
    wheel_member: str | None = None,
    wheel_member_mode: int | None = None,
    sdist_members: tuple[str, ...] = (),
    sdist_link: str | None = None,
    valid_record: bool = True,
) -> Path:
    dist = directory / "dist"
    dist.mkdir()
    metadata = _metadata(
        version=version,
        license_expression=license_expression,
        requires_python=requires_python,
        requires_dist=requires_dist,
        provides_extra=provides_extra,
        extra_headers=extra_headers,
        omit_fields=omit_fields,
    )
    wheel_name = WHEEL_NAME.replace("0.1.0", version)
    sdist_name = SDIST_NAME.replace("0.1.0", version)
    _write_wheel(
        dist / wheel_name,
        metadata,
        valid_record=valid_record,
        extra_member=wheel_member,
        extra_member_mode=wheel_member_mode,
    )
    _write_sdist(
        dist / sdist_name,
        metadata,
        extra_members=sdist_members,
        link_member=sdist_link,
    )
    return dist


def _run_verifier(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), "dist"],
        cwd=directory,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_rejected(result: subprocess.CompletedProcess[str], message: str) -> None:
    assert result.returncode != 0
    assert message in result.stderr


def test_verifier_rejects_artifacts_for_a_different_version(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, version="0.2.0")

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "unexpected distribution files")


def test_verifier_rejects_a_release_without_a_wheel(tmp_path: Path) -> None:
    dist = _create_artifacts(tmp_path)
    (dist / WHEEL_NAME).unlink()

    result = _run_verifier(tmp_path)

    _assert_rejected(result, f"missing distribution: {WHEEL_NAME}")


def test_verifier_rejects_a_release_without_an_sdist(tmp_path: Path) -> None:
    dist = _create_artifacts(tmp_path)
    (dist / SDIST_NAME).unlink()

    result = _run_verifier(tmp_path)

    _assert_rejected(result, f"missing distribution: {SDIST_NAME}")


def test_verifier_rejects_an_extra_distribution(tmp_path: Path) -> None:
    dist = _create_artifacts(tmp_path)
    (dist / "trueheart_core-0.1.0.zip").write_bytes(b"extra")

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "unexpected distribution files")


@pytest.mark.parametrize("member_name", ["../escape.txt", "/absolute.txt"])
def test_verifier_rejects_unsafe_sdist_member_paths(
    tmp_path: Path, member_name: str
) -> None:
    _create_artifacts(tmp_path, sdist_members=(member_name,))

    result = _run_verifier(tmp_path)

    _assert_rejected(result, f"unsafe sdist member: {member_name}")


@pytest.mark.parametrize(
    "member_name",
    [
        "../escaped.py",
        "/absolute.py",
        "trueheart_core/./alias.py",
        "trueheart_core\\..\\escaped.py",
        "C:/escaped.py",
        "//server/share/escaped.py",
    ],
)
def test_verifier_rejects_noncanonical_wheel_member_paths(
    tmp_path: Path, member_name: str
) -> None:
    _create_artifacts(tmp_path, wheel_member=member_name)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "unsafe wheel member:")


def test_verifier_rejects_a_non_regular_wheel_member(tmp_path: Path) -> None:
    member_name = "trueheart_core/link.py"
    _create_artifacts(
        tmp_path,
        wheel_member=member_name,
        wheel_member_mode=stat.S_IFLNK | 0o777,
    )

    result = _run_verifier(tmp_path)

    _assert_rejected(result, f"unsafe wheel member: {member_name}")


@pytest.mark.parametrize(
    "member_name",
    [
        "trueheart_core-0.1.0/./alias.txt",
        "trueheart_core-0.1.0\\..\\escaped.txt",
        "C:/escaped.txt",
        "//server/share/escaped.txt",
    ],
)
def test_verifier_rejects_noncanonical_sdist_member_paths(
    tmp_path: Path, member_name: str
) -> None:
    _create_artifacts(tmp_path, sdist_members=(member_name,))

    result = _run_verifier(tmp_path)

    _assert_rejected(result, f"unsafe sdist member: {member_name}")


def test_verifier_rejects_sdist_members_with_the_same_normalized_path(
    tmp_path: Path,
) -> None:
    root = "trueheart_core-0.1.0"
    _create_artifacts(
        tmp_path,
        sdist_members=(f"{root}/./pyproject.toml",),
    )

    result = _run_verifier(tmp_path)

    _assert_rejected(result, f"unsafe sdist member: {root}/./pyproject.toml")


def test_verifier_rejects_a_non_regular_sdist_member(tmp_path: Path) -> None:
    member_name = "trueheart_core-0.1.0/link.py"
    _create_artifacts(tmp_path, sdist_link=member_name)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, f"unsafe sdist member: {member_name}")


@pytest.mark.parametrize(
    "field",
    ["Name", "Version", "Requires-Python", "License-Expression"],
)
def test_verifier_rejects_missing_singleton_metadata_fields(
    tmp_path: Path, field: str
) -> None:
    _create_artifacts(tmp_path, omit_fields=(field,))

    result = _run_verifier(tmp_path)

    _assert_rejected(result, f"{field} must appear exactly once")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Name", "trueheart-core"),
        ("Version", "0.1.0"),
        ("Requires-Python", ">=3.11"),
        ("License-Expression", "MIT"),
    ],
)
def test_verifier_rejects_duplicate_singleton_metadata_fields(
    tmp_path: Path, field: str, value: str
) -> None:
    _create_artifacts(tmp_path, extra_headers=(f"{field}: {value}",))

    result = _run_verifier(tmp_path)

    _assert_rejected(result, f"{field} must appear exactly once")


def test_verifier_rejects_missing_dev_extra_metadata(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, provides_extra=())

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "Provides-Extra must be exactly dev")


def test_verifier_rejects_an_unknown_extra_metadata_value(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, provides_extra=("dev", "docs"))

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "Provides-Extra must be exactly dev")


def test_verifier_rejects_duplicate_dev_extra_metadata(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, provides_extra=("dev", "dev"))

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "Provides-Extra must be exactly dev")


def test_verifier_rejects_a_runtime_dependency(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, requires_dist=("requests",))

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "unapproved Requires-Dist: requests")


def test_verifier_rejects_a_dependency_for_an_unknown_extra(tmp_path: Path) -> None:
    _create_artifacts(
        tmp_path,
        requires_dist=('sphinx; extra == "docs"',),
    )

    result = _run_verifier(tmp_path)

    _assert_rejected(
        result,
        'unapproved Requires-Dist: sphinx; extra == "docs"',
    )


def test_verifier_rejects_an_unexpected_dev_dependency(tmp_path: Path) -> None:
    _create_artifacts(
        tmp_path,
        requires_dist=('requests; extra == "dev"',),
    )

    result = _run_verifier(tmp_path)

    _assert_rejected(
        result,
        'unapproved Requires-Dist: requests; extra == "dev"',
    )


def test_verifier_accepts_only_the_exact_dev_dependency_set(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, requires_dist=EXPECTED_DEV_REQUIREMENTS)

    result = _run_verifier(tmp_path)

    assert result.returncode == 0, result.stderr


def test_verifier_rejects_a_subset_of_the_dev_dependency_set(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, requires_dist=EXPECTED_DEV_REQUIREMENTS[:-1])

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "Requires-Dist must match the exact dev dependency set")


def test_verifier_rejects_a_duplicate_dev_dependency(tmp_path: Path) -> None:
    _create_artifacts(
        tmp_path,
        requires_dist=EXPECTED_DEV_REQUIREMENTS + (EXPECTED_DEV_REQUIREMENTS[0],),
    )

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "Requires-Dist must match the exact dev dependency set")


def test_verifier_rejects_the_wrong_python_requirement(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, requires_python=">=3.10")

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "Requires-Python must be >=3.11")


def test_verifier_rejects_a_wheel_with_a_tampered_record_hash(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, valid_record=False)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "wheel RECORD hash mismatch")


def test_verifier_accepts_safe_artifacts_and_writes_deterministic_checksums(
    tmp_path: Path,
) -> None:
    dist = _create_artifacts(tmp_path)

    result = _run_verifier(tmp_path)

    assert result.returncode == 0, result.stderr
    expected = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(dist.iterdir(), key=lambda item: item.name)
    )
    assert (tmp_path / "SHA256SUMS").read_text(encoding="ascii") == expected


def test_release_workflow_binds_the_tag_to_the_release_event_commit() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "ref: ${{ github.sha }}" in workflow
    assert 'fetch-depth: "0"' in workflow
    assert "EXPECTED_COMMIT: ${{ github.sha }}" in workflow
    assert "refs/tags/v0.1.0^{commit}" in workflow
    assert workflow.index("Check out release event commit") < workflow.index(
        "Verify release tag points to event commit"
    )
    assert "ref: ${{ github.event.release.tag_name }}" not in workflow


def test_release_workflow_keeps_partial_publication_recovery_fail_loud() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    lines = [line.strip() for line in workflow.splitlines()]
    assert not any(line.startswith("skip-existing:") for line in lines)
    assert not any(
        line == "--clobber" or line.startswith("--clobber ") for line in lines
    )
    assert (
        "verify the existing asset hashes before rerunning only failed jobs" in workflow
    )

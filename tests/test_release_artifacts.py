from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import stat
import subprocess
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from email.policy import default
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
EXPECTED_PROJECT_URLS = (
    "Homepage, https://github.com/qingyou0420/trueheart-core",
    "Source, https://github.com/qingyou0420/trueheart-core",
    "Issues, https://github.com/qingyou0420/trueheart-core/issues",
    "Security, https://github.com/qingyou0420/trueheart-core/security/policy",
)
README_PAYLOAD = "# TrueHeart Core\n\nSynthetic release metadata fixture.\n"
SDIST_PYPROJECT = b"""\
[build-system]
requires = ["setuptools==84.0.0"]
build-backend = "setuptools.build_meta"
"""
EXPECTED_PACKAGED_README_LINKS = {
    "https://pypi.org/project/trueheart-core/0.1.0/",
    "https://github.com/qingyou0420/trueheart-openai-agents-example",
    "https://github.com/qingyou0420/trueheart-core/blob/main/examples/basic_memory.py",
    "https://github.com/qingyou0420/trueheart-core/blob/main/docs/architecture.md",
    "https://github.com/qingyou0420/trueheart-core/blob/main/docs/security-guarantees.md",
    "https://github.com/qingyou0420/trueheart-core/blob/main/docs/threat-model.md",
    "https://github.com/qingyou0420/trueheart-core/blob/main/SECURITY.md",
    "https://github.com/qingyou0420/trueheart-core/blob/main/CONTRIBUTING.md",
    "https://github.com/qingyou0420/trueheart-core/blob/main/SUPPORT.md",
}
MARKDOWN_LINK_TARGET = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def _workflow_job_blocks(workflow: str) -> dict[str, str]:
    jobs = workflow.split("\njobs:\n", maxsplit=1)[1]
    headers = list(re.finditer(r"(?m)^  ([a-z0-9-]+):\n", jobs))
    return {
        match.group(1): jobs[match.start() : next_start]
        for match, next_start in zip(
            headers,
            [other.start() for other in headers[1:]] + [len(jobs)],
            strict=True,
        )
    }


def _metadata(
    *,
    version: str = "0.1.0",
    license_expression: str | None = "MIT",
    requires_python: str = ">=3.11",
    requires_dist: tuple[str, ...] = EXPECTED_DEV_REQUIREMENTS,
    provides_extra: tuple[str, ...] = ("dev",),
    description_content_types: tuple[str, ...] = ("text/markdown",),
    project_urls: tuple[str, ...] = EXPECTED_PROJECT_URLS,
    payload: str = README_PAYLOAD,
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
    lines.extend(
        f"Description-Content-Type: {content_type}"
        for content_type in description_content_types
    )
    lines.extend(f"Project-URL: {project_url}" for project_url in project_urls)
    lines.extend(f"Provides-Extra: {extra}" for extra in provides_extra)
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires_dist)
    lines.extend(extra_headers)
    return ("\n".join(lines) + "\n\n" + payload).encode()


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
    pyproject: bytes | None = SDIST_PYPROJECT,
    extra_members: tuple[str, ...] = (),
    link_member: str | None = None,
) -> None:
    with tarfile.open(path, "w:gz") as archive:
        root = "trueheart_core-0.1.0"
        _add_tar_bytes(archive, f"{root}/PKG-INFO", metadata)
        if pyproject is not None:
            _add_tar_bytes(archive, f"{root}/pyproject.toml", pyproject)
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
    description_content_types: tuple[str, ...] = ("text/markdown",),
    project_urls: tuple[str, ...] = EXPECTED_PROJECT_URLS,
    payload: str = README_PAYLOAD,
    extra_headers: tuple[str, ...] = (),
    omit_fields: tuple[str, ...] = (),
    wheel_member: str | None = None,
    wheel_member_mode: int | None = None,
    sdist_members: tuple[str, ...] = (),
    sdist_link: str | None = None,
    sdist_pyproject: bytes | None = SDIST_PYPROJECT,
    wheel_metadata: bytes | None = None,
    sdist_metadata: bytes | None = None,
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
        description_content_types=description_content_types,
        project_urls=project_urls,
        payload=payload,
        extra_headers=extra_headers,
        omit_fields=omit_fields,
    )
    wheel_name = WHEEL_NAME.replace("0.1.0", version)
    sdist_name = SDIST_NAME.replace("0.1.0", version)
    _write_wheel(
        dist / wheel_name,
        metadata if wheel_metadata is None else wheel_metadata,
        valid_record=valid_record,
        extra_member=wheel_member,
        extra_member_mode=wheel_member_mode,
    )
    _write_sdist(
        dist / sdist_name,
        metadata if sdist_metadata is None else sdist_metadata,
        pyproject=sdist_pyproject,
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


def _packaged_readme_payloads(directory: Path) -> dict[str, str]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    dist = _create_artifacts(directory, payload=readme)
    with zipfile.ZipFile(dist / WHEEL_NAME) as wheel:
        wheel_metadata = BytesParser(policy=default).parsebytes(
            wheel.read("trueheart_core-0.1.0.dist-info/METADATA")
        )
    with tarfile.open(dist / SDIST_NAME, mode="r:gz") as sdist:
        pkg_info = sdist.extractfile("trueheart_core-0.1.0/PKG-INFO")
        assert pkg_info is not None
        sdist_metadata = BytesParser(policy=default).parsebytes(pkg_info.read())

    wheel_payload = wheel_metadata.get_payload(decode=True)
    sdist_payload = sdist_metadata.get_payload(decode=True)
    assert isinstance(wheel_payload, bytes)
    assert isinstance(sdist_payload, bytes)
    return {
        "wheel": wheel_payload.decode("utf-8"),
        "sdist": sdist_payload.decode("utf-8"),
    }


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


@pytest.mark.parametrize(
    ("content_types", "message"),
    [
        ((), "Description-Content-Type must appear exactly once"),
        (
            ("text/markdown", "text/markdown"),
            "Description-Content-Type must appear exactly once",
        ),
        (("text/plain",), "Description-Content-Type must be text/markdown"),
    ],
)
def test_verifier_rejects_missing_duplicate_or_non_markdown_content_type(
    tmp_path: Path,
    content_types: tuple[str, ...],
    message: str,
) -> None:
    _create_artifacts(tmp_path, description_content_types=content_types)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, message)


@pytest.mark.parametrize("payload", ["", "   \n", "TrueHeart Core\n"])
def test_verifier_rejects_a_missing_or_non_readme_long_description(
    tmp_path: Path, payload: str
) -> None:
    _create_artifacts(tmp_path, payload=payload)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "long description must begin with # TrueHeart Core")


@pytest.mark.parametrize(
    "project_urls",
    [
        EXPECTED_PROJECT_URLS[:-1],
        EXPECTED_PROJECT_URLS + (EXPECTED_PROJECT_URLS[0],),
        EXPECTED_PROJECT_URLS
        + ("Documentation, https://github.com/qingyou0420/trueheart-core/wiki",),
    ],
)
def test_verifier_rejects_missing_duplicate_or_unknown_project_urls(
    tmp_path: Path, project_urls: tuple[str, ...]
) -> None:
    _create_artifacts(tmp_path, project_urls=project_urls)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "Project-URL must match the exact public URL set")


def test_verifier_rejects_invalid_public_metadata_in_the_sdist(
    tmp_path: Path,
) -> None:
    invalid_sdist_metadata = _metadata(project_urls=EXPECTED_PROJECT_URLS[:-1])
    _create_artifacts(tmp_path, sdist_metadata=invalid_sdist_metadata)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "sdist PKG-INFO: Project-URL")


def test_verifier_rejects_an_sdist_without_a_pyproject(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, sdist_pyproject=None)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "sdist is missing trueheart_core-0.1.0/pyproject.toml")


def test_verifier_rejects_malformed_sdist_pyproject_toml(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, sdist_pyproject=b"[build-system\n")

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "sdist pyproject.toml is not valid TOML")


@pytest.mark.parametrize(
    "pyproject",
    [
        b"""\
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"
""",
        b"""\
[build-system]
requires = ["setuptools==84.0.0", "wheel==0.46.3"]
build-backend = "setuptools.build_meta"
""",
        b"""\
[build-system]
requires = ["setuptools==84.0.0"]
build-backend = "setuptools.build_meta:__legacy__"
""",
        b"""\
[build-system]
requires = ["setuptools==84.0.0"]
build-backend = "setuptools.build_meta"
backend-path = ["backend"]
""",
    ],
)
def test_verifier_rejects_a_non_exact_sdist_build_system(
    tmp_path: Path, pyproject: bytes
) -> None:
    _create_artifacts(tmp_path, sdist_pyproject=pyproject)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "sdist build-system must match the exact backend pin")


def test_verifier_rejects_a_wheel_with_a_tampered_record_hash(tmp_path: Path) -> None:
    _create_artifacts(tmp_path, valid_record=False)

    result = _run_verifier(tmp_path)

    _assert_rejected(result, "wheel RECORD hash mismatch")


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_packaged_readme_records_verified_publication_and_integration(
    tmp_path: Path, artifact: str
) -> None:
    payload = _packaged_readme_payloads(tmp_path)[artifact]
    normalized_payload = " ".join(payload.lower().split())

    assert "not yet published" not in normalized_payload
    assert "after successful package publication" not in normalized_payload
    assert "trueheart core 0.1.0 is available from" in normalized_payload


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_packaged_readme_identifies_the_maintainer_owned_integration_dependency(
    tmp_path: Path, artifact: str
) -> None:
    payload = _packaged_readme_payloads(tmp_path)[artifact]
    normalized_payload = " ".join(payload.lower().split())

    assert "## Integrations" in payload
    assert "maintainer-owned" in normalized_payload
    assert "`openai-agents`" in payload


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_packaged_readme_keeps_model_and_network_activity_outside_core(
    tmp_path: Path, artifact: str
) -> None:
    payload = _packaged_readme_payloads(tmp_path)[artifact]
    normalized_payload = " ".join(payload.lower().split())

    assert "model api calls" in normalized_payload
    assert "network traffic" in normalized_payload
    assert "occur in the host application" in normalized_payload
    assert "outside TrueHeart Core's" in payload
    assert "runtime boundary" in payload


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_packaged_readme_uses_canonical_absolute_https_links(
    tmp_path: Path, artifact: str
) -> None:
    payload = _packaged_readme_payloads(tmp_path)[artifact]

    targets = MARKDOWN_LINK_TARGET.findall(payload)
    non_anchor_targets = [target for target in targets if not target.startswith("#")]
    assert all(target.startswith("https://") for target in non_anchor_targets)
    assert EXPECTED_PACKAGED_README_LINKS <= set(non_anchor_targets)


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
    assert not (tmp_path / "trueheart_core-0.1.0").exists()


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


def test_manual_release_preflight_cannot_enter_a_publication_job() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    trigger_block = workflow.split("\npermissions:", maxsplit=1)[0].split(
        "\non:\n", maxsplit=1
    )[1]

    assert trigger_block == (
        "  workflow_dispatch:\n  release:\n    types: [published]\n"
    )

    jobs = _workflow_job_blocks(workflow)
    assert set(jobs) == {"preflight", "build", "github-release", "pypi"}

    preflight = jobs["preflight"]
    assert "if: ${{ github.event_name == 'workflow_dispatch' }}" in preflight
    assert "permissions: {}" in preflight
    assert 'test "$GITHUB_EVENT_NAME" = "workflow_dispatch"' in preflight
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in preflight
    for forbidden in (
        "uses:",
        "needs:",
        "environment:",
        "id-token:",
        "contents: write",
        "github.token",
        "secrets.",
        "upload-artifact",
        "download-artifact",
        "gh release",
        "gh-action-pypi-publish",
        "python -m build",
        "pip ",
        "curl ",
        "wget ",
    ):
        assert forbidden not in preflight

    release_only = (
        "${{ github.event_name == 'release' && github.event.action == 'published' }}"
    )
    for job_name in ("build", "github-release", "pypi"):
        assert jobs[job_name].count(f"    if: {release_only}\n") == 1
        assert "workflow_dispatch" not in jobs[job_name]

    assert "    needs:" not in jobs["preflight"]
    assert "    needs:" not in jobs["build"]
    assert jobs["github-release"].count("    needs: build\n") == 1
    assert jobs["pypi"].count("    needs: build\n") == 1
    assert jobs["github-release"].count("      contents: write\n") == 1
    assert jobs["pypi"].count("      id-token: write\n") == 1
    assert jobs["pypi"].count("    environment: pypi\n") == 1
    assert workflow.count("      contents: write\n") == 1
    assert workflow.count("      id-token: write\n") == 1
    assert "always()" not in workflow
    assert "continue-on-error" not in workflow


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

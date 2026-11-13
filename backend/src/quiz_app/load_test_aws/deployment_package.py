"""commit済みsourceと再buildしたk6だけを、検証済みtool archiveと束ねる。"""

from __future__ import annotations

import io
import subprocess
import tarfile
from hashlib import sha256
from pathlib import Path

from quiz_app.load_test_aws.deployment import archive_files
from quiz_app.load_test_aws.deployment_models import DeploymentFile, HostDeployment, RuntimePayload

MAXIMUM_ARCHIVE_BYTES = 100 * 1024 * 1024


def _run(argv: tuple[str, ...], repository: Path) -> bytes:
    # shellを通さず、callerでは固定したGit/uv/pnpm引数だけを渡す。
    result = subprocess.run(  # noqa: S603 -- 固定コマンドと検証済みsource commitのみ。
        argv,
        cwd=repository,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=120,
    )
    if len(result.stdout) > MAXIMUM_ARCHIVE_BYTES:
        raise ValueError("aws_load_test_source_archive_too_large")
    return result.stdout


def repository_files(repository: Path, source_commit: str) -> tuple[DeploymentFile, ...]:
    """manifestと異なる作業treeからの配布を拒否し、ignored distは再生成する。"""
    if _run(("git", "rev-parse", "HEAD"), repository).decode().strip() != source_commit or _run(
        ("git", "status", "--porcelain", "--untracked-files=normal"), repository
    ):
        raise ValueError("aws_load_test_committed_clean_source_required")
    source = _run(("git", "archive", "--format=tar", source_commit), repository)
    files: list[DeploymentFile] = []
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or member.size > 20 * 1024 * 1024:
                raise ValueError("aws_load_test_source_archive_file_invalid")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError("aws_load_test_source_archive_file_missing")
            files.append(
                DeploymentFile(path=f"repository/{member.name}", body=extracted.read(), mode=0o644)
            )
    _run(("pnpm", "--dir", "load-test/k6", "build"), repository)
    bundle_paths = tuple(sorted((repository / "load-test/k6/dist").glob("*.js")))
    if {path.name for path in bundle_paths} != {
        "contracts.js",
        "generator-cli.js",
        "generator.js",
        "inspect-fixture.js",
        "launcher.js",
        "metrics-summary.js",
        "summary-smoke.js",
    }:
        raise ValueError("aws_load_test_generator_bundle_set_invalid")
    for path in bundle_paths:
        if path.is_symlink() or path.stat().st_size > 5 * 1024 * 1024:
            raise ValueError("aws_load_test_generator_bundle_invalid")
        files.append(
            DeploymentFile(
                path=f"repository/load-test/k6/dist/{path.name}", body=path.read_bytes(), mode=0o644
            )
        )
    requirements = _run(
        (
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements.txt",
            "--project",
            "backend",
        ),
        repository,
    )
    files.append(DeploymentFile(path="bootstrap/requirements.txt", body=requirements, mode=0o644))
    if _run(("git", "rev-parse", "HEAD"), repository).decode().strip() != source_commit or _run(
        ("git", "status", "--porcelain", "--untracked-files=normal"), repository
    ):
        raise ValueError("aws_load_test_source_changed_during_build")
    return tuple(files)


def host_package(
    config: HostDeployment,
    payload: RuntimePayload,
    source: tuple[DeploymentFile, ...],
    node_archive: bytes,
    uv_archive: bytes,
) -> bytes:
    """host間でapp DB credential、TLS秘密鍵、verifier credentialを共有しない。"""
    if (
        sha256(node_archive).hexdigest() != config.settings.node_archive_sha256
        or sha256(uv_archive).hexdigest() != config.settings.uv_archive_sha256
    ):
        raise ValueError("aws_load_test_tool_archive_mismatch")
    runtime = payload.app if config.role == "app" else payload.runner
    files = tuple(file.model_copy(update={"path": f"run/{file.path}"}) for file in runtime)
    return archive_files(
        (
            *source,
            *files,
            DeploymentFile(
                path="run/inputs/deployment.json", body=config.model_dump_json().encode()
            ),
            DeploymentFile(path="bootstrap/node.tar.xz", body=node_archive),
            DeploymentFile(path="bootstrap/uv.tar.gz", body=uv_archive),
        )
    )

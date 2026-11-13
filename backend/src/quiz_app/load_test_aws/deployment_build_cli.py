"""AWS作成前にcommit固定OCI imageをexportし、image manifest digestを確定する。"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import logfire
from pydantic import TypeAdapter

from quiz_app.load_test.orchestrator_contracts import log_failure, require_private_directory
from quiz_app.load_test_aws.deployment_cli import write_new
from quiz_app.load_test_aws.models import SHA256, SOURCE_COMMIT, StrictModel


class BuiltImage(StrictModel):
    kind: Literal["app", "gateway"]
    archive: str
    archive_sha256: SHA256
    manifest_digest: str


class ImageBuildReceipt(StrictModel):
    format: Literal["load-test-oci-images-v1"] = "load-test-oci-images-v1"
    source_commit: SOURCE_COMMIT
    images: tuple[BuiltImage, ...]


def require_clean_source(repository: Path, expected: str) -> None:
    """build前後に同じsourceを読み、途中変更を正本artifactへ混ぜない。"""
    git = shutil.which("git")
    if git is None:
        raise ValueError("aws_load_test_git_unavailable")
    head = subprocess.check_output(  # noqa: S603 -- 固定Git command。
        (git, "rev-parse", "HEAD"),
        cwd=repository,
        timeout=10,
    )
    dirty = subprocess.check_output(  # noqa: S603 -- 固定Git command。
        (git, "status", "--porcelain", "--untracked-files=normal"),
        cwd=repository,
        timeout=10,
    )
    if head.decode("ascii").strip() != expected or dirty:
        raise ValueError("aws_load_test_committed_clean_source_required")


def build_arguments(
    repository: Path, output: Path, source_commit: str, kind: str
) -> tuple[str, ...]:
    """digestが変わるprovenance indexを省き、amd64 image manifestを直接検証する。"""
    if kind not in {"app", "gateway"} or any(c in str(output) for c in (",", "\n", "\r")):
        raise ValueError("aws_load_test_image_output_invalid")
    TypeAdapter(SOURCE_COMMIT).validate_python(source_commit)
    dockerfile = "backend/Dockerfile" if kind == "app" else "frontend/Dockerfile"
    return (
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--provenance=false",
        "--sbom=false",
        "--file",
        str(repository / dockerfile),
        "--build-arg",
        f"SOURCE_COMMIT={source_commit}",
        "--build-arg",
        "DEPLOYMENT_ENVIRONMENT=aws-demo",
        "--output",
        f"type=oci,compression=gzip,oci-mediatypes=true,dest={output}",
        str(repository),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build immutable OCI images without AWS operations"
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output-directory", required=True, type=Path)
    args = parser.parse_args()
    logfire.configure(send_to_logfire=False, console=False)
    try:
        # OCI inspectorはAWSへ送信せず、local archiveの全blobを検証する。
        from quiz_app.load_test_aws.deployment_images import inspect_oci_archive

        repository = Path(__file__).resolve().parents[4]
        source = TypeAdapter(SOURCE_COMMIT).validate_python(args.source_commit)
        require_private_directory(args.output_directory, repository)
        if any(args.output_directory.iterdir()):
            raise ValueError("aws_load_test_image_output_not_empty")
        require_clean_source(repository, source)
        receipts: list[BuiltImage] = []
        for kind in ("app", "gateway"):
            archive = args.output_directory / f"{kind}.oci.tar"
            log_path = args.output_directory / f"{kind}.build.log"
            descriptor = os.open(
                log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                subprocess.run(  # noqa: S603 -- 固定buildx flagと検証済みsource/pathのみ。
                    build_arguments(repository, archive, source, kind),
                    cwd=repository,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    check=True,
                    timeout=900,
                )
            archive.chmod(0o600)
            validated = inspect_oci_archive(archive, source, kind)
            with archive.open("rb") as stream:
                archive_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            receipts.append(
                BuiltImage(
                    kind=kind,
                    archive=archive.name,
                    archive_sha256=archive_sha256,
                    manifest_digest=validated.manifest_digest,
                )
            )
        require_clean_source(repository, source)
        receipt = ImageBuildReceipt(source_commit=source, images=tuple(receipts))
        write_new(args.output_directory, "images.json", receipt.model_dump_json().encode())
        print('{"status":"images_verified"}')
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        log_failure("aws_load_test_image_build_failed", error)
        print('{"status":"refused"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

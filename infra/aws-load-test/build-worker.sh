#!/usr/bin/env bash
set -euo pipefail
umask 077

repository_root="$(cd "$(dirname "$0")/../.." && pwd -P)"
if [[ $# -ne 1 ]]; then
  echo "usage: build-worker.sh NEW_ABSOLUTE_OUTPUT_DIRECTORY" >&2
  exit 2
fi
output_directory="$1"
if [[ "$output_directory" != /* || -e "$output_directory" ]]; then
  echo "aws_load_test_new_absolute_build_directory_required" >&2
  exit 2
fi
cd "$repository_root"
# AWSへ配置するcodeとlockfileは、review済みcommitのbytesだけを使う。
if [[ -n "$(git status --porcelain -- backend/src backend/pyproject.toml backend/uv.lock infra/aws-load-test)" ]]; then
  echo "aws_load_test_worker_source_not_committed" >&2
  exit 2
fi
mkdir -m 700 "$output_directory"
uv export --project backend --locked --only-group aws-cleanup --no-emit-project \
  --no-header --no-annotate --output-file "$output_directory/requirements.txt" >/dev/null
uv pip install --require-hashes --only-binary :all: \
  --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --target "$output_directory/dependencies" --requirements "$output_directory/requirements.txt"
backend/.venv/bin/python - "$repository_root" "$output_directory" <<'PY'
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from quiz_app.load_test_aws.lifecycle import validate_worker_package

repository = Path(sys.argv[1])
output = Path(sys.argv[2])
sources = [
    *[(path, path.relative_to(output / "dependencies")) for path in (output / "dependencies").rglob("*") if path.is_file()],
    (repository / "backend/src/quiz_app/__init__.py", Path("quiz_app/__init__.py")),
    *[(path, Path("quiz_app/load_test_aws") / path.name) for path in (repository / "backend/src/quiz_app/load_test_aws").glob("*.py")],
]
archive = output / "cleanup-worker.zip"
with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as package:
    for source, name in sorted(sources, key=lambda pair: str(pair[1])):
        if source.is_symlink():
            raise ValueError("aws_load_test_worker_symlink_rejected")
        info = zipfile.ZipInfo(str(name), date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        package.writestr(info, source.read_bytes())
body = archive.read_bytes()
digest = hashlib.sha256(body).hexdigest()
validate_worker_package(body, digest)
(output / "cleanup-worker.sha256").write_text(f"{digest}  cleanup-worker.zip\n")
print(json.dumps({"sha256": digest, "compressed_bytes": len(body)}))
PY

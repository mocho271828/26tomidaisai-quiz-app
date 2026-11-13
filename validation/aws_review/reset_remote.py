"""配置版を変えず、共通排他lock下で非秘密の初期化要求だけをhostへ渡す。"""

import base64
import shlex

from validation.aws_review.models import ResetInput


def reset_host_commands(config: ResetInput) -> str:
    """新workflowを通常updateで配置済みの場合だけ、同じ版のresetを実行する。"""
    payload = base64.b64encode(config.model_dump_json().encode()).decode()
    write_input = shlex.quote(
        "import base64; from pathlib import Path; "
        "from validation.aws_review.host_data import write_private_file; "
        "write_private_file(Path('/etc/gs-app-tomidaisai/review-reset-input.json'), "
        f"base64.b64decode('{payload}', validate=True).decode('utf-8'))"
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
exec 9>/run/lock/gs-review-deploy.lock
flock --nonblock 9
repo=/opt/gs-app-tomidaisai
python="$repo/backend/.venv/bin/python"
if [[ ! -f /etc/gs-app-tomidaisai/review-host-prepared ||
      ! -d "$repo/.git" || ! -x "$python" ||
      ! -f "$repo/validation/aws_review/host_reset.py" ]]; then
  echo review_reset_requires_updated_host >&2
  exit 2
fi
source_commit=$(git -C "$repo" -c safe.directory="$repo" rev-parse HEAD)
source_status=$(git -C "$repo" -c safe.directory="$repo" status --porcelain)
if [[ "$source_commit" != '{config.source_commit}' || -n "$source_status" ]]; then
  echo review_reset_source_mismatch_run_deploy_update_first >&2
  exit 2
fi
cd "$repo"
sudo -H -u ubuntu "$python" -B -c {write_input}
sudo -H -u ubuntu "$python" -B -m validation.aws_review.host_reset \\
  --reset-input /etc/gs-app-tomidaisai/review-reset-input.json
"""

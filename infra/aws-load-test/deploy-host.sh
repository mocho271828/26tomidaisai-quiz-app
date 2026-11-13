#!/bin/bash
# SSM専用。引数は検証済みbucket/key/hashだけで、秘密値をcommandへ含めない。
set -euo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive
export AWS_DEFAULT_REGION=ap-northeast-1
export AWS_EC2_METADATA_DISABLED=false
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
bucket="$1"
key="$2"
digest="$3"
run_id="$4"
role="$5"
[[ "$bucket" =~ ^gs-lt-[0-9a-f]{32}-[0-9]{12}$ ]]
[[ "$run_id" =~ ^[0-9a-f-]{36}$ ]]
[[ "$role" == app || "$role" == runner ]]
[[ "$key" == "deployment/$role/$digest.tar.gz" ]]
[[ "$digest" =~ ^[0-9a-f]{64}$ ]]
exec 3>&1
exec > /var/log/gs-loadtest-install.log 2>&1
apt-get update -qq
apt-get install -y --no-install-recommends python3-boto3 python3-venv ca-certificates curl xz-utils uidmap dbus-user-session
install -d -m 0700 /opt/gs-loadtest
[[ ! -e /opt/gs-loadtest/repository ]]
python3 - "$bucket" "$key" "$digest" "$run_id" <<'PY'
import hashlib
import os
import pathlib
import sys
import tarfile
import boto3
from botocore.config import Config
bucket, key, expected, run_id = sys.argv[1:]
archive = pathlib.Path('/opt/gs-loadtest/package.tar.gz')
client = boto3.client('s3', region_name='ap-northeast-1', config=Config(connect_timeout=5, read_timeout=15, retries={'total_max_attempts': 3}, proxies={}))
client.download_file(bucket, key, str(archive))
if archive.stat().st_size > 100 * 1024 * 1024 or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
    raise SystemExit('deployment_archive_invalid')
with tarfile.open(archive, 'r:gz') as source:
    members = source.getmembers()
    names = [entry.name for entry in members]
    if len(names) != len(set(names)) or sum(entry.size for entry in members) > 300 * 1024 * 1024:
        raise SystemExit('deployment_archive_invalid')
    for entry in members:
        path = pathlib.PurePosixPath(entry.name)
        if not entry.isfile() or path.is_absolute() or '..' in path.parts or path.parts[0] not in {'repository', 'run', 'bootstrap'}:
            raise SystemExit('deployment_archive_invalid')
    source.extractall('/opt/gs-loadtest', members=members, filter='data')
archive.unlink()
PY
python3 - <<'PY'
import hashlib
import json
import pathlib
import tarfile
root = pathlib.Path('/opt/gs-loadtest')
config = json.loads((root / 'run/inputs/deployment.json').read_bytes())
settings = config['settings']
for name, expected, member, destination in (
    ('node.tar.xz', settings['node_archive_sha256'], f"node-v{settings['node_version']}-linux-x64/bin/node", '/usr/bin/node'),
    ('uv.tar.gz', settings['uv_archive_sha256'], 'uv-x86_64-unknown-linux-gnu/uv', '/opt/gs-loadtest/uv'),
):
    archive = root / 'bootstrap' / name
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise SystemExit('deployment_tool_invalid')
    with tarfile.open(archive, 'r:*') as source:
        entry = source.getmember(member)
        if not entry.isfile() or entry.size > 150 * 1024 * 1024:
            raise SystemExit('deployment_tool_invalid')
        with source.extractfile(entry) as stream:
            pathlib.Path(destination).write_bytes(stream.read())
    pathlib.Path(destination).chmod(0o755)
(root / 'docker-version').write_text(settings['docker_package_version'])
(root / 'compose-version').write_text(settings['docker_compose_package_version'])
PY
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<'DOCKER'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER
apt-get update -qq
docker_version="$(cat /opt/gs-loadtest/docker-version)"
compose_version="$(cat /opt/gs-loadtest/compose-version)"
apt-get install -y --no-install-recommends "docker-ce=$docker_version" "docker-ce-cli=$docker_version" containerd.io docker-buildx-plugin "docker-compose-plugin=$compose_version"
systemctl enable --now docker
install -d -m 0755 /var/lib/gs-loadtest
[[ ! -e /var/lib/gs-loadtest/home ]]
groupadd --gid 1001 gsloadtest
useradd --uid 1001 --gid 1001 --groups docker --home-dir /var/lib/gs-loadtest/home --create-home --shell /bin/bash gsloadtest
chmod 0700 /var/lib/gs-loadtest/home
loginctl enable-linger gsloadtest
systemctl start user@1001.service
install -d -m 0755 /etc/gs-loadtest/docker-cli
# fault controllerとgeneratorは、credential helperのない空directoryを検証する。
[[ -z "$(find /etc/gs-loadtest/docker-cli -mindepth 1 -maxdepth 1 -print -quit)" ]]
/opt/gs-loadtest/uv venv /opt/gs-loadtest/venv --python /usr/bin/python3
/opt/gs-loadtest/uv pip sync --python /opt/gs-loadtest/venv/bin/python --require-hashes /opt/gs-loadtest/bootstrap/requirements.txt
/opt/gs-loadtest/venv/bin/python - <<'PY'
import pathlib
import site
pathlib.Path(site.getsitepackages()[0], 'gs-loadtest-source.pth').write_text('/opt/gs-loadtest/repository/backend/src\n')
PY
install -d -m 0700 -o gsloadtest -g gsloadtest "/var/lib/gs-loadtest/$run_id"
cp -a /opt/gs-loadtest/run/. "/var/lib/gs-loadtest/$run_id/"
chown -R gsloadtest:gsloadtest "/var/lib/gs-loadtest/$run_id"
find "/var/lib/gs-loadtest/$run_id" -type d -exec chmod 0700 {} +
find "/var/lib/gs-loadtest/$run_id" -type f -exec chmod 0600 {} +
chmod 0755 /opt/gs-loadtest
chmod -R a+rX /opt/gs-loadtest/venv
find /opt/gs-loadtest/repository -type d -exec chmod 0755 {} +
find /opt/gs-loadtest/repository -type f -exec chmod 0644 {} +
# source全体はroot所有を維持し、正式launcherが実行者所有を要求するbundleだけを渡す。
chown -R gsloadtest:gsloadtest /opt/gs-loadtest/repository/load-test/k6/dist
rm -rf /opt/gs-loadtest/run /opt/gs-loadtest/bootstrap /opt/gs-loadtest/docker-version /opt/gs-loadtest/compose-version
printf '{"status":"installed"}\n' >&3

#!/usr/bin/env bash
# 専用Ubuntu EC2をSSMから準備する。DB初期化・アプリ起動は行わない。
set -Eeuo pipefail

if [[ $# -ne 5 ]]; then
  echo 'Usage: aws-rehearsal-host-prepare.sh BUCKET BUNDLE_KEY BUNDLE_SHA256 SOURCE_COMMIT API_IMAGE' >&2
  exit 2
fi
bucket="$1"
bundle_key="$2"
bundle_sha256="$3"
source_commit="$4"
api_image="$5"
[[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]
[[ "$bundle_key" =~ ^[a-z0-9-]+/source\.bundle$ ]]
[[ "$bundle_sha256" =~ ^[a-f0-9]{64}$ ]]
[[ "$source_commit" =~ ^[a-f0-9]{40}$ ]]
[[ "$api_image" =~ ^[0-9]{12}\.dkr\.ecr\.ap-northeast-1\.amazonaws\.com/gs-rehearsal-[a-z0-9-]+/api@sha256:[a-f0-9]{64}$ ]]
[[ $(id -u) -eq 0 ]]
[[ $(id -u ubuntu) -eq 1000 && $(id -g ubuntu) -eq 1000 ]]
[[ ! -e /opt/gs-app-tomidaisai ]]
umask 077
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq ca-certificates curl git python3-venv unzip gnupg groff less
# Ubuntuの有効repositoryに依存せず、公式installerで署名検証して固定versionを入れる。
aws_cli_installer=$(mktemp)
curl --fail --silent --show-error --retry 3 --max-time 60 \
  https://awscli.amazonaws.com/v2/install.sh -o "$aws_cli_installer"
bash "$aws_cli_installer" --system --version 2.36.49
rm "$aws_cli_installer"
install -m 0755 -d /etc/apt/keyrings
curl --fail --silent --show-error --retry 3 --max-time 60 \
  https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc
cat >/etc/apt/sources.list.d/docker.sources <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
chmod 0644 /etc/apt/sources.list.d/docker.sources
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
usermod -aG docker ubuntu

install -d -o ubuntu -g ubuntu -m 0700 /etc/gs-app-tomidaisai /var/lib/quiz-rehearsal-backups
install -d -o ubuntu -g ubuntu -m 0700 /var/lib/quiz-rehearsal-backups/data
aws --region ap-northeast-1 s3 cp "s3://${bucket}/${bundle_key}" /opt/rehearsal-source.bundle --only-show-errors
printf '%s  %s\n' "$bundle_sha256" /opt/rehearsal-source.bundle | sha256sum --check --status
git clone /opt/rehearsal-source.bundle /opt/gs-app-tomidaisai
git -C /opt/gs-app-tomidaisai checkout --detach "$source_commit"
[[ $(git -C /opt/gs-app-tomidaisai rev-parse HEAD) == "$source_commit" ]]
chown -R ubuntu:ubuntu /opt/gs-app-tomidaisai
rm /opt/rehearsal-source.bundle

# imageに固定されたuvを取り出し、別installerのversion差を持ち込まない。
aws --region ap-northeast-1 ecr get-login-password |
  docker login --username AWS --password-stdin "${api_image%%/*}"
docker pull "$api_image"
container_id=$(docker create "$api_image")
trap 'docker rm "$container_id" >/dev/null' EXIT
docker cp "${container_id}:/usr/local/bin/uv" /usr/local/bin/uv
chmod 0755 /usr/local/bin/uv
sudo -H -u ubuntu uv sync --project /opt/gs-app-tomidaisai/backend --python /usr/bin/python3 --frozen --no-dev

curl --fail --silent --show-error --retry 3 --max-time 60 \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
  -o /etc/gs-app-tomidaisai/rds-ca.pem
chown ubuntu:ubuntu /etc/gs-app-tomidaisai/rds-ca.pem
chmod 0644 /etc/gs-app-tomidaisai/rds-ca.pem
printf 'host_prepared=true\n'
hostname -f
docker compose version --short
sha256sum /etc/gs-app-tomidaisai/rds-ca.pem

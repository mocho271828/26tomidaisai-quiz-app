from pathlib import Path

from quiz_app.schema_revision import SCHEMA_REVISION

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AWS_DEMO_DIRECTORY = REPOSITORY_ROOT / "deploy" / "aws-demo"

LOCAL_ONLY_VALUES = (
    "demo-2026",
    "local-demo-only",
    "local-only-database-password",
    "local-only-redis-password",
    "local-demo-session-secret-change-me",
    "replace-for-local-review",
    "60000000-0000-4000-8000-000000000001",
)
SECRET_ENVIRONMENT_KEYS = (
    "DATABASE_URL",
    "REDIS_URL",
    "SESSION_SECRET",
)


def _compose_service_block(compose: str, service_name: str) -> str:
    lines = compose.splitlines(keepends=True)
    start = lines.index(f"  {service_name}:\n")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if (line.startswith("  ") and not line.startswith("    ")) or (
            line.strip() and not line.startswith(" ")
        ):
            end = index
            break
    return "".join(lines[start:end])


def test_aws_compose_is_standalone_and_has_no_mutating_service() -> None:
    compose = (AWS_DEMO_DIRECTORY / "compose.yaml").read_text(encoding="utf-8")
    api_service = compose.split("  api:\n", maxsplit=1)[1].split("  gateway:\n", maxsplit=1)[0]
    gateway_service = compose.split("  gateway:\n", maxsplit=1)[1].split(
        "  preflight:\n", maxsplit=1
    )[0]

    assert "build:" not in compose
    assert "migrate:" not in compose
    assert "seed:" not in compose
    assert "alembic" not in compose
    assert "postgres:" not in compose
    assert "redis:" not in compose
    assert "./.local" not in compose
    assert "AWS_DEMO_API_IMAGE" in compose
    assert "AWS_DEMO_GATEWAY_IMAGE" in compose
    assert '"127.0.0.1:${AWS_DEMO_GATEWAY_PORT:' in compose
    assert compose.count("pull_policy: never") == 3
    assert compose.count('restart: "no"') == 3
    assert compose.count("cap_add:") == 1
    assert compose.count("- NET_BIND_SERVICE") == 1
    assert "cap_add:" not in api_service
    assert "cap_add:\n      - NET_BIND_SERVICE" in gateway_service
    assert compose.count("format: raw") == 2
    assert compose.count("create_host_path: false") == 2
    assert "/run/secrets/aws-demo-postgres-ca.pem" in compose
    assert "/api/health/ready" in compose
    assert "AWS_DEMO_EVENT_SLUG" not in compose
    assert "AWS_DEMO_EXPECTED_EVENT_ID: ${AWS_DEMO_EVENT_ID:" in compose
    assert "AWS_DEMO_ROUND_SCOPE: ${AWS_DEMO_ROUND_SCOPE:" in compose
    assert "API_INSTANCE_ID: api-single" in compose
    assert "AWS_DEMO_ROUND_SCOPE: single-round" in compose
    assert "api-secondary:" not in compose
    assert "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY" not in compose


def test_load_test_compose_is_additive_hardened_and_uses_one_backup_bind() -> None:
    base = (AWS_DEMO_DIRECTORY / "compose.yaml").read_text(encoding="utf-8")
    override = (AWS_DEMO_DIRECTORY / "compose.load-test.yaml").read_text(encoding="utf-8")
    combined = f"{base}\n{override}"

    assert "build:" not in override
    assert "migrate:" not in override
    assert "seed:" not in override
    assert "api-secondary:" in override
    assert "image: ${AWS_DEMO_API_IMAGE:" in override
    assert override.count("AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY:") == 2
    assert override.count("target: /var/lib/quiz-round-backups") == 2
    assert combined.count("target: /run/secrets/aws-demo-postgres-ca.pem") == 4
    assert override.count("pull_policy: never") == 8
    assert override.count("read_only: true") >= 12
    assert "cap_drop:\n      - ALL" in override
    assert "cap_add:" not in override
    assert "no-new-privileges:true" in override
    assert override.count("AWS_DEMO_RUNTIME_UID:") == 8
    assert override.count("AWS_DEMO_RUNTIME_GID:") == 8
    assert 'user: "${AWS_DEMO_RUNTIME_UID:' in override
    assert "API_INSTANCE_ID: api-primary" in override
    assert "API_INSTANCE_ID: api-secondary" in override
    assert override.count("/api/health/live") == 2
    assert "/api/health/ready" not in override
    assert "ports:" not in override
    assert '"127.0.0.1:${AWS_DEMO_GATEWAY_PORT:' in base
    assert "network_mode:" not in override
    assert "privileged:" not in override
    assert "/var/run/docker.sock" not in override
    assert "internal: true" in override
    assert override.count("- metrics") == 8
    assert 'profiles: ["node-metrics"]' in override
    assert "--collector.disable-defaults" in override
    assert "--storage.tsdb.retention.time=14d" in override
    assert "--storage.tsdb.retention.size" not in override
    assert "AWS_DEMO_LOAD_TEST_MANIFEST_FILE:" in override
    assert "target: /run/load-test/manifest.json" in override
    assert "target: /run/load-test/manifest.json\n        read_only: true" in override
    assert "--repository-root\n      - /app" in override
    for service in (
        "prometheus:",
        "postgres-exporter:",
        "redis-exporter:",
        "node-exporter:",
        "metrics-export:",
    ):
        assert service in override
    assert "entrypoint:\n      - caddy" in override
    assert (
        "command:\n      - run\n      - --config\n      - /etc/caddy/Caddyfile.load-test"
        in override
    )


def test_load_test_compose_routes_declared_redis_clients_through_private_toxiproxy() -> None:
    override = (AWS_DEMO_DIRECTORY / "compose.load-test.yaml").read_text(encoding="utf-8")
    api = _compose_service_block(override, "api")
    api_secondary = _compose_service_block(override, "api-secondary")
    preflight = _compose_service_block(override, "preflight")
    toxiproxy = _compose_service_block(override, "toxiproxy")
    fault_control_client = _compose_service_block(override, "fault-control-client")
    redis_exporter = _compose_service_block(override, "redis-exporter")

    assert (
        "正式controllerはseal済みminimal Composeを使う。この定義はpreflight/diagnostic専用。"
    ) in override

    assert "image: ${AWS_DEMO_TOXIPROXY_IMAGE:" in toxiproxy
    assert "pull_policy: never" in toxiproxy
    assert 'user: "${AWS_DEMO_RUNTIME_UID:' in toxiproxy and "${AWS_DEMO_RUNTIME_GID:" in toxiproxy
    assert "read_only: true" in toxiproxy
    assert "cap_drop:\n      - ALL" in toxiproxy
    assert "security_opt:\n      - no-new-privileges:true" in toxiproxy
    assert 'restart: "no"' in toxiproxy
    assert "ports:" not in toxiproxy
    assert "expose:" not in toxiproxy
    assert "source: ${AWS_DEMO_TOXIPROXY_CONFIG_FILE:" in toxiproxy
    assert "target: /run/load-test/toxiproxy.json" in toxiproxy
    assert "target: /run/load-test/toxiproxy.json\n        read_only: true" in toxiproxy
    assert "create_host_path: false" in toxiproxy
    assert (
        "command:\n"
        "      - -host\n"
        "      - 172.30.251.2\n"
        "      - -port\n"
        '      - "8474"\n'
        "      - -config\n"
        "      - /run/load-test/toxiproxy.json"
    ) in toxiproxy
    assert "http://172.30.251.2:8474" in toxiproxy
    assert "gs-app-tomidaisai-aws-demo-redis" in toxiproxy
    assert "default: {}" in toxiproxy
    assert "fault-data:" in toxiproxy
    assert "ipv4_address: 172.30.250.2" in toxiproxy
    assert "fault-control:" in toxiproxy
    assert "ipv4_address: 172.30.251.2" in toxiproxy
    assert '- "${AWS_DEMO_REDIS_HOSTNAME:?AWS_DEMO_REDIS_HOSTNAME must be explicit}"' in toxiproxy

    for non_controller_service in (
        "api",
        "api-secondary",
        "gateway",
        "preflight",
        "prometheus",
        "postgres-exporter",
        "redis-exporter",
        "node-exporter",
        "metrics-export",
    ):
        assert "fault-control" not in _compose_service_block(override, non_controller_service)

    assert 'profiles: ["fault-control"]' in fault_control_client
    assert "image: ${AWS_DEMO_API_IMAGE:" in fault_control_client
    assert "pull_policy: never" in fault_control_client
    assert 'user: "${AWS_DEMO_RUNTIME_UID:' in fault_control_client
    assert "${AWS_DEMO_RUNTIME_GID:" in fault_control_client
    assert (
        "entrypoint:\n      - python\n      - -m\n      - quiz_app.load_test.fault_control_probe"
    ) in fault_control_client
    assert "command:\n      - probe" in fault_control_client
    assert (
        "environment:\n      AWS_DEMO_TOXIPROXY_CONFIG_SHA256: ${AWS_DEMO_TOXIPROXY_CONFIG_SHA256:"
    ) in fault_control_client
    assert "source: ${AWS_DEMO_TOXIPROXY_CONFIG_FILE:" in fault_control_client
    assert "target: /run/load-test/toxiproxy.json" in fault_control_client
    assert "target: /run/load-test/toxiproxy.json\n        read_only: true" in fault_control_client
    assert "create_host_path: false" in fault_control_client
    assert "networks:\n      - fault-control" in fault_control_client
    assert "fault-data" not in fault_control_client
    assert "- default" not in fault_control_client
    assert "- metrics" not in fault_control_client
    assert "env_file:" not in fault_control_client
    assert "ports:" not in fault_control_client
    assert "expose:" not in fault_control_client
    assert "/run/secrets" not in fault_control_client
    assert "/var/run/docker.sock" not in fault_control_client
    for secret_key in SECRET_ENVIRONMENT_KEYS:
        assert secret_key not in fault_control_client
    assert "read_only: true" in fault_control_client
    assert "cap_drop:\n      - ALL" in fault_control_client
    assert "security_opt:\n      - no-new-privileges:true" in fault_control_client
    assert 'restart: "no"' in fault_control_client
    assert "toxiproxy:\n        condition: service_healthy" in fault_control_client

    redis_route = (
        '- "${AWS_DEMO_REDIS_HOSTNAME:?AWS_DEMO_REDIS_HOSTNAME must be explicit}=172.30.250.2"'
    )
    for client in (api, api_secondary, redis_exporter):
        assert "- fault-data" in client
        assert "extra_hosts:" in client
        assert redis_route in client
        assert "toxiproxy:\n        condition: service_healthy" in client
        assert "fault-control" not in client

    # preflightは--no-depsで実Redisへ直接到達し、proxy経路とは独立して検証する。
    assert "AWS_DEMO_TOXIPROXY_CONFIG_SHA256:" in preflight
    assert 'user: "${AWS_DEMO_RUNTIME_UID:' in preflight
    assert "source: ${AWS_DEMO_TOXIPROXY_CONFIG_FILE:" in preflight
    assert "target: /run/load-test/toxiproxy.json" in preflight
    assert "fault-data" not in preflight
    assert "fault-control" not in preflight
    assert "extra_hosts:" not in preflight
    assert "depends_on:" not in preflight

    network_definitions = override.split("\nnetworks:\n", maxsplit=1)[1]
    assert "fault-data:\n    internal: true" in network_definitions
    assert "subnet: 172.30.250.0/29" in network_definitions
    assert "fault-control:\n    internal: true" in network_definitions
    assert "com.docker.network.bridge.gateway_mode_ipv4: isolated" in network_definitions
    assert "subnet: 172.30.251.0/29" in network_definitions


def test_load_test_release_templates_require_toxiproxy_contract() -> None:
    required_fields = (
        "AWS_DEMO_TOXIPROXY_IMAGE=",
        "AWS_DEMO_TOXIPROXY_CONFIG_FILE=",
        "AWS_DEMO_TOXIPROXY_CONFIG_SHA256=",
        "AWS_DEMO_REDIS_HOSTNAME=",
    )

    for filename in (
        "release.load-test.env.template",
        "rollback.load-test.env.template",
    ):
        template = (AWS_DEMO_DIRECTORY / filename).read_text(encoding="utf-8")
        for field in required_fields:
            assert template.count(field) == 1


def test_all_app_caddyfiles_deny_internal_and_removed_public_routes() -> None:
    normal = (REPOSITORY_ROOT / "infra" / "Caddyfile").read_text(encoding="utf-8")
    load_test = (REPOSITORY_ROOT / "infra" / "Caddyfile.load-test").read_text(encoding="utf-8")
    local_tls = (REPOSITORY_ROOT / "infra" / "local-tls" / "Caddyfile").read_text(encoding="utf-8")
    multi_api = (REPOSITORY_ROOT / "infra" / "Caddyfile.local-multi-api").read_text(
        encoding="utf-8"
    )
    local_tls_multi_api = (
        REPOSITORY_ROOT / "infra" / "local-tls" / "Caddyfile.multi-api"
    ).read_text(encoding="utf-8")

    for caddyfile in (normal, load_test, local_tls, multi_api, local_tls_multi_api):
        assert "@internal path /internal /internal/*" in caddyfile
        assert "handle @internal" in caddyfile
        assert "@spa_entrypoints path / /operator /operator/ /moderator /moderator/\n" in caddyfile
        assert "/result" not in caddyfile
        assert "handle @spa_entrypoints" in caddyfile
        assert "try_files {path} /index.html" not in caddyfile
        assert caddyfile.count("respond 404") == 2
        assert all(not line.startswith(" ") for line in caddyfile.splitlines())

    expected_load_directives = (
        "reverse_proxy api:8000 api-secondary:8000",
        "lb_policy least_conn",
        "health_uri /api/health/live",
        "health_interval 2s",
        "health_timeout 1s",
        "health_fails 3",
        "health_passes 2",
        "fail_duration 2s",
        "max_fails 5",
        "lb_try_duration 1s",
        "lb_try_interval 100ms",
    )
    for directive in expected_load_directives:
        assert directive in load_test
    assert "admin off" in load_test
    assert ":2019 {\n\tmetrics\n}" in load_test


def test_load_prometheus_configuration_is_fixed_internal_and_has_no_remote_write() -> None:
    config = (REPOSITORY_ROOT / "infra" / "prometheus.load-test.yml").read_text(encoding="utf-8")

    assert "scrape_interval: 1s" in config
    assert "scrape_timeout: 900ms" in config
    assert "remote_write" not in config
    for target in (
        "api:8000",
        "api-secondary:8000",
        "gateway:2019",
        "postgres-exporter:9187",
        "redis-exporter:9121",
        "node-exporter:9100",
    ):
        assert target in config


def test_public_aws_templates_do_not_contain_local_values_or_secret_placeholders() -> None:
    public_templates = [
        AWS_DEMO_DIRECTORY / "release.env.template",
        AWS_DEMO_DIRECTORY / "rollback.env.template",
        AWS_DEMO_DIRECTORY / "runtime.nonsecret.env.template",
        AWS_DEMO_DIRECTORY / "release.load-test.env.template",
        AWS_DEMO_DIRECTORY / "rollback.load-test.env.template",
        AWS_DEMO_DIRECTORY / "runtime.load-test.nonsecret.env.template",
        AWS_DEMO_DIRECTORY / "release.rehearsal.env.template",
        AWS_DEMO_DIRECTORY / "rollback.rehearsal.env.template",
        AWS_DEMO_DIRECTORY / "runtime.rehearsal.nonsecret.env.template",
    ]

    combined = "\n".join(path.read_text(encoding="utf-8") for path in public_templates)
    for local_value in LOCAL_ONLY_VALUES:
        assert local_value not in combined
    for secret_key in SECRET_ENVIRONMENT_KEYS:
        assert f"{secret_key}=" not in combined


def test_release_templates_pin_current_schema_without_fake_digest() -> None:
    for filename in (
        "release.env.template",
        "rollback.env.template",
        "release.load-test.env.template",
        "rollback.load-test.env.template",
        "release.rehearsal.env.template",
        "rollback.rehearsal.env.template",
    ):
        template = (AWS_DEMO_DIRECTORY / filename).read_text(encoding="utf-8")

        assert f"AWS_DEMO_SCHEMA_REVISION={SCHEMA_REVISION}" in template
        assert "@sha256:" not in template
        assert "AWS_DEMO_API_IMAGE=\n" in template
        assert "AWS_DEMO_GATEWAY_IMAGE=\n" in template
        assert "AWS_DEMO_POSTGRES_CA_FILE=\n" in template
        assert "AWS_DEMO_POSTGRES_CA_SHA256=\n" in template
        assert "AWS_DEMO_EXECUTION_HOST=\n" in template
        assert "AWS_DEMO_DOCKER_CONTEXT=\n" in template
        assert "AWS_DEMO_DOCKER_ENDPOINT=unix:///var/run/docker.sock" in template
        assert "AWS_DEMO_RUNTIME_UID=\n" in template
        assert "AWS_DEMO_RUNTIME_GID=\n" in template
        assert "AWS_DEMO_EVENT_SLUG" not in template
        expected_scope = "load-test-ten-round" if "load-test" in filename else "single-round"
        if "rehearsal" in filename:
            expected_scope = "rehearsal-six-round"
        assert f"AWS_DEMO_ROUND_SCOPE={expected_scope}" in template
        if expected_scope != "single-round":
            assert "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY=\n" in template
        else:
            assert "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY" not in template


def test_nonsecret_runtime_template_uses_explicit_disabled_sentinels() -> None:
    template = (AWS_DEMO_DIRECTORY / "runtime.nonsecret.env.template").read_text(encoding="utf-8")

    assert "DEMO_OPERATOR_USERNAME=managed-in-database" in template
    assert "DEMO_OPERATOR_PASSWORD=unused" in template
    assert "ROUND_BACKUP_ENCRYPTION_KEY=disabled" in template
    assert "ROUND_BACKUP_STORAGE_MODE=disabled" in template
    assert "AWS_DEMO_ROUND_SCOPE=single-round" in template
    assert "API_INSTANCE_ID=api-single" in template
    assert "PGSSLROOTCERT=/run/secrets/aws-demo-postgres-ca.pem" in template


def test_load_test_runtime_template_requires_external_backup_key() -> None:
    template = (AWS_DEMO_DIRECTORY / "runtime.load-test.nonsecret.env.template").read_text(
        encoding="utf-8"
    )

    assert "AWS_DEMO_ROUND_SCOPE=load-test-ten-round" in template
    assert "API_INSTANCE_ID=api-primary" in template
    assert "ROUND_BACKUP_STORAGE_MODE=filesystem" in template
    assert "ROUND_BACKUP_DIRECTORY=/var/lib/quiz-round-backups" in template
    assert "ROUND_BACKUP_ENCRYPTION_KEY=" not in template


def test_rehearsal_keeps_private_api_security_and_requires_external_backup_key() -> None:
    override = (AWS_DEMO_DIRECTORY / "compose.rehearsal.yaml").read_text(encoding="utf-8")
    template = (AWS_DEMO_DIRECTORY / "runtime.rehearsal.nonsecret.env.template").read_text(
        encoding="utf-8"
    )
    for prohibited in (
        "ports:",
        "networks:",
        "extra_hosts:",
        "cap_add:",
        "privileged:",
        "preflight:",
        "healthcheck:",
        "toxiproxy",
        "load-test",
        "build:",
    ):
        assert prohibited not in override
    assert 'user: "${AWS_DEMO_RUNTIME_UID:' in override
    assert "target: /var/lib/quiz-round-backups" in override
    assert "create_host_path: false" in override
    assert "ROUND_BACKUP_STORAGE_MODE=filesystem" in template
    assert "ROUND_BACKUP_ENCRYPTION_KEY=" not in template


def test_preflight_wrapper_has_no_deployment_or_database_mutation_command() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "aws-demo-preflight.sh").read_text(encoding="utf-8")

    prohibited_command_fragments = (
        "docker compose up",
        "docker compose build",
        "docker image pull",
        "docker image push",
        "alembic upgrade",
        "terraform apply",
    )
    for prohibited_command in prohibited_command_fragments:
        assert prohibited_command not in script
    assert "--frozen --offline --no-sync --no-env-file" in script
    assert "--pull never preflight" in script
    assert 'inspect_release_images "${release_file}"' in script
    assert 'inspect_release_images "${rollback_file}"' in script
    assert "reject_aws_demo_ambient_environment" in script
    assert "verify_aws_demo_execution_target" in script
    assert "DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG" in (
        script.replace("\\\n    ", " ")
    )
    assert "compgen -A variable COMPOSE_" in script
    assert 'read_release_field round-scope "${release_file}"' in script
    assert 'compose_files+=(--file "${AWS_PREFLIGHT_LOAD_TEST_COMPOSE_FILE}")' in script


def test_aws_runbook_documents_safe_scope_switch_and_rollback() -> None:
    runbook = (REPOSITORY_ROOT / "docs" / "AWS_REHEARSAL.md").read_text(encoding="utf-8")
    transition = runbook.split("## デモ配置の切替と復帰\n", 1)[1].split("\n## ", 1)[0]

    assert "single-round" in transition and "load-test-ten-round" in transition
    assert "deploy/aws-demo/compose.yaml" in transition
    assert "deploy/aws-demo/compose.load-test.yaml" in transition
    assert "--rollback-operation" in transition
    assert "up -d --no-build --pull never --remove-orphans --wait" in transition
    assert "`api-secondary`が不在であることを確認する" in transition
    assert "named volumeやhost backup directoryは削除しない" in transition


def test_aws_docs_fix_external_path_and_backup_inventory_contract() -> None:
    documentation = (REPOSITORY_ROOT / "docs" / "AWS_DEMO_REQUIRED_SECRETS.md").read_text(
        encoding="utf-8"
    )

    assert "全ancestor" in documentation
    assert "single-link" in documentation
    assert "rb-<32 lowercase hex>.(advance_round|complete_event).fernet" in documentation
    assert "entropyを証明" in documentation
    for rejected_entry in ("temporary file", "subdirectory", "symlink", "hard link"):
        assert rejected_entry in documentation


def test_dockerfiles_expose_only_nonsecret_release_metadata_labels() -> None:
    backend_dockerfile = (REPOSITORY_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    frontend_dockerfile = (REPOSITORY_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")

    for dockerfile in (backend_dockerfile, frontend_dockerfile):
        assert "ARG SOURCE_COMMIT=unversioned" in dockerfile
        assert "ARG DEPLOYMENT_ENVIRONMENT=local-demo" in dockerfile
        assert "org.opencontainers.image.revision" in dockerfile
        assert "io.tomidaisai.deployment-environment" in dockerfile
        for secret_key in SECRET_ENVIRONMENT_KEYS:
            assert f"ARG {secret_key}" not in dockerfile

    assert "VITE_DEMO_EVENT_SLUG" not in frontend_dockerfile
    assert "VITE_DEMO_EVENT_ID" not in frontend_dockerfile
    assert "io.tomidaisai.event-slug" not in frontend_dockerfile
    assert "io.tomidaisai.event-id" not in frontend_dockerfile
    assert "COPY infra/Caddyfile /etc/caddy/Caddyfile" in frontend_dockerfile
    assert (
        "COPY infra/Caddyfile.local-multi-api /etc/caddy/Caddyfile.multi-api" in frontend_dockerfile
    )
    assert "COPY infra/Caddyfile.load-test /etc/caddy/Caddyfile.load-test" in frontend_dockerfile


def test_review_host_can_read_only_its_own_cloudformation_stack_resources() -> None:
    template = (REPOSITORY_ROOT / "infra/aws-rehearsal/workload.yaml").read_text()
    app_role = template.split("  AppRole:\n", 1)[1].split("  AppInstanceProfile:\n", 1)[0]
    assert app_role.count("cloudformation:") == 1
    assert (
        "              - !If\n"
        "                - IsManualReview\n"
        "                - Effect: Allow\n"
        "                  Action: cloudformation:DescribeStackResource\n"
        "                  Resource: !Ref 'AWS::StackId'\n"
        "                - !Ref 'AWS::NoValue'\n"
    ) in app_role

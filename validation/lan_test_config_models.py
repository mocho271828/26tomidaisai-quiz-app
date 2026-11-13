"""LAN presetが必要とするComposeの境界だけを検証する。"""

from typing import Literal

from pydantic import AliasPath, BaseModel, ConfigDict, Field, RootModel, SecretStr


class BoundaryModel(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class RuntimeValues(RootModel[dict[str, str]]):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)


class Mount(BoundaryModel):
    type: Literal["volume", "bind", "tmpfs"]
    source: str = ""
    target: str
    read_only: bool = False


class Port(BoundaryModel):
    host_ip: str = ""
    published: str
    target: int
    protocol: str = "tcp"


class ApiEnvironment(BoundaryModel):
    app_env: Literal["local-demo"] = Field(alias="APP_ENV")
    database_url: SecretStr = Field(alias="DATABASE_URL")
    session_secret: SecretStr = Field(alias="SESSION_SECRET")
    username: str = Field(alias="DEMO_OPERATOR_USERNAME")
    password: SecretStr = Field(alias="DEMO_OPERATOR_PASSWORD")
    auth: Literal["true", "false"] = Field(alias="ENABLE_DEMO_AUTH")
    backup_mode: Literal["filesystem", "disabled"] = Field(alias="ROUND_BACKUP_STORAGE_MODE")
    backup_directory: str = Field(alias="ROUND_BACKUP_DIRECTORY")
    backup_key: SecretStr = Field(alias="ROUND_BACKUP_ENCRYPTION_KEY")
    seed: str = Field(alias="ENABLE_DEMO_SEED")
    secure: str = Field(alias="SESSION_COOKIE_SECURE")
    origins: str = Field(alias="REALTIME_ALLOWED_ORIGINS")
    logfire: str = Field(alias="LOGFIRE_SEND_TO_LOGFIRE")
    redis_url: SecretStr = Field(alias="REDIS_URL")
    ssl_mode: str | None = Field(default=None, alias="PGSSLMODE")
    ssl_root: str | None = Field(default=None, alias="PGSSLROOTCERT")

    def retained(self) -> dict[str, str]:
        return {
            "APP_ENV": self.app_env,
            "SESSION_SECRET": self.session_secret.get_secret_value(),
            "DEMO_OPERATOR_USERNAME": self.username,
            "DEMO_OPERATOR_PASSWORD": self.password.get_secret_value(),
            "ENABLE_DEMO_AUTH": self.auth,
            "ROUND_BACKUP_STORAGE_MODE": self.backup_mode,
            "ROUND_BACKUP_DIRECTORY": self.backup_directory,
            "ROUND_BACKUP_ENCRYPTION_KEY": self.backup_key.get_secret_value(),
        }


class ApiService(BoundaryModel):
    environment: ApiEnvironment
    volumes: list[Mount] = Field(default_factory=list)
    ports: list[Port] = Field(default_factory=list)


class GatewayArgs(BoundaryModel):
    event_start: str = Field(default="", alias="VITE_EVENT_START_AT")
    survey: str = Field(default="", alias="VITE_SURVEY_URL")
    home: str = Field(default="", alias="VITE_HOME_URL")


class ComposeInput(BoundaryModel):
    name: Literal["gs-app-tomidaisai", "gs-app-tomidaisai-tls"]
    api: ApiService = Field(validation_alias=AliasPath("services", "api"))
    secondary: ApiService = Field(validation_alias=AliasPath("services", "api-secondary"))
    password: SecretStr = Field(
        validation_alias=AliasPath("services", "postgres", "environment", "POSTGRES_PASSWORD")
    )
    database: Literal["quiz_demo"] = Field(
        validation_alias=AliasPath("services", "postgres", "environment", "POSTGRES_DB")
    )
    username: Literal["quiz"] = Field(
        validation_alias=AliasPath("services", "postgres", "environment", "POSTGRES_USER")
    )
    volume_name: Literal["gs-app-tomidaisai_postgres-data"] = Field(
        validation_alias=AliasPath("volumes", "postgres-data", "name")
    )
    external_volume: bool = Field(
        default=False, validation_alias=AliasPath("volumes", "postgres-data", "external")
    )
    pgdata: Literal["/var/lib/postgresql/data"] | None = Field(
        default=None, validation_alias=AliasPath("services", "postgres", "environment", "PGDATA")
    )
    postgres_ports: list[Port] = Field(
        default_factory=list, validation_alias=AliasPath("services", "postgres", "ports")
    )
    redis_ports: list[Port] = Field(
        default_factory=list, validation_alias=AliasPath("services", "redis", "ports")
    )
    postgres_mounts: list[Mount] = Field(
        validation_alias=AliasPath("services", "postgres", "volumes")
    )
    redis_mounts: list[Mount] = Field(
        default_factory=list, validation_alias=AliasPath("services", "redis", "volumes")
    )
    gateway_mounts: list[Mount] = Field(
        default_factory=list, validation_alias=AliasPath("services", "gateway", "volumes")
    )
    gateway_ports: list[Port] = Field(validation_alias=AliasPath("services", "gateway", "ports"))
    gateway_args: GatewayArgs = Field(
        validation_alias=AliasPath("services", "gateway", "build", "args")
    )
    redis_password: SecretStr | None = Field(
        default=None,
        validation_alias=AliasPath("services", "redis", "environment", "REDIS_PASSWORD"),
    )

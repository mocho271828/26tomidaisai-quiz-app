"""明示Deployが新規作成した専用開催だけの、参加者ID割当準備。"""

from pathlib import Path
from uuid import UUID

import logfire
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from quiz_app.bootstrap.participant_id_pool_sql import build_pool_preparation_statement
from quiz_app.dependency_hosts import validate_remote_dependency_host
from quiz_app.rehearsal.initialization import APPLICATION_ROLE
from quiz_app.rehearsal.preparation import PREPARATION_LOCK_KEY
from validation.aws_review.host_contracts import HostRefusedError
from validation.aws_review.host_data import (
    POSTGRES_CA_FILE,
    DatabaseIdentity,
    DatabasePresence,
    HostSecrets,
)


def prepare_review_participant_ids(
    secrets: HostSecrets, event_id: UUID, postgres_ca: Path = POSTGRES_CA_FILE
) -> None:
    """停止・reset・開催登録後にだけ呼び、poolと権限を同時に確定する。"""

    url = make_url(secrets.initialization.admin_database_url.get_secret_value())
    if url.username != "quizadmin" or url.database != "quiz" or not postgres_ca.is_absolute():
        raise HostRefusedError("review_participant_id_database_target_invalid")
    host = validate_remote_dependency_host("database", url.render_as_string(hide_password=False))
    logfire.info("review participant ID preparation started", event_id=event_id)
    try:
        with (
            psycopg.connect(
                host=host.hostname,
                hostaddr=host.selected_address,
                port=5432,
                dbname="quiz",
                user="quizadmin",
                password=url.password,
                sslmode="verify-full",
                sslrootcert=str(postgres_ca),
                connect_timeout=5,
                options="-c statement_timeout=30000 -c lock_timeout=5000",
                row_factory=dict_row,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT current_database() AS database_name, current_user AS current_role, "
                "session_user AS session_role"
            )
            DatabaseIdentity.model_validate(cursor.fetchone())
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (PREPARATION_LOCK_KEY,))
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() AND usename = %s) AS present",
                (APPLICATION_ROLE,),
            )
            if DatabasePresence.model_validate(cursor.fetchone()).present:
                raise HostRefusedError("review_database_application_still_connected")
            cursor.execute(
                "SELECT ((SELECT count(*) FROM public.events) = 1 "
                "AND EXISTS (SELECT 1 FROM public.events WHERE id = %s) "
                "AND NOT EXISTS (SELECT 1 FROM public.participants)) AS present",
                (event_id,),
            )
            if not DatabasePresence.model_validate(cursor.fetchone()).present:
                raise HostRefusedError("review_participant_id_fresh_event_required")
            # psqlのmeta command/COMMITを含めず、権限付与失敗も同じtransactionで戻す。
            cursor.execute(build_pool_preparation_statement(event_id), prepare=False)
            cursor.execute(
                sql.SQL("GRANT USAGE ON SEQUENCE public.{} TO {}").format(
                    sql.Identifier(f"participant_id_{event_id.hex}"),
                    sql.Identifier(APPLICATION_ROLE),
                )
            )
    except psycopg.Error:
        # 接続URL・driver本文・生成SQLは、SSMの失敗診断へ渡さない。
        raise HostRefusedError("review_participant_id_preparation_failed") from None
    logfire.info("review participant ID preparation completed", event_id=event_id)

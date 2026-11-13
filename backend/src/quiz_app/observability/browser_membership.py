"""可変ブラウザ・6問の保存とDOMを、run専用HMACで個別照合する。"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import stat
import sys
from pathlib import Path
from typing import Annotated

import logfire
from pydantic import Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from quiz_app.db import get_engine
from quiz_app.observability.browser_membership_models import (
    BrowserConditions,
    BrowserEvent,
    BrowserManifest,
    BrowserRun,
    ManualParticipantAllowlist,
    MembershipReport,
    MembershipSnapshot,
    RecordingHealth,
    SnapshotMetadata,
    StoredAnswer,
    StrictModel,
)
from quiz_app.observability.browser_membership_report import build_report
from quiz_app.observability.realtime_failures import realtime_failure_record

IDENTITY_DOMAIN = "quiz.load-test.identity.v1"
MAX_EVIDENCE_BYTES = 256 * 1024 * 1024

METADATA_SQL = """
SELECT current_setting('transaction_read_only') = 'on' AS transaction_read_only,
       current_setting('transaction_isolation') AS isolation_level,
       (SELECT COUNT(*) FROM events) AS event_count,
       (SELECT COUNT(*) FROM quiz_rounds) AS round_count,
       (SELECT COUNT(*) FROM quiz_rounds r WHERE r.state = 'Voided' OR EXISTS (
         SELECT 1 FROM operation_audits a WHERE a.round_id = r.id
           AND a.target_type = 'quiz_round' AND a.after_state = 'Voided'
           AND a.outcome = 'succeeded')) AS void_history,
       (SELECT COUNT(*) FROM answers a LEFT JOIN participants p ON p.id = a.participant_id
         LEFT JOIN quiz_rounds r ON r.id = a.round_id
         WHERE p.id IS NULL OR r.id IS NULL OR p.event_id <> r.event_id) AS unmatched_answers,
       (SELECT COUNT(*) FROM participants) AS participant_count,
       (SELECT COUNT(*) FROM answers) AS answer_count,
       transaction_timestamp() AS captured_at
"""
PARTICIPANTS_SQL = "SELECT public_label FROM participants ORDER BY public_label"
ANSWERS_SQL = """
WITH numbered_rounds AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY version) AS round_number FROM quiz_rounds
)
SELECT p.public_label, r.round_number FROM answers a
JOIN participants p ON p.id = a.participant_id
JOIN numbered_rounds r ON r.id = a.round_id
ORDER BY p.public_label, r.round_number
"""


class ParticipantRow(StrictModel):
    public_label: str = Field(pattern=r"^[A-Z]{3}$", repr=False)


class AnswerRow(ParticipantRow):
    round_number: int = Field(ge=1, le=6)


class ManualLabels(StrictModel):
    labels: list[Annotated[str, Field(pattern=r"^[A-Z]{3}$")]] = Field(max_length=17576, repr=False)


def identity_fingerprint(key: bytes, run_id: str) -> str:
    """TS participantIdentityと同じrun専用の鍵fingerprintを返す。"""
    if len(key) != 32 or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", run_id) is None:
        raise ValueError("membership_identity_invalid")
    return hmac.digest(key, f"{IDENTITY_DOMAIN}\0{run_id}".encode(), "sha256").hex()


def participant_identity(key: bytes, run_id: str, public_label: str) -> str:
    """生の参加者IDをartifactへ渡さず、run内だけで比較できる値へ変換する。"""
    identity_fingerprint(key, run_id)
    if re.fullmatch(r"[A-Z]{3}", public_label) is None:
        raise ValueError("membership_identity_invalid")
    message = f"{IDENTITY_DOMAIN}\0{run_id}\0{public_label}".encode()
    return hmac.digest(key, message, "sha256").hex()


def read_identity_key(path: Path) -> bytes:
    """所有する0600通常fileから鍵だけを読み、symlink/hardlinkを拒否する。"""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        before = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size not in (64, 65)
        ):
            raise ValueError("membership_identity_key_invalid")
        body = file.read(66)
        if not unchanged_file(before, os.fstat(file.fileno())) or (
            re.fullmatch(rb"[a-f0-9]{64}\n?", body) is None
        ):
            raise ValueError("membership_identity_key_invalid")
    return bytes.fromhex(body.decode().strip())


def manual_allowlist(path: Path, key: bytes, run_id: str) -> ManualParticipantAllowlist:
    """生のIDは所有者限定fileから読み、run専用HMACだけを成果物へ渡す。"""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        before = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or not 0 < before.st_size <= 256 * 1024
        ):
            raise ValueError("membership_manual_labels_file_invalid")
        body = file.read(256 * 1024 + 1)
        if len(body) != before.st_size or not unchanged_file(before, os.fstat(file.fileno())):
            raise ValueError("membership_evidence_changed")
    labels = ManualLabels.model_validate_json(body)
    return ManualParticipantAllowlist(
        run_id=run_id,
        identity_key_fingerprint=identity_fingerprint(key, run_id),
        participant_keys=[participant_identity(key, run_id, label) for label in labels.labels],
    )


def read_snapshot(
    engine: Engine, key: bytes, run_id: str, source_commit: str
) -> MembershipSnapshot:
    """単一のREAD ONLY/REPEATABLE READ transactionで匿名snapshotを作る。"""
    fingerprint = identity_fingerprint(key, run_id)
    if re.fullmatch(r"[a-f0-9]{40}", source_commit) is None:
        raise ValueError("membership_source_commit_invalid")
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            connection.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            metadata = SnapshotMetadata.model_validate(
                connection.execute(text(METADATA_SQL)).mappings().one()
            )
            if metadata.participant_count > 17576 or metadata.answer_count > 105456:
                raise ValueError("membership_snapshot_limit_exceeded")
            # VOID履歴がないと確認してからversion順を実出題番号へ対応付ける。
            participants = [
                ParticipantRow.model_validate(row)
                for row in connection.execute(text(PARTICIPANTS_SQL)).mappings()
            ]
            answers = [
                AnswerRow.model_validate(row)
                for row in connection.execute(text(ANSWERS_SQL)).mappings()
            ]
    if metadata.participant_count != len(participants) or metadata.answer_count != len(answers):
        raise ValueError("membership_snapshot_cardinality_invalid")
    return MembershipSnapshot(
        run_id=run_id,
        source_commit=source_commit,
        identity_key_fingerprint=fingerprint,
        metadata=metadata,
        participants=[participant_identity(key, run_id, row.public_label) for row in participants],
        answers=[
            StoredAnswer(
                participant_key=participant_identity(key, run_id, row.public_label),
                round_number=row.round_number,
            )
            for row in answers
        ],
    )


def read_evidence(path: Path, *, allow_empty: bool = False) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as file:
        status = os.fstat(file.fileno())
        if not stat.S_ISREG(status.st_mode) or not (
            (0 if allow_empty else 1) <= status.st_size <= MAX_EVIDENCE_BYTES
        ):
            raise ValueError("membership_evidence_file_invalid")
        body = file.read(MAX_EVIDENCE_BYTES + 1)
        if len(body) != status.st_size or not unchanged_file(status, os.fstat(file.fileno())):
            raise ValueError("membership_evidence_changed")
    return body


def unchanged_file(before: os.stat_result, after: os.stat_result) -> bool:
    """読取りだけで変わるatimeを除き、入力差替えと書換えを拒否する。"""
    fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_mode",
        "st_uid",
        "st_nlink",
    )
    return all(getattr(before, field) == getattr(after, field) for field in fields)


def write_output(
    path: Path, value: MembershipSnapshot | MembershipReport | ManualParticipantAllowlist
) -> None:
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w"
    ) as file:
        file.write(value.model_dump_json(indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    """private鍵はfileで渡し、DB・入力例外の内容をstdout/logへ出さない。"""
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    snapshot_parser = actions.add_parser("snapshot")
    snapshot_parser.add_argument("--run-id", required=True)
    snapshot_parser.add_argument("--source-commit", required=True)
    snapshot_parser.add_argument("--identity-key-file", required=True, type=Path)
    allowlist_parser = actions.add_parser("manual-allowlist")
    allowlist_parser.add_argument("--run-id", required=True)
    allowlist_parser.add_argument("--identity-key-file", required=True, type=Path)
    allowlist_parser.add_argument("--labels-file", required=True, type=Path)
    analyze_parser = actions.add_parser("analyze")
    analyze_parser.add_argument("--snapshot", required=True, type=Path)
    analyze_parser.add_argument("--conditions", type=Path, nargs="+", default=[])
    analyze_parser.add_argument("--events", type=Path, nargs="+", default=[])
    analyze_parser.add_argument("--health", type=Path, nargs="+")
    analyze_parser.add_argument("--manifest", type=Path)
    analyze_parser.add_argument("--manual-allowlist", type=Path)
    for child in (snapshot_parser, analyze_parser, allowlist_parser):
        child.add_argument("--output", required=True, type=Path)
    logfire.configure(send_to_logfire=False, console=logfire.ConsoleOptions(output=sys.stderr))
    arguments = parser.parse_args(argv)
    try:
        logfire.info("browser_membership_started", operation=arguments.action)
        if arguments.action == "snapshot":
            key = read_identity_key(arguments.identity_key_file)
            logfire.info("browser_membership_read_only_snapshot_started")
            value = read_snapshot(get_engine(), key, arguments.run_id, arguments.source_commit)
            logfire.info(
                "browser_membership_snapshot_complete",
                participants=value.metadata.participant_count,
                answers=value.metadata.answer_count,
            )
        elif arguments.action == "manual-allowlist":
            key = read_identity_key(arguments.identity_key_file)
            value = manual_allowlist(arguments.labels_file, key, arguments.run_id)
        else:
            snapshot_body = read_evidence(arguments.snapshot)
            snapshot = MembershipSnapshot.model_validate_json(snapshot_body)
            if len(arguments.conditions) != len(arguments.events):
                raise ValueError("membership_browser_inputs_mismatch")
            if arguments.health is not None and len(arguments.health) != len(arguments.events):
                raise ValueError("membership_browser_health_inputs_mismatch")
            input_sha256 = {"snapshot": hashlib.sha256(snapshot_body).hexdigest()}
            manifest = None
            if arguments.manifest is not None:
                body = read_evidence(arguments.manifest)
                input_sha256["manifest"] = hashlib.sha256(body).hexdigest()
                manifest = BrowserManifest.model_validate_json(body)
            manual_participants = None
            if arguments.manual_allowlist is not None:
                body = read_evidence(arguments.manual_allowlist)
                input_sha256["manual_allowlist"] = hashlib.sha256(body).hexdigest()
                manual_participants = ManualParticipantAllowlist.model_validate_json(body)
            runs: list[BrowserRun] = []
            for index, (condition, events) in enumerate(
                zip(arguments.conditions, arguments.events, strict=True)
            ):
                condition_body = read_evidence(condition)
                events_body = read_evidence(events, allow_empty=manifest is not None)
                input_sha256[f"conditions_{index}"] = hashlib.sha256(condition_body).hexdigest()
                input_sha256[f"events_{index}"] = hashlib.sha256(events_body).hexdigest()
                health = None
                if manifest is not None:
                    health_path = (
                        arguments.health[index]
                        if arguments.health is not None
                        else events.with_name("evidence-health.json")
                    )
                    try:
                        health_body = read_evidence(health_path)
                    except FileNotFoundError:
                        # 異常終了でhealthが残らなかったworkerも固定母数から除かない。
                        health_body = None
                    if health_body is not None:
                        input_sha256[f"health_{index}"] = hashlib.sha256(health_body).hexdigest()
                        health = RecordingHealth.model_validate_json(health_body)
                runs.append(
                    BrowserRun(
                        conditions=BrowserConditions.model_validate_json(condition_body),
                        events=[
                            BrowserEvent.model_validate_json(line)
                            for line in events_body.splitlines()
                            if line
                        ],
                        recording_health=health,
                    )
                )
            value = build_report(snapshot, runs, manifest, manual_participants)
            value.input_sha256 = input_sha256
            logfire.info(
                "browser_membership_comparison_complete",
                cohorts=len(runs),
                expected_pairs=value.expected_pairs,
                mapped_participants=value.mapped_participants,
                decision=value.decision,
            )
        write_output(arguments.output, value)
        logfire.info("browser_membership_finished", operation=arguments.action)
        return 0 if not isinstance(value, MembershipReport) or value.decision == "PASS" else 1
    except (ValueError, OSError, SQLAlchemyError) as error:
        # SDKへcause・SQL・bind・validation入力を渡さず、app内の失敗位置だけを残す。
        safe = realtime_failure_record(error, "single_event", None)
        logfire.error(
            "browser_membership_failed",
            stack=safe.stack,
            stack_state=safe.stack_state,
            _exc_info=False,
        )
        print(
            '{"status":"failed","code":"browser_membership_invalid_or_unavailable"}',
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

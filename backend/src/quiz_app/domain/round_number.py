import logfire
from sqlalchemy import select
from sqlmodel import Session

from quiz_app.domain.errors import DomainInvariantError
from quiz_app.models import OperationAudit, QuizRound
from quiz_app.models.enums import RoundState

PRACTICE_ROUND_COUNT = 1


def is_practice_round(round_number: int) -> bool:
    """開始前VOIDを除いた実出題順の先頭1問を練習として扱う。"""

    return 1 <= round_number <= PRACTICE_ROUND_COUNT


def quiz_round_number(session: Session, round_: QuizRound) -> int:
    """開始前VOIDを除いた開催内の進行順を、永続履歴から1始まりで返す。"""

    try:
        return _round_number_from_history(session, round_)
    except DomainInvariantError:
        logfire.exception("round_number_projection_failed")
        raise


def _round_number_from_history(session: Session, round_: QuizRound) -> int:
    # 前Roundは確定後に変更されず、次Roundは最大version + 1で作られる。
    # 対象自身を数えないことで、読取中に対象のversionが進んでも番号を維持する。
    previous_rounds = session.scalars(
        select(QuizRound).where(
            QuizRound.event_id == round_.event_id,
            QuizRound.version < round_.version,
            QuizRound.id != round_.id,
        )
    ).all()
    if not previous_rounds:
        return 1

    audits = session.scalars(
        select(OperationAudit).where(
            OperationAudit.event_id == round_.event_id,
            OperationAudit.round_id.in_([previous.id for previous in previous_rounds]),
            OperationAudit.target_type == "quiz_round",
            OperationAudit.after_state == RoundState.VOIDED.value,
            OperationAudit.outcome == "succeeded",
        )
    ).all()
    number = 1
    for previous in previous_rounds:
        void_audits = [
            audit
            for audit in audits
            if audit.round_id == previous.id and audit.target_id == str(previous.id)
        ]
        # VOID後にbackup状態へ遷移しても、VOID確定の監査は残る。
        if previous.state is not RoundState.VOIDED and not void_audits:
            number += 1
            continue
        if previous.open_at is None:
            continue
        timestamps = {audit.occurred_at for audit in void_audits if audit.occurred_at is not None}
        # 現在時刻との比較では、開始前に中止した問題まで後日加算されてしまう。
        # VOID確定時刻が欠落・矛盾する履歴は推測せず、固定error codeで拒否する。
        if len(timestamps) != 1:
            raise DomainInvariantError("round_number_void_timing_unavailable")
        voided_at = timestamps.pop()
        if voided_at >= previous.open_at:
            number += 1
    return number

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Generic, TypeVar, cast
from uuid import UUID

from sqlalchemy import Engine, create_engine
from sqlmodel import Session, SQLModel

from quiz_app.audience.auth_service import ParticipantPrincipal
from quiz_app.audience.contracts import PersonalResultRevision, ScoredParticipantOutcomeResponse
from quiz_app.audience.projection import AudienceProjectionService
from quiz_app.audience.result_batch import ResultBatch, ResultBatchKey
from quiz_app.models.core import Event, ScoringPolicy
from quiz_app.models.enums import (
    EventState,
    RankingVisibility,
    RoundResultPublication,
    RoundState,
    SpeedCohort,
)
from quiz_app.models.quiz import (
    Answer,
    Question,
    QuestionOption,
    QuizRound,
    RoundOptionStat,
)
from quiz_app.quiz_configuration import ResultVisibility

NOW = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)
EVENT_ID = UUID("60000000-0000-4000-8000-000000000001")
QUESTION_ID = UUID("60000000-0000-4000-8000-000000000002")
OPTION_ID = UUID("60000000-0000-4000-8000-000000000003")
PARTICIPANT_ID = UUID("60000000-0000-4000-8000-000000000004")
IMAGE_URL = "https://r2-object.test-only.tomidaisai.jp/load-test/round-2.png"
IMAGE_ALT = "Round 2 question image"
RowT = TypeVar("RowT")


class FakeScalarResult(Generic[RowT]):
    def __init__(self, rows: list[RowT]) -> None:
        self._rows = rows

    def all(self) -> list[RowT]:
        return self._rows


class FakeProjectionSession:
    def __init__(self, question: Question, options: list[QuestionOption]) -> None:
        self._question = question
        self._options = options

    def get(self, _: object, __: UUID) -> Question:
        return self._question

    def scalars(self, _: object) -> FakeScalarResult:
        return FakeScalarResult(self._options)


def test_participant_projection_hides_answer_and_retired_images_while_result_is_withheld() -> None:
    question = Question(
        id=QUESTION_ID,
        event_id=EVENT_ID,
        version=1,
        prompt="[DEMO] 2 + 2 は？",
        display_order=1,
        image_url=IMAGE_URL,
        image_alt=IMAGE_ALT,
    )
    options = [
        QuestionOption(
            id=OPTION_ID,
            question_id=QUESTION_ID,
            label="B",
            content="4",
            display_order=1,
            is_correct=True,
        )
    ]
    round_ = QuizRound(
        event_id=EVENT_ID,
        question_id=QUESTION_ID,
        version=2,
        state=RoundState.OPEN,
        open_at=NOW - timedelta(seconds=1),
        standard_close_at=NOW + timedelta(seconds=4),
    )
    session = cast(Session, FakeProjectionSession(question, options))
    service = AudienceProjectionService(cast(Engine, object()))

    participant = service._current_round_projection(
        session,
        round_,
        NOW,
        round_number=1,
    )

    assert participant.question is not None
    assert set(participant.question.model_dump()) == {"question_id", "prompt", "options"}
    assert IMAGE_URL not in participant.model_dump_json()
    assert IMAGE_ALT not in participant.model_dump_json()
    assert "is_correct" not in participant.question.model_dump_json()
    assert "result" not in participant.model_dump(mode="json")


def test_unselected_operator_preview_never_enters_audience_projection() -> None:
    next_question_id = UUID("60000000-0000-4000-8000-000000000090")
    next_option_id = UUID("60000000-0000-4000-8000-000000000091")
    next_prompt = "NEXT-QUESTION-ONLY-FOR-OPERATOR"
    next_option_content = "NEXT-OPTION-ONLY-FOR-OPERATOR"
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    current_question = Question(
        id=QUESTION_ID,
        event_id=EVENT_ID,
        version=1,
        prompt="[DEMO] 現在の問題",
        display_order=1,
        created_at=NOW,
    )
    current_options = [
        QuestionOption(
            id=OPTION_ID,
            question_id=QUESTION_ID,
            label="A",
            content="現在の選択肢",
            display_order=1,
            is_correct=True,
        )
    ]
    next_question = Question(
        id=next_question_id,
        event_id=EVENT_ID,
        version=2,
        prompt=next_prompt,
        display_order=2,
        created_at=NOW,
    )
    next_options = [
        QuestionOption(
            id=next_option_id,
            question_id=next_question_id,
            label="A",
            content=next_option_content,
            display_order=1,
            is_correct=True,
        )
    ]
    round_ = QuizRound(
        event_id=EVENT_ID,
        question_id=QUESTION_ID,
        version=2,
        state=RoundState.OPEN,
        open_at=NOW - timedelta(seconds=1),
        standard_close_at=NOW + timedelta(seconds=4),
    )
    service = AudienceProjectionService(engine)
    with Session(engine) as session:
        session.add_all([current_question, *current_options, next_question, *next_options])
        session.commit()
        participant = service._current_round_projection(
            session,
            round_,
            NOW,
            round_number=1,
        )

    participant_payload = participant.model_dump_json()
    assert str(next_question_id) not in participant_payload
    assert str(next_option_id) not in participant_payload
    assert next_prompt not in participant_payload
    assert next_option_content not in participant_payload
    assert "is_correct" not in participant_payload


def result_revision(round_id: UUID) -> PersonalResultRevision:
    return PersonalResultRevision(
        event_id=EVENT_ID,
        event_version=1,
        result_round_id=round_id,
        result_round_version=1,
        ranking_snapshot_version=None,
        result_visibility=ResultVisibility(
            distribution=False, personal_ranking=True, overall_ranking=False
        ),
    )


def test_participant_outcome_extracts_only_own_score_from_shared_batch() -> None:
    stored = ScoredParticipantOutcomeResponse(
        timing_basis="server_received_v1",
        status="Scored",
        scoring_policy_version="scoring-demo-v1",
        is_correct=True,
        base_points=400,
        speed_points=550,
        elapsed_ms=2450,
        total_points=950,
        speed_rank=1,
        speed_cohort_size=4,
        speed_cohort=SpeedCohort.CORRECT,
    )
    revision = result_revision(UUID(int=21))
    batch = ResultBatch(
        ResultBatchKey.from_revision(revision),
        {PARTICIPANT_ID: OPTION_ID, UUID(int=99): UUID(int=100)},
        {
            PARTICIPANT_ID: stored,
            UUID(int=99): stored.model_copy(
                update={
                    "total_points": 123,
                    "base_points": 100,
                    "speed_points": 23,
                    "elapsed_ms": 9876,
                }
            ),
        },
        {},
        (),
    )
    payload = batch.for_participant(
        ParticipantPrincipal(PARTICIPANT_ID, UUID(int=10), EVENT_ID, "ABC", True), revision
    )
    outcome = payload.outcome
    assert outcome is not None
    assert outcome.status == "Scored"
    assert (outcome.base_points, outcome.speed_points, outcome.elapsed_ms) == (400, 550, 2450)
    assert outcome.total_points == 950
    assert outcome.scoring_policy_version == "scoring-demo-v1"
    assert outcome.speed_cohort is SpeedCohort.CORRECT
    assert outcome.speed_rank == 1
    assert outcome.speed_cohort_size == 4
    assert payload.selected_option_id == OPTION_ID
    assert str(UUID(int=99)) not in payload.model_dump_json()
    assert str(UUID(int=100)) not in payload.model_dump_json()
    outcome.speed_rank = None
    assert stored.speed_rank == 1


def test_participant_outcome_is_unanswered_without_score_ledger() -> None:
    revision = result_revision(UUID(int=22))
    batch = ResultBatch(ResultBatchKey.from_revision(revision), {}, {}, {}, ())
    outcome = batch.for_participant(
        ParticipantPrincipal(PARTICIPANT_ID, UUID(int=10), EVENT_ID, "ABC", True), revision
    ).outcome
    assert outcome is not None
    assert outcome.model_dump(mode="json") == {"status": "Unanswered"}


def test_published_common_projection_has_no_personal_outcome_or_answer_query() -> None:
    question = Question(
        id=QUESTION_ID,
        event_id=EVENT_ID,
        version=1,
        prompt="[DEMO] 2 + 2 は？",
        display_order=1,
        image_url=IMAGE_URL,
        image_alt=IMAGE_ALT,
    )
    options = [
        QuestionOption(
            id=OPTION_ID,
            question_id=QUESTION_ID,
            label="B",
            content="4",
            display_order=1,
            is_correct=True,
        )
    ]
    round_ = QuizRound(
        id=UUID(int=23),
        event_id=EVENT_ID,
        question_id=QUESTION_ID,
        version=3,
        state=RoundState.REVEAL,
        result_publication=RoundResultPublication.PUBLISHED,
    )
    stats = [
        RoundOptionStat(
            round_id=round_.id,
            option_id=OPTION_ID,
            valid_response_count=1,
            response_percentage=Decimal("100.00"),
        )
    ]

    class PublishedProjectionSession:
        def __init__(self) -> None:
            self._scalar_result_index = 0
            self.ledger_read_count = 0

        def get(self, _: object, __: UUID) -> Question:
            return question

        def scalars(
            self,
            _: object,
        ) -> FakeScalarResult[QuestionOption] | FakeScalarResult[RoundOptionStat]:
            rows: list[QuestionOption] | list[RoundOptionStat]
            if self._scalar_result_index == 0:
                rows = options
            else:
                rows = stats
            self._scalar_result_index += 1
            return FakeScalarResult(rows)

        def scalar(self, _: object) -> None:
            self.ledger_read_count += 1
            raise AssertionError("common_projection_must_not_read_personal_data")

    participant_session = PublishedProjectionSession()
    service = AudienceProjectionService(cast(Engine, object()))

    participant = service._common_latest_result_projection(
        cast(Session, participant_session),
        round_,
        NOW,
        round_number=1,
        is_distribution_visible=True,
    )

    assert participant.result.publication == "Published"
    assert participant.question is not None
    assert set(participant.question.model_dump()) == {"question_id", "prompt", "options"}
    assert IMAGE_URL not in participant.model_dump_json()
    assert IMAGE_ALT not in participant.model_dump_json()
    assert participant.version == 3
    assert participant.round_number == 1
    assert "selected_option_id" not in participant.model_dump()
    assert "outcome" not in participant.result.model_dump()
    assert participant_session.ledger_read_count == 0


def test_state_keeps_previous_published_result_while_next_round_is_current(
    legacy_quiz_configuration: object,
) -> None:
    previous_question_id = UUID(int=31)
    previous_option_id = UUID(int=32)
    previous_round_id = UUID(int=33)
    current_question_id = UUID(int=34)
    current_option_id = UUID(int=35)
    current_round_id = UUID(int=36)
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    # SQLiteはPostgreSQL専用の部分インデックス条件を反映しないため、履歴Roundを再現する。
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_quiz_rounds_one_active_per_event")
    event = Event(
        id=EVENT_ID,
        slug="demo-2026",
        title="[DEMO] 初回確認",
        state=EventState.IN_PROGRESS,
        ranking_visibility=RankingVisibility.HIDDEN_ALL,
        scoring_policy_version="scoring-demo",
        display_name_policy_version="display-demo",
        created_at=NOW,
        updated_at=NOW,
    )
    previous_question = Question(
        id=previous_question_id,
        event_id=EVENT_ID,
        version=1,
        prompt="直前の公開済み問題",
        display_order=1,
        created_at=NOW,
    )
    previous_option = QuestionOption(
        id=previous_option_id,
        question_id=previous_question_id,
        label="A",
        content="公開済み正解",
        display_order=1,
        is_correct=True,
    )
    previous_round = QuizRound(
        id=previous_round_id,
        event_id=EVENT_ID,
        question_id=previous_question_id,
        version=60,
        state=RoundState.ROUND_BACKUP_VALIDATED,
        result_publication=RoundResultPublication.PUBLISHED,
        created_at=NOW,
    )
    previous_stat = RoundOptionStat(
        round_id=previous_round_id,
        option_id=previous_option_id,
        valid_response_count=0,
        response_percentage=Decimal("0.00"),
    )
    current_question = Question(
        id=current_question_id,
        event_id=EVENT_ID,
        version=2,
        prompt="次の未公開問題",
        display_order=2,
        created_at=NOW,
    )
    current_option = QuestionOption(
        id=current_option_id,
        question_id=current_question_id,
        label="A",
        content="次の未公開選択肢",
        display_order=1,
        is_correct=True,
    )
    current_round = QuizRound(
        id=current_round_id,
        event_id=EVENT_ID,
        question_id=current_question_id,
        version=74,
        state=RoundState.PREPARING_ASSETS,
        result_publication=RoundResultPublication.WITHHELD,
        created_at=NOW,
    )
    with Session(engine) as session:
        session.add_all(
            [
                event,
                *[
                    QuizRound(
                        id=UUID(int=identifier),
                        event_id=EVENT_ID,
                        question_id=UUID(int=identifier + 100),
                        version=version,
                        state=RoundState.ROUND_BACKUP_VALIDATED,
                        created_at=NOW,
                    )
                    for identifier, version in ((41, 10), (42, 40))
                ],
                previous_question,
                previous_option,
                previous_round,
                previous_stat,
                current_question,
                current_option,
                current_round,
            ]
        )
        session.commit()

    class FixedClockAudienceProjectionService(AudienceProjectionService):
        @staticmethod
        def _database_time(_: Session) -> datetime:
            return NOW

    service = FixedClockAudienceProjectionService(engine)
    principal = ParticipantPrincipal(
        participant_id=PARTICIPANT_ID,
        session_id=UUID(int=37),
        event_id=EVENT_ID,
        public_label="ABC",
        is_ranking_eligible=True,
    )

    participant = service.get_participant_state(principal)

    assert participant.current_round is not None
    assert participant.current_round.round_id == current_round_id
    assert participant.current_round.round_number == 4
    assert participant.current_round.version == 74
    assert participant.current_round.question is not None
    assert participant.current_round.question.prompt == "次の未公開問題"
    assert participant.latest_result is not None
    assert participant.latest_result.round_id == previous_round_id
    assert participant.latest_result.round_number == 3
    assert participant.latest_result.question.prompt == "直前の公開済み問題"
    assert "selected_option_id" not in participant.latest_result.model_dump()
    assert "outcome" not in participant.latest_result.result.model_dump()
    assert participant.personal_result_delivery.status == "pending"
    common = service.get_common_state(EVENT_ID)
    assert common.latest_result is not None
    common_result = common.latest_result.model_dump()
    assert "selected_option_id" not in common_result
    assert "outcome" not in common_result["result"]

    reconnected = service.get_participant_state(principal)
    late_joiner = service.get_participant_state(
        ParticipantPrincipal(
            participant_id=UUID(int=99),
            session_id=UUID(int=98),
            event_id=EVENT_ID,
            public_label="XYZ",
            is_ranking_eligible=False,
        )
    )
    assert late_joiner.participant.is_ranking_eligible is True
    for projection in (reconnected, late_joiner):
        assert projection.current_round is not None
        assert projection.current_round.round_number == 4
        assert projection.latest_result is not None
        assert projection.latest_result.round_number == 3

    with Session(engine) as session:
        stored_current_round = session.get(QuizRound, current_round_id)
        assert stored_current_round is not None
        session.delete(stored_current_round)
        session.commit()

    # fixtureが直接DBを変更したので、実mutationのcommit後無効化を明示する。
    service.invalidate_common_state(EVENT_ID)
    published_only = service.get_participant_state(principal)

    assert published_only.current_round is None
    assert published_only.latest_result is not None
    assert published_only.latest_result.round_id == previous_round_id
    assert published_only.latest_result.round_number == 3


def test_answer_status_is_scoped_to_participant_query_result() -> None:
    answer = Answer(
        participant_id=UUID(int=10),
        round_id=UUID(int=11),
        option_id=UUID(int=12),
        client_answer_id=UUID(int=13),
        received_at=NOW,
    )

    policy = ScoringPolicy(
        version="fixture",
        base_points=400,
        initial_speed_bonus=100,
        speed_bucket_ms=100,
        speed_decrement_per_bucket=1,
        standard_duration_ms=10000,
    )

    class FakeAnswerStatusSession:
        def execute(self, _: object) -> "FakeAnswerStatusSession":
            return self

        def one_or_none(self) -> tuple[Answer, ScoringPolicy]:
            return answer, policy

    status = AudienceProjectionService._answer_status(
        cast(Session, FakeAnswerStatusSession()),
        answer.participant_id,
        answer.client_answer_id,
    )

    assert status.status == "AcceptedLocked"
    assert status.answer.client_answer_id == answer.client_answer_id

    class MissingAnswerStatusSession:
        def execute(self, _: object) -> "MissingAnswerStatusSession":
            return self

        def one_or_none(self) -> None:
            return None

    missing = AudienceProjectionService._answer_status(
        cast(Session, MissingAnswerStatusSession()),
        answer.participant_id,
        UUID(int=99),
    )

    assert missing.status == "NotStored"
    assert missing.client_answer_id == UUID(int=99)

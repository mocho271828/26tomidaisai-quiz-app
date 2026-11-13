import type { OperatorState } from "../../../api/operatorContracts";

export const finalRoundIds = {
  event: "7a000000-0000-4000-8000-000000000001",
  round: "7a000000-0000-4000-8000-000000000002",
  question: "7a000000-0000-4000-8000-000000000003",
};

export function finalRoundProjection(): OperatorState {
  const control = { mode: "manual" as const, initially_visible: false, changes: [] };
  return {
    award_ceremony_phase: null,
    event_id: finalRoundIds.event, event_version: 5, event_title: "富大祭", event_state: "InProgress",
    projected_at: "2026-10-22T03:00:00Z", start_delay_seconds: 3, participant_count: 10,
    available_questions: [], used_question_ids: [], latest_ranking_snapshot_version: null,
    result_visibility: { distribution: false, personal_ranking: false, overall_ranking: false },
    visibility_controls: { distribution: control, personal_ranking: control, overall_ranking: control },
    current_round: {
      round_id: finalRoundIds.round, version: 3, state_version: 5, state: "ReadyToReveal",
      open_at: "2026-10-22T02:59:45Z", standard_close_at: "2026-10-22T02:59:55Z",
      result_publication: "Withheld", can_publish_results: true, result_failure_code: null,
      question: { question_id: finalRoundIds.question, version: 1, prompt: "現在の問題", options: [] },
      statistics: { answer_count: 0, average_response_ms: null, options: [] },
    },
  };
}

import type { OperatorState } from "../../api/operatorContracts";

const responseTimeFormatter = new Intl.NumberFormat("ja-JP", {
  maximumFractionDigits: 3,
  minimumFractionDigits: 3,
});

export function formatModeratorResponseTime(milliseconds: number | null): string {
  return milliseconds === null ? "回答なし" : `${responseTimeFormatter.format(milliseconds / 1000)}秒`;
}

export function isModeratorRoundFinalized(round: OperatorState["current_round"]): boolean {
  return round !== null && round.state !== "Voided" &&
    (round.state === "ReadyToReveal" || round.result_publication === "Published");
}

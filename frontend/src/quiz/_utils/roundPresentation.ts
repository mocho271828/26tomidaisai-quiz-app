export const PRACTICE_ROUND_COUNT = 1;

export function isPracticeRound(roundNumber: number): boolean {
  return roundNumber > 0 && roundNumber <= PRACTICE_ROUND_COUNT;
}

// APIと計測は実出題番号を維持し、参加者へ見せる本番番号だけを振り直す。
export function roundLabel(roundNumber: number): string {
  return isPracticeRound(roundNumber)
    ? `第${roundNumber}問（練習）`
    : `第${roundNumber - PRACTICE_ROUND_COUNT}問`;
}

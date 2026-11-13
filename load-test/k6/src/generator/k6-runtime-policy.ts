export const SCENARIO_START_TOLERANCE_MILLISECONDS = 1_000;

/** wall clockが後退しても、同一VUのdeadline座標を非減少に保つ。 */
export function advanceNondecreasingMilliseconds(
  previousMilliseconds: number,
  observedMilliseconds: number,
): number | null {
  if (
    !Number.isSafeInteger(previousMilliseconds) ||
    previousMilliseconds < 0 ||
    !Number.isSafeInteger(observedMilliseconds) ||
    observedMilliseconds < 0
  ) {
    return null;
  }
  return Math.max(previousMilliseconds, observedMilliseconds);
}

/** manifest予定時刻とk6 scenario開始時刻のずれを両端含みで検証する。 */
export function isScenarioStartWithinTolerance(
  scheduledStartMilliseconds: number,
  scenarioStartMilliseconds: number,
  toleranceMilliseconds: number,
): boolean {
  return (
    Number.isSafeInteger(scheduledStartMilliseconds) &&
    Number.isSafeInteger(scenarioStartMilliseconds) &&
    Number.isSafeInteger(toleranceMilliseconds) &&
    toleranceMilliseconds >= 0 &&
    Math.abs(scenarioStartMilliseconds - scheduledStartMilliseconds) <=
      toleranceMilliseconds
  );
}

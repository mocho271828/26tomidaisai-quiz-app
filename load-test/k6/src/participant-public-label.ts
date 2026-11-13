export const PARTICIPANT_PUBLIC_LABEL_PATTERN = /^[A-Z]{3}(?![\s\S])/u;
export const PARTICIPANT_PUBLIC_LABEL_CAPACITY = 26 ** 3;

/** 1始まりのparticipant順序を3桁base-26へ写像し、再実行時も同じ公開IDを返す。 */
export function participantPublicLabelForOrdinal(ordinal: number): string {
  if (
    !Number.isSafeInteger(ordinal) ||
    ordinal < 1 ||
    ordinal > PARTICIPANT_PUBLIC_LABEL_CAPACITY
  ) {
    throw new Error("participant_public_label_ordinal_invalid");
  }

  const zeroBased = ordinal - 1;
  return [26 ** 2, 26, 1]
    .map((divisor) => String.fromCharCode(65 + (Math.floor(zeroBased / divisor) % 26)))
    .join("");
}

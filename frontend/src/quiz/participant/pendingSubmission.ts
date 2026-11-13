import { z } from "zod";

const pendingSubmissionSchema = z.strictObject({
  round_id: z.uuid(),
  round_version: z.number().int().min(1),
  option_id: z.uuid(),
  client_answer_id: z.uuid(),
  client_elapsed_ms: z.number().int().nonnegative(),
});

export type PendingSubmission = z.infer<typeof pendingSubmissionSchema>;

const STORAGE_KEY = "quiz.pending-participant-answer.v1";

export function loadPendingSubmission(): PendingSubmission | null {
  let serialized: string | null;
  try {
    serialized = window.sessionStorage.getItem(STORAGE_KEY);
  } catch (error: unknown) {
    if (error instanceof DOMException) {
      return null;
    }
    throw error;
  }
  if (serialized === null) {
    return null;
  }
  try {
    const decoded: unknown = JSON.parse(serialized);
    const parsed = pendingSubmissionSchema.safeParse(decoded);
    if (parsed.success) {
      return parsed.data;
    }
  } catch (error: unknown) {
    if (!(error instanceof SyntaxError)) {
      throw error;
    }
  }
  clearPendingSubmission();
  return null;
}

export function savePendingSubmission(value: PendingSubmission): boolean {
  const parsed = pendingSubmissionSchema.parse(value);
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(parsed));
  } catch (error: unknown) {
    if (error instanceof DOMException) {
      return false;
    }
    throw error;
  }
  return true;
}

export function clearPendingSubmission(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch (error: unknown) {
    if (!(error instanceof DOMException)) {
      throw error;
    }
  }
}

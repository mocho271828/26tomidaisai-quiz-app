import { z } from "zod";

import { participantJoinRequestSchema } from "../../api/contracts";
import { createParticipantJoinToken } from "./participantUi";

const STORAGE_KEY = "quiz.pending-participant-join.v1";
const pendingParticipantJoinSchema = participantJoinRequestSchema.extend({
  version: z.literal(1),
});

export class ParticipantJoinStorageError extends Error {
  constructor() {
    super("参加情報をこのタブに保存・確認できませんでした。ブラウザ設定を確認してください。");
    this.name = "ParticipantJoinStorageError";
  }
}

export function getPendingParticipantJoinToken(): string {
  let serialized: string | null;
  try {
    serialized = window.sessionStorage.getItem(STORAGE_KEY);
  } catch (error: unknown) {
    if (error instanceof DOMException) throw new ParticipantJoinStorageError();
    throw error;
  }

  let value: unknown = null;
  if (serialized !== null) {
    try {
      value = JSON.parse(serialized);
    } catch (error: unknown) {
      if (error instanceof SyntaxError) throw new ParticipantJoinStorageError();
      throw error;
    }
  }
  // 不明な保存内容や再送期限の経過を理由に別tokenを作り、重複登録してはいけない。
  const parsed = pendingParticipantJoinSchema.safeParse(
    serialized === null
      ? { version: 1, client_join_token: createParticipantJoinToken() }
      : value,
  );
  if (!parsed.success) throw new ParticipantJoinStorageError();
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(parsed.data));
  } catch (error: unknown) {
    if (error instanceof DOMException) throw new ParticipantJoinStorageError();
    throw error;
  }
  return parsed.data.client_join_token;
}

export function clearPendingParticipantJoin(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch (error: unknown) {
    // サーバーで本人確認済みの状態は、端末の削除失敗で未登録へ戻さない。
    if (!(error instanceof DOMException)) throw error;
  }
}

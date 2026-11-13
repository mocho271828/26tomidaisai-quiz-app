import { z } from "zod";

export const MAX_MANUAL_COMMANDS = 4096;
// 既存の操作上限から参加1操作を除き、問別の状態とIPC配列も有界にする。
export const MAX_MANUAL_ROUNDS = MAX_MANUAL_COMMANDS - 1;
export const expectedRoundsSchema = z.number().int().min(1).max(MAX_MANUAL_ROUNDS);
export const roundNumberSchema = expectedRoundsSchema;

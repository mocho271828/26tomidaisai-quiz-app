import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import { joinParticipant } from "../../api/audienceClient";
import { ApiResponseError, errorMessage } from "../../api/client";
import type { ParticipantState } from "../../api/contracts";
import {
  clearPendingParticipantJoin,
  getPendingParticipantJoinToken,
  ParticipantJoinStorageError,
} from "./pendingParticipantJoin";

type ParticipantJoinOptions = {
  acceptProjection: (candidate: ParticipantState, generation: number) => boolean;
  beginJoin: () => number;
  isGenerationCurrent: (generation: number) => boolean;
  onError: (message: string | null) => void;
};

export type ParticipantJoinState = {
  isJoinDisabled: boolean;
  isJoining: boolean;
  join: () => Promise<void>;
  onJoin: (event: FormEvent<HTMLFormElement>) => Promise<void>;
  reset: () => void;
};

export function useParticipantJoin({
  acceptProjection,
  beginJoin,
  isGenerationCurrent,
  onError,
}: ParticipantJoinOptions): ParticipantJoinState {
  const [isJoining, setIsJoining] = useState(false);
  const isActiveRef = useRef(true);
  const joinInFlightGenerationRef = useRef<number | null>(null);

  useEffect(() => {
    isActiveRef.current = true;
    return () => {
      isActiveRef.current = false;
    };
  }, []);

  const reset = useCallback((): void => {
    joinInFlightGenerationRef.current = null;
    if (isActiveRef.current) {
      setIsJoining(false);
    }
  }, []);

  const join = useCallback(
    async (): Promise<void> => {
      if (!isActiveRef.current || joinInFlightGenerationRef.current !== null) {
        return;
      }
      const generation = beginJoin();
      joinInFlightGenerationRef.current = generation;
      setIsJoining(true);
      onError(null);

      try {
        // 保存後に応答だけ失われた場合も、同じ参加要求として再送する。
        const candidate = await joinParticipant(getPendingParticipantJoinToken());
        if (!isActiveRef.current || !isGenerationCurrent(generation)) {
          return;
        }
        if (acceptProjection(candidate, generation)) {
          clearPendingParticipantJoin();
        }
      } catch (error: unknown) {
        if (!isActiveRef.current || !isGenerationCurrent(generation)) {
          return;
        }
        onError(
          error instanceof ParticipantJoinStorageError
            ? error.message
            : !(error instanceof ApiResponseError) || error.status >= 500
              ? `${errorMessage(error)} もう一度参加ボタンを押してください。`
              : errorMessage(error),
        );
      } finally {
        if (joinInFlightGenerationRef.current === generation) {
          joinInFlightGenerationRef.current = null;
        }
        if (isActiveRef.current && isGenerationCurrent(generation)) {
          setIsJoining(false);
        }
      }
    },
    [acceptProjection, beginJoin, isGenerationCurrent, onError],
  );

  const onJoin = useCallback((event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    return join();
  }, [join]);

  return {
    isJoinDisabled: isJoining,
    isJoining,
    join,
    onJoin,
    reset,
  };
}

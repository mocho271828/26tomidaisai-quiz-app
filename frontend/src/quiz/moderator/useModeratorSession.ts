import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { ApiResponseError } from "../../api/client";
import { getOperatorSession, loginOperator, logoutOperator } from "../../api/operatorClient";
import type { OperatorSession } from "../../api/operatorContracts";

export function useModeratorSession() {
  const [session, setSession] = useState<OperatorSession | null>(null);
  const [isChecking, setIsChecking] = useState(true);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const active = useRef(false);
  const inFlight = useRef(false);
  const generation = useRef(0);

  useEffect(() => {
    active.current = true;
    const startedGeneration = generation.current;
    const controller = new AbortController();
    void getOperatorSession(AbortSignal.any([controller.signal, AbortSignal.timeout(5000)])).then((value) => {
      if (!controller.signal.aborted && generation.current === startedGeneration) setSession(value);
    }).catch((cause: unknown) => {
      if (!controller.signal.aborted && !(cause instanceof ApiResponseError && cause.status === 401)) {
        setError("ログイン状態を確認できませんでした。もう一度ログインしてください。");
      }
    }).finally(() => {
      if (!controller.signal.aborted) setIsChecking(false);
    });
    return () => { active.current = false; controller.abort(); };
  }, []);

  const onAuthenticationLost = useCallback((): void => {
    generation.current += 1;
    setSession(null);
    setError("ログインの有効期限が切れました。もう一度ログインしてください。");
  }, []);

  const onLogin = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setIsBusy(true);
    setError(null);
    try {
      const value = await loginOperator({ username, password });
      if (active.current) { setSession(value); setPassword(""); }
    } catch (cause: unknown) {
      if (active.current) setError(cause instanceof ApiResponseError && cause.status === 401
        ? "IDまたはパスワードを確認してください。"
        : "ログインできませんでした。時間をおいてもう一度お試しください。");
    } finally {
      inFlight.current = false;
      if (active.current) setIsBusy(false);
    }
  };

  const onLogout = async (): Promise<void> => {
    if (inFlight.current) return;
    inFlight.current = true;
    setIsBusy(true);
    setError(null);
    try {
      await logoutOperator();
      if (active.current) setSession(null);
    } catch (cause: unknown) {
      if (active.current) {
        if (cause instanceof ApiResponseError && cause.status === 401) setSession(null);
        else setError("ログアウトできませんでした。もう一度お試しください。");
      }
    } finally {
      inFlight.current = false;
      if (active.current) setIsBusy(false);
    }
  };

  return { session, isChecking, isBusy, error, username, password,
    setUsername, setPassword, onLogin, onLogout, onAuthenticationLost };
}

import { act, renderHook, waitFor } from "@testing-library/react";
import type { FormEvent } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiResponseError } from "../../api/client";
import type { OperatorSession } from "../../api/operatorContracts";
import { useModeratorSession } from "./useModeratorSession";

const api = vi.hoisted(() => ({ session: vi.fn(), login: vi.fn(), logout: vi.fn() }));
vi.mock("../../api/operatorClient", () => ({ getOperatorSession: api.session, loginOperator: api.login, logoutOperator: api.logout }));

const session: OperatorSession = { operator: {
  account_id: "78000000-0000-4000-8000-000000000001", username: "operator-fixture", role: "Operator",
} };
const unauthorized = new ApiResponseError(401, { code: "operator_session_invalid", message: "expired" });
function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}
function formEvent(): FormEvent<HTMLFormElement> {
  const form = document.createElement("form");
  return { nativeEvent: new Event("submit"), currentTarget: form, target: form,
    bubbles: true, cancelable: true, defaultPrevented: false, eventPhase: 2, isTrusted: false,
    preventDefault: vi.fn(), isDefaultPrevented: () => false, stopPropagation: vi.fn(),
    isPropagationStopped: () => false, persist: () => undefined, timeStamp: 0, type: "submit" };
}

describe("useModeratorSession", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    api.session.mockResolvedValue(session);
    api.login.mockResolvedValue(session);
    api.logout.mockResolvedValue(undefined);
  });
  afterEach(() => vi.restoreAllMocks());

  it("既存の運営sessionを1回確認して使い、資格情報を別保存しない", async () => {
    const { result } = renderHook(() => useModeratorSession());
    expect(result.current.isChecking).toBe(true);
    await waitFor(() => expect(result.current.isChecking).toBe(false));
    expect(result.current.session).toEqual(session);
    expect(api.session).toHaveBeenCalledOnce();
    expect(api.login).not.toHaveBeenCalled();
    expect(result.current.password).toBe("");
  });

  it("初回401はログイン画面へ進める通常状態とし、通信失敗と区別する", async () => {
    api.session.mockRejectedValueOnce(unauthorized);
    const { result } = renderHook(() => useModeratorSession());
    await waitFor(() => expect(result.current.isChecking).toBe(false));
    expect(result.current.session).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("初回の通信失敗は内部メッセージを出さずログイン可能な状態へ戻す", async () => {
    api.session.mockRejectedValueOnce(new Error("internal network detail"));
    const { result } = renderHook(() => useModeratorSession());
    await waitFor(() => expect(result.current.isChecking).toBe(false));
    expect(result.current.session).toBeNull();
    expect(result.current.error).toBe("ログイン状態を確認できませんでした。もう一度ログインしてください。");
    expect(result.current.error).not.toContain("internal");
  });

  it("初回確認は5秒の期限で打ち切り、確認中のまま固定しない", async () => {
    const deadline = new AbortController();
    const timeout = vi.spyOn(AbortSignal, "timeout").mockReturnValue(deadline.signal);
    api.session.mockImplementationOnce((signal: AbortSignal) => new Promise<OperatorSession>((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    }));
    const { result } = renderHook(() => useModeratorSession());
    expect(timeout).toHaveBeenCalledWith(5000);
    await act(async () => deadline.abort(new DOMException("deadline", "TimeoutError")));
    expect(result.current.isChecking).toBe(false);
    expect(result.current.session).toBeNull();
    expect(result.current.error).not.toBeNull();
    expect(api.session).toHaveBeenCalledOnce();
  });

  it("初回確認中に認証失効した後で古いsession応答を復活させない", async () => {
    const pending = deferred<OperatorSession>();
    api.session.mockReturnValueOnce(pending.promise);
    const { result } = renderHook(() => useModeratorSession());
    act(() => result.current.onAuthenticationLost());
    await act(async () => { pending.resolve(session); await pending.promise; });
    expect(result.current.session).toBeNull();
    expect(result.current.error).toBe("ログインの有効期限が切れました。もう一度ログインしてください。");
  });

  it("運営と同じlogin APIへ入力を渡し、多重送信を防ぎ成功後にpasswordを消す", async () => {
    api.session.mockRejectedValueOnce(unauthorized);
    const pending = deferred<OperatorSession>();
    api.login.mockReturnValueOnce(pending.promise);
    const { result } = renderHook(() => useModeratorSession());
    await waitFor(() => expect(result.current.isChecking).toBe(false));
    act(() => { result.current.setUsername("operator-fixture"); result.current.setPassword("test-only-password"); });
    const event = formEvent();
    let first: Promise<void> | undefined;
    await act(async () => { first = result.current.onLogin(event); void result.current.onLogin(event); });
    expect(event.preventDefault).toHaveBeenCalledTimes(2);
    expect(api.login).toHaveBeenCalledOnce();
    expect(api.login).toHaveBeenCalledWith({ username: "operator-fixture", password: "test-only-password" });
    expect(result.current.isBusy).toBe(true);
    await act(async () => { pending.resolve(session); await first; });
    expect(result.current.session).toEqual(session);
    expect(result.current.password).toBe("");
    expect(result.current.isBusy).toBe(false);
  });

  it.each([401, 503])("login失敗%sを表示し、再試行を妨げない", async (status) => {
    api.session.mockRejectedValueOnce(unauthorized);
    api.login.mockRejectedValueOnce(new ApiResponseError(status, { code: "operator_session_invalid", message: "internal reason" }));
    const { result } = renderHook(() => useModeratorSession());
    await waitFor(() => expect(result.current.isChecking).toBe(false));
    await act(async () => result.current.onLogin(formEvent()));
    expect(result.current.session).toBeNull();
    expect(result.current.isBusy).toBe(false);
    expect(result.current.error).toBe(status === 401
      ? "IDまたはパスワードを確認してください。"
      : "ログインできませんでした。時間をおいてもう一度お試しください。");
    await act(async () => result.current.onLogin(formEvent()));
    expect(result.current.session).toEqual(session);
    expect(result.current.error).toBeNull();
    expect(api.login).toHaveBeenCalledTimes(2);
  });

  it("logoutの多重送信を防ぎ、正常終了後にsessionを消す", async () => {
    const pending = deferred<void>();
    api.logout.mockReturnValueOnce(pending.promise);
    const { result } = renderHook(() => useModeratorSession());
    await waitFor(() => expect(result.current.isChecking).toBe(false));
    let first: Promise<void> | undefined;
    await act(async () => { first = result.current.onLogout(); void result.current.onLogout(); });
    expect(api.logout).toHaveBeenCalledOnce();
    expect(result.current.isBusy).toBe(true);
    await act(async () => { pending.resolve(); await first; });
    expect(result.current.session).toBeNull();
    expect(result.current.isBusy).toBe(false);
  });

  it.each([401, 503])("logout失敗%sでは失効と一時障害を区別する", async (status) => {
    api.logout.mockRejectedValueOnce(new ApiResponseError(status, { code: "operator_session_invalid", message: "internal reason" }));
    const { result } = renderHook(() => useModeratorSession());
    await waitFor(() => expect(result.current.isChecking).toBe(false));
    await act(async () => result.current.onLogout());
    expect(result.current.session).toEqual(status === 401 ? null : session);
    expect(result.current.error).toBe(status === 401 ? null : "ログアウトできませんでした。もう一度お試しください。");
    expect(result.current.isBusy).toBe(false);
  });

  it("unmountで初回確認をcancelし、遅い完了を反映しない", async () => {
    const pending = deferred<OperatorSession>();
    api.session.mockReturnValueOnce(pending.promise);
    const { result, unmount } = renderHook(() => useModeratorSession());
    const signal: AbortSignal = api.session.mock.calls[0]![0];
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { pending.resolve(session); await pending.promise; });
    expect(result.current.session).toBeNull();
  });
});

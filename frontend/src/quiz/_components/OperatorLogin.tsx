import { SignInIcon } from "@phosphor-icons/react";
import type { FormEvent } from "react";

import { ErrorNotice, LoadingPanel } from "./PageFrame";
import { FestivalLogo } from "./FestivalLogo";

type OperatorLoginProps = {
  title?: string;
  isChecking: boolean;
  isBusy: boolean;
  pageError: string | null;
  username: string;
  password: string;
  onUsernameChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => Promise<void>;
};

export function OperatorLogin({
  title = "運営ログイン",
  isChecking, isBusy, pageError, username, password,
  onUsernameChange, onPasswordChange, onSubmit,
}: OperatorLoginProps) {
  return (
    <main className="operator-page operator-login">
      <section className="operator-card operator-login-card">
        <FestivalLogo />
        <h1>{title}</h1>
        {isChecking ? (
          <LoadingPanel label="ログイン状態を確認しています" />
        ) : (
          <>
            {pageError !== null ? <ErrorNotice>{pageError}</ErrorNotice> : null}
            <form className="operator-login-form" onSubmit={onSubmit}>
              <label htmlFor="operator-username">ユーザー名</label>
              <input
                autoComplete="username" id="operator-username" maxLength={80}
                onChange={(event) => onUsernameChange(event.currentTarget.value)}
                required value={username}
              />
              <label htmlFor="operator-password">パスワード</label>
              <input
                autoComplete="current-password" id="operator-password" maxLength={256}
                onChange={(event) => onPasswordChange(event.currentTarget.value)}
                required type="password" value={password}
              />
              <button className="primary-button" disabled={isBusy} type="submit">
                <SignInIcon aria-hidden="true" size={20} />
                {isBusy ? "確認しています…" : "ログイン"}
              </button>
            </form>
          </>
        )}
      </section>
    </main>
  );
}

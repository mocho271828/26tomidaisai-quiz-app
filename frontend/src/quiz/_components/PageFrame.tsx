import { CircleNotchIcon, InfoIcon, WarningCircleIcon } from "@phosphor-icons/react";
import type { ReactNode } from "react";

import { FestivalLogo } from "./FestivalLogo";

type PageFrameProps = {
  audience: string;
  children: ReactNode;
  layout?: "answering" | "operator-wide" | "standard";
  rootClassName?: string;
  title: string;
};

export function PageFrame({ audience, children, layout = "standard", rootClassName, title }: PageFrameProps) {
  const hasVisibleHeader = layout !== "answering";
  const shellClassName = ["app-shell", `app-shell-${layout}`, rootClassName].filter(Boolean).join(" ");

  return (
    <main className={shellClassName}>
      {hasVisibleHeader ? (
        <header className="app-header app-header-stage">
          <FestivalLogo />
          <p className="app-header-audience">{audience}</p>
        </header>
      ) : <h1 className="sr-only">{title}</h1>}
      <div className={`app-content app-content-${layout}`}>
        {hasVisibleHeader ? <h1>{title}</h1> : null}
        {children}
      </div>
    </main>
  );
}

export function ErrorNotice({ children }: { children: ReactNode }) {
  return <div className="notice notice-error" role="alert"><WarningCircleIcon aria-hidden="true" size={22} weight="fill" />{children}</div>;
}

export function InfoNotice({ children }: { children: ReactNode }) {
  return <div className="notice notice-info" role="status"><InfoIcon aria-hidden="true" size={22} weight="fill" />{children}</div>;
}

export function LoadingPanel({ label }: { label: string }) {
  return (
    <section className="card centered-card" aria-busy="true" aria-live="polite">
      <CircleNotchIcon aria-hidden="true" className="spinner" size={28} weight="bold" />
      <p>{label}</p>
    </section>
  );
}

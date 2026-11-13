import { z } from "zod";

import { OperatorPage } from "./quiz/operator/OperatorPage";
import { ParticipantPage } from "./quiz/participant/ParticipantPage";
import { ModeratorPage } from "./quiz/moderator/ModeratorPage";
import "./quiz/moderator/moderatorEmbeddedParticipant.css";

const moderatorPreviewSchema = z.literal("1");

function NotFoundPage() {
  return (
    <main className="app-shell app-shell-standard">
      <div className="app-content app-content-standard">
        <h1>ページが見つかりません</h1>
        <section className="card centered-card" role="alert">
          <p className="muted">このURLの画面はありません。クイズの参加画面から始めてください。</p>
          <a className="primary-button" href="/">
            クイズに参加する
          </a>
        </section>
      </div>
    </main>
  );
}

export function App() {
  const path = window.location.pathname.replace(/\/+$/u, "") || "/";
  if (path === "/") {
    const embeddingPath = window.frameElement?.ownerDocument.location?.pathname.replace(/\/+$/u, "");
    const isModeratorPreview = embeddingPath === "/moderator" && moderatorPreviewSchema.safeParse(
      new URLSearchParams(window.location.search).get("moderator-preview"),
    ).success;
    return isModeratorPreview ? (
      <div className="moderator-embedded-participant"><ParticipantPage autoJoin /></div>
    ) : <ParticipantPage autoJoin={false} />;
  }
  if (path === "/operator") {
    return <OperatorPage />;
  }
  if (path === "/moderator") {
    return <ModeratorPage />;
  }

  return <NotFoundPage />;
}

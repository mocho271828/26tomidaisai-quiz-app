import {
  useId,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import scoreAccent from "../../assets/participant-2026/score-accent.svg";
import scoreMuted from "../../assets/participant-2026/score-muted.svg";
import chartAccent from "../../assets/participant-2026/chart-accent.svg";
import chartMuted from "../../assets/participant-2026/chart-muted.svg";
import trophyAccent from "../../assets/participant-2026/trophy-accent.svg";
import trophyMuted from "../../assets/participant-2026/trophy-muted.svg";
import { isPracticeRound, roundLabel } from "../_utils/roundPresentation";

type ResultTabId = "answer" | "distribution" | "ranking";

type ResultTabsProps = {
  answer: ReactNode;
  distribution?: ReactNode;
  ranking?: ReactNode;
  roundNumber?: number;
};

const resultTabLabels = {
  answer: "マイスコア",
  distribution: "みんなの回答",
  ranking: "ランキング",
};

const resultTabIcons = {
  answer: [scoreMuted, scoreAccent],
  distribution: [chartMuted, chartAccent],
  ranking: [trophyMuted, trophyAccent],
};

export function ResultTabs({
  answer,
  distribution,
  ranking,
  roundNumber,
}: ResultTabsProps) {
  const [selectedTab, setSelectedTab] = useState<ResultTabId>("answer");
  const tabGroupId = useId();
  const tabs: Array<{ content: ReactNode; id: ResultTabId }> = [
    { content: answer, id: "answer" },
    ...(distribution === undefined
      ? []
      : [{ content: distribution, id: "distribution" } as const]),
    ...(ranking === undefined
      ? []
      : [{ content: ranking, id: "ranking" } as const]),
  ];
  const activeTab = tabs.some((tab) => tab.id === selectedTab)
    ? selectedTab
    : "answer";
  const selectTabFromKeyboard = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    currentTab: ResultTabId,
  ): void => {
    const currentIndex = tabs.findIndex((tab) => tab.id === currentTab);
    const lastIndex = tabs.length - 1;
    const nextIndex =
      event.key === "ArrowRight" || event.key === "ArrowDown"
        ? (currentIndex + 1) % tabs.length
        : event.key === "ArrowLeft" || event.key === "ArrowUp"
          ? (currentIndex - 1 + tabs.length) % tabs.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? lastIndex
              : null;
    if (nextIndex === null) {
      return;
    }
    event.preventDefault();
    const nextTab = tabs[nextIndex];
    if (nextTab === undefined) {
      return;
    }
    setSelectedTab(nextTab.id);
    const tabButtons = event.currentTarget.parentElement?.querySelectorAll(
      '[role="tab"]',
    );
    const nextButton = tabButtons?.item(nextIndex);
    if (nextButton instanceof HTMLButtonElement) {
      nextButton.focus();
    }
  };

  return (
    <section
      className="result-tabs result-tabs-participant"
      aria-label="クイズ結果"
    >
      <header className="participant-result-heading">
        {roundNumber === undefined ? null : <span className="participant-result-round" data-round-number={roundNumber}>{isPracticeRound(roundNumber) ? `第${roundNumber}問 · 練習` : roundLabel(roundNumber)}</span>}
        <h1>{resultTabLabels[activeTab]}</h1>
      </header>
      <div className="result-tab-list" role="tablist" aria-label="結果表示">
        <span className="result-tab-indicator" aria-hidden="true" style={{ width: `calc((100% - 10px) / ${tabs.length})`, transform: `translateX(${tabs.findIndex((tab) => tab.id === activeTab) * 100}%)` }} />
        {tabs.map((tab) => {
          const isSelected = tab.id === activeTab;
          return (
            <button
              aria-controls={`${tabGroupId}-${tab.id}-panel`}
              aria-selected={isSelected}
              id={`${tabGroupId}-${tab.id}-tab`}
              key={tab.id}
              onClick={() => setSelectedTab(tab.id)}
              onKeyDown={(event) => selectTabFromKeyboard(event, tab.id)}
              role="tab"
              tabIndex={isSelected ? 0 : -1}
              type="button"
            >
              <img alt="" src={resultTabIcons[tab.id][isSelected ? 1 : 0]} />
              {resultTabLabels[tab.id]}
            </button>
          );
        })}
      </div>
      {tabs.map((tab) => {
        const isSelected = tab.id === activeTab;
        return (
          <div
            aria-labelledby={`${tabGroupId}-${tab.id}-tab`}
            className="result-tab-panel"
            hidden={!isSelected}
            id={`${tabGroupId}-${tab.id}-panel`}
            key={tab.id}
            role="tabpanel"
            tabIndex={isSelected ? 0 : -1}
          >
            {tab.content}
          </div>
        );
      })}
    </section>
  );
}

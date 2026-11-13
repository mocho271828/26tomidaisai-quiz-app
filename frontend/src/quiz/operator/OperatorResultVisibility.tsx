import { ChartBarIcon, TrophyIcon } from "@phosphor-icons/react";

import type {
  OperatorState,
  ResultVisibilityTarget,
} from "../../api/operatorContracts";
import { roundLabel } from "../_utils/roundPresentation";
import { visibilityTargetLabels } from "./operatorUi";
import "./operatorVisibility.css";

const visibilitySettings = [
  {
    target: "distribution",
    targets: ["distribution"],
    Icon: ChartBarIcon,
    label: "みんなの回答",
    description: "参加者画面の回答数・割合",
  },
  {
    target: "rankings",
    targets: ["personal_ranking", "overall_ranking"],
    Icon: TrophyIcon,
    label: "自分の順位・みんなの順位",
    description: "本人の総合順位・回答速度順位と、みんなの順位（TOP3）をまとめて切り替えます。",
  },
] as const;

export function OperatorResultVisibility({
  isBusy,
  onChange,
  projection,
}: {
  isBusy: boolean;
  onChange: (target: ResultVisibilityTarget, visible: boolean) => Promise<void>;
  projection: Pick<OperatorState, "result_visibility" | "visibility_controls">;
}) {
  return (
    <section
      className="operator-card operator-visibility"
      aria-labelledby="result-visibility-title"
    >
      <h2 id="result-visibility-title">参加者への公開設定</h2>
      <p className="operator-visibility-intro">
        正解・結果の公開後に反映。運営の閲覧には影響しません。
      </p>
      {visibilitySettings.map(({ target, targets, Icon, label, description }) => {
        const isVisible = targets.every((item) => projection.result_visibility[item]);
        const isMixed = !isVisible && targets.some((item) => projection.result_visibility[item]);
        const automaticTargets = targets.filter((item) =>
          projection.visibility_controls[item].mode === "automatic",
        );
        const titleId = `result-visibility-${target}`;
        const descriptionId = `${titleId}-description`;
        return (
          <div className="operator-visibility-setting" key={target}>
            <Icon
              aria-hidden="true"
              className="operator-visibility-icon"
              size={22}
              weight="bold"
            />
            <div className="operator-visibility-description">
              <h3 id={titleId}>{label}</h3>
              <p id={descriptionId}>
                {description}
                {isMixed ? ` 現在の設定は${targets.map((item) =>
                  `${visibilityTargetLabels[item]}：${projection.result_visibility[item] ? "公開" : "非公開"}`,
                ).join("、")}です。押すと両方を公開します。` : null}
              </p>
              <span className="operator-visibility-mode">
                {automaticTargets.length > 0 ? "自動切替" : "手動切替"}
              </span>
            </div>
            <button
              aria-describedby={descriptionId}
              aria-labelledby={titleId}
              aria-pressed={isMixed ? "mixed" : isVisible}
              className="operator-visibility-toggle"
              disabled={isBusy}
              onClick={() => void onChange(target, !isVisible)}
              type="button"
            >
              <span aria-hidden="true" className="operator-visibility-track" />
              <strong>{isMixed ? "設定不一致" : isVisible ? "公開" : "非公開"}</strong>
            </button>
            {automaticTargets.length > 0 ? (
              <div className="operator-visibility-schedule">
                {automaticTargets.map((item) => (
                  <p key={item}>
                    {visibilityTargetLabels[item]}：
                    {projection.visibility_controls[item].changes
                      .map((change) =>
                        `${roundLabel(change.after_round)}終了後：${change.visible ? "公開" : "非公開"}`,
                      )
                      .join(" ／ ")}
                  </p>
                ))}
                <p>手動の変更は、次の自動切替まで有効です。</p>
              </div>
            ) : null}
          </div>
        );
      })}
    </section>
  );
}

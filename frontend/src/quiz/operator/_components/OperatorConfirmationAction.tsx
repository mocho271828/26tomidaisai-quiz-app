import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";

type ConfirmationProps = {
  title: string;
  confirmLabel: string;
  acknowledgement?: string;
  isDangerous?: boolean;
  isDisabled: boolean;
  children: ReactNode;
  onConfirm: () => void;
};

function ConfirmationDialog({
  title, confirmLabel, acknowledgement, isDangerous, isDisabled, children, onConfirm, onCancel,
}: ConfirmationProps & { onCancel: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const submittedRef = useRef(false);
  const [isAcknowledged, setIsAcknowledged] = useState(false);
  const titleId = useId();
  const descriptionId = useId();
  const canConfirm = !isDisabled && (acknowledgement === undefined || isAcknowledged);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog === null) return;
    const opener = document.activeElement;
    dialog.showModal();
    cancelRef.current?.focus();
    return () => {
      dialog.close();
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  }, []);

  return (
    <dialog className="operator-confirmation" ref={dialogRef} aria-modal="true"
      aria-labelledby={titleId} aria-describedby={descriptionId}
      onCancel={(event) => { event.preventDefault(); onCancel(); }}>
      <h2 id={titleId}>{title}</h2>
      <div id={descriptionId}>{children}</div>
      {acknowledgement !== undefined ? (
        <label className="operator-confirmation-check">
          <input type="checkbox" checked={isAcknowledged}
            onChange={(event) => setIsAcknowledged(event.currentTarget.checked)} />
          <span>{acknowledgement}</span>
        </label>
      ) : null}
      <div className="operator-confirmation-actions">
        <button className="secondary-button" type="button" ref={cancelRef} onClick={onCancel}>キャンセル</button>
        <button className={isDangerous ? "danger-button" : "primary-button"} type="button"
          disabled={!canConfirm} onClick={() => {
            if (!canConfirm || submittedRef.current) return;
            submittedRef.current = true;
            onConfirm();
          }}>{confirmLabel}</button>
      </div>
    </dialog>
  );
}

export function OperatorConfirmationAction({
  triggerLabel, ...props
}: ConfirmationProps & { triggerLabel: ReactNode }) {
  const [isConfirming, setIsConfirming] = useState(false);

  return (
    <>
      <button className={props.isDangerous ? "danger-button" : "secondary-button"}
        disabled={props.isDisabled} type="button" aria-haspopup="dialog"
        onClick={() => setIsConfirming(true)}>{triggerLabel}</button>
      {isConfirming ? (
        <ConfirmationDialog {...props} onCancel={() => setIsConfirming(false)} onConfirm={() => {
          setIsConfirming(false);
          props.onConfirm();
        }} />
      ) : null}
    </>
  );
}

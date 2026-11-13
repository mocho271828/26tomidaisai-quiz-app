import { vi } from "vitest";

export function mockDialogMethods(): () => void {
  const prototype = HTMLDialogElement.prototype;
  const originalShowModal = Object.getOwnPropertyDescriptor(prototype, "showModal");
  const originalClose = Object.getOwnPropertyDescriptor(prototype, "close");
  // jsdom に未実装の表示切替だけを補い、focus と cancel はアプリ側の処理を検証する。
  Object.defineProperty(prototype, "showModal", {
    configurable: true,
    value: vi.fn(function (this: HTMLDialogElement): void { this.open = true; }),
  });
  Object.defineProperty(prototype, "close", {
    configurable: true,
    value: vi.fn(function (this: HTMLDialogElement): void {
      this.open = false;
      this.dispatchEvent(new Event("close"));
    }),
  });
  return () => {
    if (originalShowModal === undefined) Reflect.deleteProperty(prototype, "showModal");
    else Object.defineProperty(prototype, "showModal", originalShowModal);
    if (originalClose === undefined) Reflect.deleteProperty(prototype, "close");
    else Object.defineProperty(prototype, "close", originalClose);
  };
}

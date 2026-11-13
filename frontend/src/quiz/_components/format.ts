export function formatDateTime(value: string | null): string {
  if (value === null) {
    return "未設定";
  }
  return new Intl.DateTimeFormat("ja-JP", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(value));
}

export function elapsedSeconds(milliseconds: number): string {
  return (Math.floor(Math.max(0, milliseconds) / 100) / 10).toFixed(1);
}

export function remainingSeconds(milliseconds: number): string {
  return (Math.ceil(Math.max(0, milliseconds) / 100) / 10).toFixed(1);
}

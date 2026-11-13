import { z } from "zod";

export const writerOptionsSchema = z.object({
  bufferMaxBytes: z.number().int().positive().default(4 * 1024 * 1024),
  bufferMaxRecords: z.number().int().positive().default(10000),
  batchMaxBytes: z.number().int().positive().default(64 * 1024),
  flushIntervalMs: z.number().int().min(1).max(5000).default(250),
}).strict();

export type WriterOptions = z.input<typeof writerOptionsSchema>;
export type WriterHealth = {
  state: "open" | "closing" | "closed" | "failed";
  complete: boolean;
  attemptedRecords: number;
  acceptedRecords: number;
  writtenRecords: number;
  droppedRecords: number;
  failedRecords: number;
  pendingRecords: number;
  pendingBytes: number;
  peakPendingRecords: number;
  peakPendingBytes: number;
  batches: number;
  writeMs: number;
  queueWaitTotalMs: number;
  queueWaitMaxMs: number;
  firstLossMonotonicMs: number | null;
  lastLossMonotonicMs: number | null;
  failure: "evidence_write_failed" | null;
};
type Entry = { text: string; bytes: number; queuedAt: number };

/** ファイルI/O中のbatchも容量へ含め、producerを待たせず欠落を明示する。 */
export class BufferedEventWriter {
  readonly options: z.output<typeof writerOptionsSchema>;
  private queue: Entry[] = [];
  private pending: Promise<void> | undefined;
  private closePromise: Promise<void> | undefined;
  private readonly timer: ReturnType<typeof setInterval>;
  private readonly metrics: WriterHealth = {
    state: "open", complete: false, attemptedRecords: 0, acceptedRecords: 0,
    writtenRecords: 0, droppedRecords: 0, failedRecords: 0,
    pendingRecords: 0, pendingBytes: 0, peakPendingRecords: 0, peakPendingBytes: 0,
    batches: 0, writeMs: 0, queueWaitTotalMs: 0, queueWaitMaxMs: 0,
    firstLossMonotonicMs: null, lastLossMonotonicMs: null, failure: null,
  };

  constructor(private readonly write: (batch: string) => Promise<void>, options: WriterOptions = {}) {
    this.options = writerOptionsSchema.parse(options);
    this.timer = setInterval(() => { this.startDrain(); }, this.options.flushIntervalMs);
    this.timer.unref();
  }

  enqueue(text: string): boolean {
    this.metrics.attemptedRecords++;
    const bytes = Buffer.byteLength(text);
    if (this.metrics.state !== "open" ||
        this.metrics.pendingRecords >= this.options.bufferMaxRecords ||
        this.metrics.pendingBytes + bytes > this.options.bufferMaxBytes) {
      this.metrics.droppedRecords++;
      this.recordLoss();
      return false;
    }
    this.queue.push({text, bytes, queuedAt: performance.now()});
    this.metrics.acceptedRecords++;
    this.metrics.pendingRecords++;
    this.metrics.pendingBytes += bytes;
    this.metrics.peakPendingRecords = Math.max(this.metrics.peakPendingRecords, this.metrics.pendingRecords);
    this.metrics.peakPendingBytes = Math.max(this.metrics.peakPendingBytes, this.metrics.pendingBytes);
    if (this.metrics.pendingBytes >= this.options.batchMaxBytes) this.startDrain();
    return true;
  }

  health(): WriterHealth { return {...this.metrics}; }

  async flush(): Promise<void> {
    // drain開始後に届いたeventも同じ直列writerが回収するため、順序は変わらない。
    do {
      this.startDrain();
      await this.pending;
    } while (this.queue.length > 0 && this.metrics.failure === null);
    if (this.metrics.failure !== null) throw new Error(this.metrics.failure);
  }

  close(): Promise<void> {
    this.closePromise ??= this.finish();
    return this.closePromise;
  }

  private async finish(): Promise<void> {
    clearInterval(this.timer);
    if (this.metrics.state === "open") this.metrics.state = "closing";
    await this.flush();
    this.metrics.state = "closed";
    this.metrics.complete = this.metrics.droppedRecords === 0 && this.metrics.failedRecords === 0;
  }

  private recordLoss(): void {
    const now = performance.now();
    this.metrics.firstLossMonotonicMs ??= now;
    this.metrics.lastLossMonotonicMs = now;
    this.metrics.complete = false;
  }

  private startDrain(): void {
    if (this.pending !== undefined || this.queue.length === 0 || this.metrics.failure !== null) return;
    // 例外はdrain内で固定codeへ変換する。timerから未処理rejectionを発生させない。
    this.pending = this.drain().finally(() => { this.pending = undefined; });
  }

  private async drain(): Promise<void> {
    while (this.queue.length > 0) {
      let size = 0;
      let count = 0;
      for (const entry of this.queue) {
        if (count > 0 && size + entry.bytes > this.options.batchMaxBytes) break;
        size += entry.bytes;
        count++;
      }
      const entries = this.queue.splice(0, count);
      const started = performance.now();
      for (const entry of entries) {
        const waitMs = started - entry.queuedAt;
        this.metrics.queueWaitTotalMs += waitMs;
        this.metrics.queueWaitMaxMs = Math.max(this.metrics.queueWaitMaxMs, waitMs);
      }
      try {
        await this.write(entries.map(entry => entry.text).join(""));
        this.metrics.writtenRecords += count;
        this.metrics.batches++;
        this.metrics.pendingRecords -= count;
        this.metrics.pendingBytes -= size;
      } catch {
        // 部分writeの有無はOSエラーから確定できないため、当該batchも失敗区間に含める。
        this.metrics.failedRecords += this.metrics.pendingRecords;
        this.metrics.pendingRecords = 0;
        this.metrics.pendingBytes = 0;
        this.queue = [];
        this.metrics.failure = "evidence_write_failed";
        this.metrics.state = "failed";
        this.recordLoss();
        clearInterval(this.timer);
        return;
      } finally {
        this.metrics.writeMs += performance.now() - started;
      }
    }
  }
}

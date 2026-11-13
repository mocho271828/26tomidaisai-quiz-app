export interface StubMetricSample {
  readonly metricKind: "counter" | "trend";
  readonly metricName: string;
  readonly value: number;
  readonly tags: Readonly<Record<string, string>>;
}

const samples: StubMetricSample[] = [];

export function resetMetricStub(): void {
  samples.length = 0;
}

export function readMetricSamples(metricName?: string): readonly StubMetricSample[] {
  return metricName === undefined
    ? samples
    : samples.filter((sample) => sample.metricName === metricName);
}

export class Counter {
  readonly #name: string;

  constructor(name: string) {
    this.#name = name;
  }

  add(value: number, tags: Readonly<Record<string, string>> = {}): void {
    samples.push({ metricKind: "counter", metricName: this.#name, value, tags });
  }
}

export class Trend {
  readonly #name: string;

  constructor(name: string, isTime = false) {
    this.#name = name;
    void isTime;
  }

  add(value: number, tags: Readonly<Record<string, string>> = {}): void {
    samples.push({ metricKind: "trend", metricName: this.#name, value, tags });
  }
}

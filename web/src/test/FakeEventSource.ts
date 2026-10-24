/** Minimal EventSource stand-in for tests: records instances and lets tests emit events. */
export class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readonly listeners = new Map<string, ((event: Event) => void)[]>();
  readyState = FakeEventSource.OPEN;
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(name: string, listener: (event: Event) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
  get closed() {
    return this.readyState === FakeEventSource.CLOSED;
  }
  emit(name: string, data: unknown = {}) {
    this.dispatch(name, new MessageEvent(name, { data: JSON.stringify(data) }));
  }
  emitRaw(name: string, data: string) {
    this.dispatch(name, new MessageEvent(name, { data }));
  }
  /** Simulates a failed connection the browser gave up on (no automatic retry). */
  fail() {
    this.readyState = FakeEventSource.CLOSED;
    this.dispatch('error', new Event('error'));
  }
  private dispatch(name: string, event: Event) {
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }
}

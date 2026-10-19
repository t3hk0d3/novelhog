import { hashKey, type QueryClient, type QueryKey } from '@tanstack/react-query';
import { useEffect, useSyncExternalStore } from 'react';
import type { Chapter, Export, Novel } from '../api/client';
import {
  type ChapterEvent,
  type DeletedNovel,
  type EventPayloads,
  type ExportDeletedEvent,
  type ExportEvent,
  type NovelDeletedEvent,
  type NovelEvent,
} from '../api/events';

const EVENTS_URL = '/api/v1/events';

// Whether the stream said `ready`; REST queries wait for it (spec 10) so nothing committed
// between the first connect and the first load is lost. One flag for the one app-wide stream,
// driven only by useLiveEvents.
let ready = false;
const listeners = new Set<() => void>();

function setLiveReady(value: boolean): void {
  if (ready === value) return;
  ready = value;
  listeners.forEach((listener) => {
    listener();
  });
}

/** True once the stream's `ready` event arrived. Gate REST queries with `enabled: useLiveReady()`. */
export function useLiveReady(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => ready,
  );
}

type Entity = { id: string };
type Updater<T> = (old: T | undefined) => T | undefined;

/** Upserts or removes entities by id in arrival order, then orders the result. */
function applyById<T extends Entity>(
  list: T[],
  changes: { id: string; value: T | null }[],
  order: (a: T, b: T) => number,
): T[] {
  const byId = new Map(list.map((item) => [item.id, item]));
  for (const { id, value } of changes) {
    if (value === null) byId.delete(id);
    else byId.set(id, value);
  }
  return [...byId.values()].sort(order);
}

const byPosition = (a: Chapter, b: Chapter) => a.position - b.position;
// RFC 3339 UTC timestamps with a fixed format compare correctly as strings; ties keep order.
const newestFirst = (a: { created_at: string }, b: { created_at: string }) =>
  a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0;

function stripNovel(event: NovelEvent): Novel {
  const novel: NovelEvent = { ...event };
  delete novel.released;
  return novel;
}

function stripChapter(event: ChapterEvent): Chapter | null {
  const { position } = event;
  if (position === null) return null;
  const chapter: Partial<ChapterEvent> = { ...event };
  delete chapter.novel_id;
  return { ...chapter, position } as Chapter;
}

type Pending = {
  novels: (NovelEvent | NovelDeletedEvent)[];
  chapters: Map<string, ChapterEvent[]>;
  exports: Map<string, (ExportEvent | ExportDeletedEvent)[]>;
};

const emptyPending = (): Pending => ({ novels: [], chapters: new Map(), exports: new Map() });

function push<T>(map: Map<string, T[]>, key: string, value: T): void {
  const bucket = map.get(key);
  if (bucket) bucket.push(value);
  else map.set(key, [value]);
}

/**
 * Writes to the cache. While the key's query is fetching, the write is also kept so it can be
 * replayed on top of the fetched data (REST may have read before the event was committed).
 * Lists that were never loaded stay unloaded (an updater returning undefined leaves the cache).
 */
class CacheWriter {
  private readonly replay = new Map<string, { key: QueryKey; updaters: Updater<unknown>[] }>();

  constructor(private readonly client: QueryClient) {}

  write<T>(key: QueryKey, updater: Updater<T>): void {
    this.client.setQueryData<T>(key, updater);
    if (this.client.getQueryState(key)?.fetchStatus !== 'fetching') return;
    const hash = hashKey(key);
    const entry = this.replay.get(hash) ?? { key, updaters: [] };
    entry.updaters.push(updater as Updater<unknown>);
    this.replay.set(hash, entry);
  }

  /** Called on every cache update: once a buffered query settles, replay or drop its buffer. */
  settle(key: QueryKey, state: { fetchStatus: string; status: string }): void {
    const hash = hashKey(key);
    const entry = this.replay.get(hash);
    if (!entry || state.fetchStatus !== 'idle') return;
    this.replay.delete(hash);
    if (state.status !== 'success') return;
    for (const updater of entry.updaters) this.client.setQueryData(entry.key, updater);
  }
}

/** Applies one batch: one cache write per list key, whatever the number of events. */
function flush(writer: CacheWriter, client: QueryClient, batch: Pending): void {
  if (batch.novels.length > 0) {
    const changes: { id: string; value: Novel | null }[] = [];
    for (const event of batch.novels) {
      if ('deleted' in event) {
        const marker: DeletedNovel = { id: event.id, deleted: true };
        writer.write(['novel', event.id], () => marker);
        client.removeQueries({ queryKey: ['chapters', event.id] });
        client.removeQueries({ queryKey: ['exports', event.id] });
        changes.push({ id: event.id, value: null });
      } else {
        const novel = stripNovel(event);
        writer.write(['novel', novel.id], () => novel);
        changes.push({ id: novel.id, value: novel });
      }
    }
    writer.write<Novel[]>(['novels'], (old) => old && applyById(old, changes, newestFirst));
  }
  for (const [novelId, events] of batch.chapters) {
    const changes = events.map((event) => ({ id: event.id, value: stripChapter(event) }));
    writer.write<Chapter[]>(
      ['chapters', novelId],
      (old) => old && applyById(old, changes, byPosition),
    );
  }
  for (const [novelId, events] of batch.exports) {
    const changes = events.map((event) => ({
      id: event.id,
      value: 'deleted' in event ? null : event,
    }));
    writer.write<Export[]>(
      ['exports', novelId],
      (old) => old && applyById(old, changes, newestFirst),
    );
  }
}

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30_000;

/**
 * Opens the app's single EventSource and patches the query cache from it. Mount once. The
 * browser resends Last-Event-ID on reconnect, so no cursor is kept here. When the browser gives
 * up on the connection (readyState CLOSED, for example after a 502 during an API restart) the
 * stream is reopened with backoff; the new stream starts with `ready` (the API has no cursor
 * parameter, spec 5.7), which reloads all REST data like a resync.
 */
export function useLiveEvents(client: QueryClient): void {
  useEffect(() => {
    const writer = new CacheWriter(client);
    const unsubscribe = client.getQueryCache().subscribe((event) => {
      if (event.type === 'updated')
        writer.settle(event.query.queryKey as QueryKey, event.query.state);
    });
    let source: EventSource | undefined;
    let batch = emptyPending();
    let frame: number | undefined;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let backoff = RECONNECT_MIN_MS;

    const schedule = () => {
      frame ??= requestAnimationFrame(() => {
        frame = undefined;
        const current = batch;
        batch = emptyPending();
        flush(writer, client, current);
      });
    };

    const connect = () => {
      const current = new EventSource(EVENTS_URL);
      source = current;
      const listen = <K extends keyof EventPayloads>(
        name: K,
        handle: (payload: EventPayloads[K]) => void,
      ) => {
        current.addEventListener(name, (event) => {
          let payload: EventPayloads[K];
          try {
            payload = JSON.parse((event as MessageEvent<string>).data) as EventPayloads[K];
          } catch {
            console.warn(`Dropped malformed ${name} event`);
            return;
          }
          handle(payload);
          schedule();
        });
      };

      listen('novel', (payload) => batch.novels.push(payload));
      listen('chapter', (payload) => {
        push(batch.chapters, payload.novel_id, payload);
      });
      listen('export', (payload) => {
        if (!('novel_id' in payload)) return;
        push(batch.exports, payload.novel_id, payload);
      });
      current.addEventListener('open', () => {
        backoff = RECONNECT_MIN_MS;
      });
      current.addEventListener('ready', () => {
        // A repeated ready (not a replay) means the stream restarted: reload like a resync.
        if (ready) void client.invalidateQueries();
        setLiveReady(true);
      });
      current.addEventListener('resync', () => {
        void client.invalidateQueries();
      });
      current.addEventListener('error', () => {
        // While readyState is CONNECTING the browser retries by itself, with Last-Event-ID.
        if (current.readyState !== EventSource.CLOSED || retryTimer !== undefined) return;
        retryTimer = setTimeout(() => {
          retryTimer = undefined;
          connect();
        }, backoff);
        backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
      });
    };
    connect();

    return () => {
      source?.close();
      clearTimeout(retryTimer);
      unsubscribe();
      if (frame !== undefined) cancelAnimationFrame(frame);
      setLiveReady(false);
    };
  }, [client]);
}

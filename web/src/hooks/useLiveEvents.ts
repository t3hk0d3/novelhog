import type { QueryClient } from '@tanstack/react-query';
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
// between the first connect and the first load is lost. One flag for the one app-wide stream.
let ready = false;
const listeners = new Set<() => void>();

/** Sets the live flag; the hook below drives it, tests may set it directly. */
export function setLiveReady(value: boolean): void {
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

/** Upserts or removes entities by id in arrival order; new ones go last. */
function applyById<T extends Entity>(list: T[], changes: { id: string; value: T | null }[]): T[] {
  const byId = new Map(list.map((item) => [item.id, item]));
  for (const { id, value } of changes) {
    if (value === null) byId.delete(id);
    else byId.set(id, value);
  }
  return [...byId.values()];
}

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
 * Applies one batch: one cache write per key, whatever the number of events. Lists that were
 * never loaded stay unloaded (an update function returning undefined leaves the cache alone).
 */
function flush(client: QueryClient, batch: Pending): void {
  if (batch.novels.length > 0) {
    const changes: { id: string; value: Novel | null }[] = [];
    for (const event of batch.novels) {
      if ('deleted' in event) {
        const marker: DeletedNovel = { id: event.id, deleted: true };
        client.setQueryData(['novel', event.id], marker);
        changes.push({ id: event.id, value: null });
      } else {
        const novel = stripNovel(event);
        client.setQueryData(['novel', novel.id], novel);
        changes.push({ id: novel.id, value: novel });
      }
    }
    client.setQueryData<Novel[]>(['novels'], (old) => old && applyById(old, changes));
  }
  for (const [novelId, events] of batch.chapters) {
    const changes = events.map((event) => ({ id: event.id, value: stripChapter(event) }));
    client.setQueryData<Chapter[]>(
      ['chapters', novelId],
      (old) => old && applyById(old, changes).sort((a, b) => a.position - b.position),
    );
  }
  for (const [novelId, events] of batch.exports) {
    const changes = events.map((event) => ({
      id: event.id,
      value: 'deleted' in event ? null : event,
    }));
    client.setQueryData<Export[]>(['exports', novelId], (old) => old && applyById(old, changes));
  }
}

/**
 * Opens the app's single EventSource and patches the query cache from it. Mount once. The
 * browser resends Last-Event-ID on reconnect, so no cursor is kept here.
 */
export function useLiveEvents(client: QueryClient): void {
  useEffect(() => {
    const source = new EventSource(EVENTS_URL);
    let batch = emptyPending();
    let frame: number | undefined;

    const schedule = () => {
      frame ??= requestAnimationFrame(() => {
        frame = undefined;
        const current = batch;
        batch = emptyPending();
        flush(client, current);
      });
    };

    const listen = <K extends keyof EventPayloads>(
      name: K,
      handle: (payload: EventPayloads[K]) => void,
    ) => {
      source.addEventListener(name, (event) => {
        try {
          handle(JSON.parse((event as MessageEvent<string>).data) as EventPayloads[K]);
        } catch {
          // a malformed payload is dropped; the next snapshot of the entity repairs the cache
          return;
        }
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
    source.addEventListener('ready', () => {
      // A repeated ready (not a replay) means the stream restarted: reload like a resync.
      if (ready) void client.invalidateQueries();
      setLiveReady(true);
    });
    source.addEventListener('resync', () => {
      void client.invalidateQueries();
    });

    return () => {
      source.close();
      if (frame !== undefined) cancelAnimationFrame(frame);
      setLiveReady(false);
    };
  }, [client]);
}

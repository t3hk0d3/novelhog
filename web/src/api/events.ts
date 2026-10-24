/**
 * SSE event payloads, written by hand from spec sections 5.7 and 4 ("Event log") because the
 * OpenAPI document does not describe the stream. Entity events are state snapshots; a delete
 * is the short `deleted` form.
 */
import type { Chapter, Export, Novel } from './client';

/** `novel` event: a snapshot, plus the positions a sync released. */
export type NovelEvent = Novel & { released?: number[] };
export type NovelDeletedEvent = { id: string; deleted: true };

/** `chapter` event: a snapshot plus its novel. `position` is null once a sync removed it. */
export type ChapterEvent = Omit<Chapter, 'position'> & {
  novel_id: string;
  position: number | null;
};

/** `export` event. */
export type ExportEvent = Export;
export type ExportDeletedEvent = { id: string; novel_id: string; deleted: true };

/**
 * `task` events (`TK` rows of the log) carry the Task object of the tasks API. The UI does not
 * listen for them yet, so `task` is deliberately not an `EventName` and not in `EVENT_NAMES`;
 * the stream still delivers them, and they advance `Last-Event-ID`.
 */

/** `ready` and `resync` carry no data. */
export type EmptyEvent = Record<string, never>;

export interface EventPayloads {
  novel: NovelEvent | NovelDeletedEvent;
  chapter: ChapterEvent;
  export: ExportEvent | ExportDeletedEvent;
  ready: EmptyEvent;
  resync: EmptyEvent;
}
export type EventName = keyof EventPayloads;
export const EVENT_NAMES: readonly EventName[] = ['novel', 'chapter', 'export', 'ready', 'resync'];

/**
 * What `['novel', id]` holds after the novel was deleted: the live-updates hook replaces the
 * cached novel with this marker. Consumers check `isDeletedNovel` and show "This novel was
 * deleted" with a link to the library.
 */
export type DeletedNovel = NovelDeletedEvent;

export function isDeletedNovel(value: unknown): value is DeletedNovel {
  return (
    typeof value === 'object' &&
    value !== null &&
    (value as { deleted?: unknown }).deleted === true &&
    typeof (value as { id?: unknown }).id === 'string'
  );
}

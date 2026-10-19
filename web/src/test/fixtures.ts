import type { Chapter, ChapterState, Novel } from '../api/client';

/** A fully populated novel; override only what a test cares about. */
export function makeNovel(overrides: Partial<Novel> = {}): Novel {
  return {
    id: 'NV000000000001',
    title: 'The Ninth Cartographer',
    host: 'royalroad.com',
    toc_url: 'https://royalroad.com/fiction/1',
    import_error: null,
    importing: false,
    cover_url: null,
    followed: true,
    syncing: false,
    refresh_interval: 3600,
    next_refresh_at: '2026-10-19T12:00:00.000Z',
    last_refreshed_at: null,
    total_pages: 0,
    downloaded_pages: 0,
    pending_pages: 0,
    error_pages: 0,
    new_pages: 0,
    created_at: '2026-10-19T10:00:00.000Z',
    updated_at: '2026-10-19T10:00:00.000Z',
    ...overrides,
  };
}

/** A downloaded chapter at `position`; override only what a test cares about. */
export function makeChapter(position: number, overrides: Partial<Chapter> = {}): Chapter {
  return {
    id: `CH${String(position).padStart(12, '0')}`,
    position,
    url: `https://royalroad.com/fiction/1/chapter/${String(position)}`,
    title: `Page ${String(position)}`,
    state: 'downloaded',
    error: null,
    error_at: null,
    has_images: false,
    released_at: '2026-10-01T10:00:00.000Z',
    queued_at: null,
    updated_at: '2026-10-01T10:00:00.000Z',
    ...overrides,
  };
}

/** Chapters 1..count; `states` maps a position to its state, everything else is downloaded. */
export function makeChapters(count: number, states: Record<number, ChapterState> = {}): Chapter[] {
  return Array.from({ length: count }, (_, i) =>
    makeChapter(i + 1, { state: states[i + 1] ?? 'downloaded' }),
  );
}

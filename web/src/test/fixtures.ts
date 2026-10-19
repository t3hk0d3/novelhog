import type { Novel } from '../api/client';

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

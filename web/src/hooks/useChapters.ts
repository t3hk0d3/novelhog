import { useQuery } from '@tanstack/react-query';
import { api, type Chapter } from '../api/client';
import { useLiveReady } from './useLiveEvents';

const PAGE_SIZE = 1000;

const MAX_ATTEMPTS = 3;

/**
 * Pages until `total`. Offsets shift when a sync inserts or removes chapters mid-load, so ids
 * are deduplicated, and the load restarts when `total` changed between pages (up to
 * MAX_ATTEMPTS; then the last result stands and live events repair any gap).
 */
async function loadAllChapters(novelId: string): Promise<Chapter[]> {
  for (let attempt = 1; ; attempt += 1) {
    const byId = new Map<string, Chapter>();
    let firstTotal: number | undefined;
    let changed = false;
    for (;;) {
      const page = await api.listChapters(novelId, { offset: byId.size, limit: PAGE_SIZE });
      firstTotal ??= page.total;
      changed ||= page.total !== firstTotal;
      const before = byId.size;
      for (const chapter of page.items) byId.set(chapter.id, chapter);
      if (byId.size >= page.total || byId.size === before) break;
    }
    if (!changed || attempt >= MAX_ATTEMPTS) {
      return [...byId.values()].sort((a, b) => a.position - b.position);
    }
  }
}

/**
 * A novel's whole chapter list in position order, loaded page by page into
 * `['chapters', novelId]` once the live stream is ready; live events patch it afterwards.
 */
export function useChapters(novelId: string) {
  return useQuery({
    queryKey: ['chapters', novelId],
    queryFn: () => loadAllChapters(novelId),
    enabled: useLiveReady(),
  });
}

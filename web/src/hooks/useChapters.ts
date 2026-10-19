import { useQuery } from '@tanstack/react-query';
import { api, type Chapter } from '../api/client';
import { useLiveReady } from './useLiveEvents';

const PAGE_SIZE = 1000;

async function loadAllChapters(novelId: string): Promise<Chapter[]> {
  const chapters: Chapter[] = [];
  for (;;) {
    const page = await api.listChapters(novelId, { offset: chapters.length, limit: PAGE_SIZE });
    chapters.push(...page.items);
    if (chapters.length >= page.total || page.items.length === 0) return chapters;
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

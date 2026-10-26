import { useQuery } from '@tanstack/react-query';
import { api, type Chapter } from '../api/client';
import { useLiveReady } from './useLiveEvents';

/**
 * The query for a chapter's stored Markdown. The chapter's `updated_at` is part of the key, so a
 * re-download (which the live events show on the chapter) fetches the new text; the details
 * preview, the reader and the reader's prefetch of its neighbours share the entries.
 */
export function chapterContentQuery(novelId: string, chapter: Chapter | undefined) {
  return {
    queryKey: ['chapter-content', novelId, chapter?.id, chapter?.updated_at],
    queryFn: () => api.getChapterContent(novelId, chapter?.id ?? ''),
  };
}

/**
 * A downloaded chapter's stored Markdown. Nothing is requested for a chapter that is not
 * downloaded: it may have no content, and the owner shows its state instead.
 */
export function useChapterContent(novelId: string, chapter: Chapter | undefined) {
  const live = useLiveReady();
  return useQuery({
    ...chapterContentQuery(novelId, chapter),
    enabled: live && chapter?.state === 'downloaded',
  });
}

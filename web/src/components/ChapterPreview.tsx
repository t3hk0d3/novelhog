import { type Chapter } from '../api/client';
import { useChapterContent } from '../hooks/useChapterContent';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { errorMessage } from '../lib/errors';
import { ChapterMarkdown } from './ChapterMarkdown';
import { Dialog } from './Dialog';

const DESKTOP = '(min-width: 720px)';

/**
 * The chapter's whole text in a dialog of its own, opened from the chapter details (a modal over
 * a modal). It shares the content query with the reader, so the reader opens instantly after it. Render it only
 * while open: mounting is what requests the text.
 */
export function ChapterPreview({
  novelId,
  chapter,
  onClose,
}: {
  novelId: string;
  chapter: Chapter;
  onClose: () => void;
}) {
  const wide = useMediaQuery(DESKTOP);
  const content = useChapterContent(novelId, chapter);
  const title = chapter.title ?? `Chapter ${String(chapter.position)}`;
  return (
    <Dialog title={title} sheet={!wide} onClose={onClose}>
      <section
        aria-label="Chapter content"
        tabIndex={0}
        className="mt-4 max-h-[60dvh] overflow-y-auto rounded-lg border border-line p-3 text-sm [overflow-wrap:anywhere]"
      >
        {content.isError ? (
          <p className="font-semibold text-danger">{errorMessage(content.error)}</p>
        ) : content.data === undefined ? (
          <p className="text-ink-muted">Loading content…</p>
        ) : (
          <ChapterMarkdown markdown={content.data.markdown} />
        )}
      </section>
      <div className="mt-4 flex border-t border-line pt-4 max-[719px]:[&>*]:flex-1 min-[720px]:justify-end">
        <button
          type="button"
          onClick={onClose}
          className="min-h-11 rounded-lg border border-line px-4 text-sm font-semibold"
        >
          Back
        </button>
      </div>
    </Dialog>
  );
}

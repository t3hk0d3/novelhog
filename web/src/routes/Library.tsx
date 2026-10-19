import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { NovelCard } from '../components/NovelCard';
import { useLiveReady } from '../hooks/useLiveEvents';
import { errorMessage } from '../lib/errors';

/** The library (`/`): one card per novel, newest first (spec section 10, screen 1). */
export function Library({ onAddNovel }: { onAddNovel: () => void }) {
  const novels = useQuery({
    queryKey: ['novels'],
    queryFn: api.listNovels,
    enabled: useLiveReady(),
  });

  return (
    <section>
      <h1 className="mb-4 text-2xl font-bold">Library</h1>
      {novels.isError && (
        <p role="alert" className="text-[var(--status-error-tab)]">
          {errorMessage(novels.error)}
        </p>
      )}
      {novels.data?.length === 0 && (
        <div className="rounded-xl border border-dashed border-line p-8 text-center">
          <p className="mb-4 text-ink-muted">
            Your library is empty. Add the table-of-contents page of a novel and novelhog will
            import its chapters and keep checking for new ones.
          </p>
          <button
            type="button"
            onClick={onAddNovel}
            className="rounded-lg bg-accent px-4 py-2 font-semibold text-accent-ink"
          >
            Add novel
          </button>
        </div>
      )}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {novels.data?.map((novel) => (
          <NovelCard key={novel.id} novel={novel} />
        ))}
      </div>
    </section>
  );
}

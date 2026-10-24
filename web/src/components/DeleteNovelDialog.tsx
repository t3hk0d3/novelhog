import { useMutation } from '@tanstack/react-query';
import { useNavigate } from 'react-router';
import { api, type Novel } from '../api/client';
import { errorMessage } from '../lib/errors';
import { Dialog } from './Dialog';

/** Confirmation naming the novel and its downloaded chapters; on success goes to the library. */
export function DeleteNovelDialog({ novel, onClose }: { novel: Novel; onClose: () => void }) {
  const navigate = useNavigate();
  const remove = useMutation({
    mutationFn: () => api.deleteNovel(novel.id),
    onSuccess: () => {
      void navigate('/');
    },
  });
  const count = novel.downloaded_pages;
  return (
    <Dialog title="Delete novel" onClose={onClose}>
      <p className="mt-4">
        Delete <strong>{novel.title ?? novel.host}</strong> with its {String(count)} downloaded{' '}
        {count === 1 ? 'chapter' : 'chapters'} and all its exports? This cannot be undone.
      </p>
      {remove.isError && (
        <p role="alert" className="mt-3 text-danger">
          {errorMessage(remove.error)}
        </p>
      )}
      <div className="mt-6 flex justify-end gap-2">
        <button
          type="button"
          data-autofocus
          onClick={onClose}
          className="rounded-lg border border-line px-4 py-2 font-semibold"
        >
          Cancel
        </button>
        <button
          type="button"
          disabled={remove.isPending}
          onClick={() => {
            remove.mutate();
          }}
          className="rounded-lg bg-danger px-4 py-2 font-semibold text-page disabled:opacity-60"
        >
          Delete
        </button>
      </div>
    </Dialog>
  );
}

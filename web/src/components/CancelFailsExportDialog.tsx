import type { Export } from '../api/client';
import { Dialog } from './Dialog';

interface Props {
  title: string;
  /** The waiting exports the cancellation would fail. */
  needed: Export[];
  /** "this chapter" or "these chapters". */
  subject: string;
  onKeep: () => void;
  onConfirm: () => void;
}

/** Confirmation before cancelling queued chapters that waiting exports need (spec section 10). */
export function CancelFailsExportDialog({ title, needed, subject, onKeep, onConfirm }: Props) {
  return (
    <Dialog title={title} onClose={onKeep}>
      <p className="mt-4 text-sm">
        {needed.map((e) => `“${e.filename}”`).join(', ')}{' '}
        {needed.length === 1 ? 'is a waiting export that needs' : 'are waiting exports that need'}{' '}
        {subject}. Cancelling will fail {needed.length === 1 ? 'the export' : 'those exports'}.
      </p>
      <div className="mt-6 flex justify-end gap-2">
        <button
          type="button"
          data-autofocus
          onClick={onKeep}
          className="min-h-11 rounded-lg border border-line px-4 text-sm font-semibold"
        >
          Keep queued
        </button>
        <button
          type="button"
          onClick={onConfirm}
          className="min-h-11 rounded-lg bg-danger px-4 text-sm font-semibold text-page"
        >
          Cancel queued
        </button>
      </div>
    </Dialog>
  );
}

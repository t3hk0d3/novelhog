import type { Selection } from '../hooks/useSelection';

/**
 * The chapters heading row (spec section 10): a Select button in normal mode; in select mode
 * "N selected", Select all (every visible row) and Done. Also holds the live region that
 * announces entering select mode.
 */
export function SelectToolbar({ selection }: { selection: Selection }) {
  const link = 'min-h-11 px-2 text-sm font-semibold text-accent';
  return (
    <div className="flex items-center justify-between gap-3">
      <p role="status" className="sr-only">
        {selection.announcement}
      </p>
      {selection.active ? (
        <>
          <p className="font-bold">{`${String(selection.selected.length)} selected`}</p>
          <div className="flex items-center gap-2">
            <button type="button" onClick={selection.selectAll} className={link}>
              Select all
            </button>
            <button
              type="button"
              onClick={selection.exit}
              className="min-h-11 rounded-lg bg-accent px-4 text-sm font-semibold text-accent-ink"
            >
              Done
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="font-bold">Chapters</p>
          <button
            type="button"
            onClick={() => {
              selection.enter();
            }}
            className="min-h-11 rounded-lg border border-line bg-surface px-4 text-sm font-semibold"
          >
            Select
          </button>
        </>
      )}
    </div>
  );
}

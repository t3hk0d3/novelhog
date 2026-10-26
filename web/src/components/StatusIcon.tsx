import type { ChapterState } from '../api/client';

export const STATE_LABELS: Record<ChapterState, string> = {
  not_requested: 'Not requested',
  pending: 'Pending',
  downloading: 'Downloading',
  downloaded: 'Downloaded',
  error: 'Error',
};

/** CSS variable holding the icon (foreground) colour of a state; it pairs with TAB_COLOR_VAR. */
export const ICON_COLOR_VAR: Record<ChapterState, string> = {
  not_requested: 'var(--status-not-requested-icon)',
  pending: 'var(--status-pending-icon)',
  downloading: 'var(--status-downloading-icon)',
  downloaded: 'var(--status-downloaded-icon)',
  error: 'var(--status-error-icon)',
};

/** CSS variable holding the tab (background) colour of a state. */
export const TAB_COLOR_VAR: Record<ChapterState, string> = {
  not_requested: 'var(--status-not-requested-tab)',
  pending: 'var(--status-pending-tab)',
  downloading: 'var(--status-downloading-tab)',
  downloaded: 'var(--status-downloaded-tab)',
  error: 'var(--status-error-tab)',
};

function Glyph({ state }: { state: ChapterState }) {
  switch (state) {
    case 'not_requested':
      return <circle cx="12" cy="12" r="8" />;
    case 'pending':
      return (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 2" />
        </>
      );
    case 'downloading':
      return <path d="M12 4v12M6 11l6 6 6-6M5 20h14" />;
    case 'downloaded':
      return (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M8 12.5l3 3 5-6" />
        </>
      );
    case 'error':
      return (
        <>
          <path d="M12 3.5L22 20H2z" />
          <path d="M12 10v4.5M12 17.5v.01" />
        </>
      );
  }
}

/** The glyph of a state alone, any size, hidden from screen readers (the owner says the state). */
export function StatusGlyph({ state, size = 22 }: { state: ChapterState; size?: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke={ICON_COLOR_VAR[state]}
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      data-state={state}
    >
      <Glyph state={state} />
    </svg>
  );
}

/** Display-only status icon with a screen-reader label (spec section 10 status table). */
export function StatusIcon({ state }: { state: ChapterState }) {
  return (
    <>
      <StatusGlyph state={state} />
      <span className="sr-only">{STATE_LABELS[state]}</span>
    </>
  );
}

interface FollowToggleProps {
  followed: boolean;
  /** Called with the new value when the user toggles. */
  onChange: (followed: boolean) => void;
  disabled?: boolean;
}

/** Pill button with a star (spec section 10): gold and filled when following, grey outline if not. */
export function FollowToggle({ followed, onChange, disabled = false }: FollowToggleProps) {
  const tone = followed
    ? 'border-gold/50 bg-gold/15 text-gold'
    : 'border-line bg-surface text-ink-muted';
  return (
    <button
      type="button"
      aria-pressed={followed}
      disabled={disabled}
      onClick={() => {
        onChange(!followed);
      }}
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-semibold disabled:opacity-60 ${tone}`}
    >
      <svg
        viewBox="0 0 24 24"
        width="16"
        height="16"
        fill={followed ? 'currentColor' : 'none'}
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z" />
      </svg>
      {followed ? 'Following' : 'Follow'}
    </button>
  );
}

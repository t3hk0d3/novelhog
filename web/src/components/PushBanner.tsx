import { useState, type ReactNode } from 'react';
import type { Push } from '../hooks/usePush';
import { errorMessage } from '../lib/errors';
import { BellIcon, BellOffIcon, CloseIcon } from './icons';
import { useToast } from './Toast';

const DISMISSED_KEY = 'novelhog.pushBannerDismissed';
const BLOCKED_DISMISSED_KEY = 'novelhog.pushBlockedBannerDismissed';

/** Storage errors only make the banner reappear, so they are swallowed. */
function isDismissed(key: string): boolean {
  try {
    return localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

function storeDismissed(key: string): void {
  try {
    localStorage.setItem(key, '1');
  } catch {
    // the banner shows again next load
  }
}

/** Banner below the header offering device notifications; see spec 10. */
export function PushBanner({ push }: { push: Push }) {
  const toast = useToast();
  const { status } = push;
  const [dismissed, setDismissed] = useState(() => ({
    default: isDismissed(DISMISSED_KEY),
    blocked: isDismissed(BLOCKED_DISMISSED_KEY),
  }));

  let kind: 'default' | 'blocked' | 'ios';
  if (status === 'off') kind = 'default';
  else if (status === 'blocked') kind = 'blocked';
  else if (status === 'install-first') kind = 'ios';
  else return null;

  const blocked = kind === 'blocked';
  if (blocked ? dismissed.blocked : dismissed.default) return null;

  const dismiss = () => {
    storeDismissed(blocked ? BLOCKED_DISMISSED_KEY : DISMISSED_KEY);
    setDismissed((old) => (blocked ? { ...old, blocked: true } : { ...old, default: true }));
  };
  const turnOn = () => {
    push.subscribe().catch((error: unknown) => {
      toast.show(errorMessage(error));
    });
  };

  let text: ReactNode;
  if (kind === 'default')
    text = 'Get a notification on this device when a followed novel has new chapters.';
  else if (kind === 'blocked') {
    text =
      "Notifications are blocked for this site. Allow them in your browser's site settings, then reload the page.";
  } else {
    text = (
      <>
        To get notifications on iPhone, add novelhog to your Home Screen first: tap{' '}
        <strong>Share</strong>, then <strong>Add to Home Screen</strong>, and open it from there.
      </>
    );
  }

  return (
    <section
      aria-label="Notifications"
      className={`flex flex-wrap items-center gap-3 border-b px-4 py-2 text-sm ${
        blocked ? 'border-gold bg-gold/15 text-gold-ink' : 'border-line bg-accent/10 text-ink'
      }`}
    >
      {blocked ? <BellOffIcon /> : <BellIcon />}
      <p className="min-w-0 flex-1 basis-60">{text}</p>
      {kind === 'default' && (
        <button
          type="button"
          onClick={turnOn}
          disabled={push.busy}
          className="rounded-lg bg-accent px-3 py-1 text-sm font-semibold text-accent-ink disabled:opacity-60"
        >
          Turn on
        </button>
      )}
      <button
        type="button"
        aria-label="Dismiss"
        onClick={dismiss}
        className="rounded-lg p-1 hover:bg-page/60"
      >
        <CloseIcon />
      </button>
    </section>
  );
}

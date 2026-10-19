import type { Push, PushStatus } from '../hooks/usePush';
import { errorMessage } from '../lib/errors';
import { BellFilledIcon, BellIcon, BellOffIcon } from './icons';
import { useToast } from './Toast';

/** Tooltips for the states in which the bell is shown struck through and disabled. */
const UNAVAILABLE: Partial<Record<PushStatus, string>> = {
  'needs-https': 'Notifications need HTTPS',
  blocked: 'Blocked in browser settings',
  'install-first': 'Add novelhog to your Home Screen to get notifications',
  unsupported: 'Notifications are not supported in this browser',
};

/** Header bell: toggles notifications on this device. Hidden when the server has Web Push off. */
export function NotificationsBell({ push }: { push: Push }) {
  const toast = useToast();
  const { status } = push;
  if (status === 'hidden') return null;

  const reason = UNAVAILABLE[status];
  const on = status === 'on';
  const toggle = () => {
    (on ? push.unsubscribe() : push.subscribe()).catch((error: unknown) => {
      toast.show(errorMessage(error));
    });
  };
  return (
    <button
      type="button"
      aria-label={`Notifications on this device: ${on ? 'on' : 'off'}`}
      title={reason}
      disabled={reason !== undefined || status === 'checking' || push.busy}
      onClick={toggle}
      className="rounded-lg p-1.5 text-ink hover:bg-page disabled:text-ink-muted disabled:opacity-60"
    >
      {reason !== undefined ? <BellOffIcon /> : on ? <BellFilledIcon /> : <BellIcon />}
    </button>
  );
}

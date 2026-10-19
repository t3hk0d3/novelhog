import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import { useLiveReady } from './useLiveEvents';

/** localStorage key for this device's subscription id (spec 5.6). */
export const SUBSCRIPTION_ID_KEY = 'novelhog.pushSubscriptionId';

/**
 * Why the bell is shown the way it is:
 * `hidden` (server has Web Push off, or settings not loaded: no bell), `needs-https`,
 * `install-first` (iOS outside an installed PWA), `unsupported` (browser lacks Push),
 * `blocked` (permission denied), `checking` (asking the browser), `off`, `on`.
 */
export type PushStatus =
  | 'hidden'
  | 'needs-https'
  | 'install-first'
  | 'unsupported'
  | 'blocked'
  | 'checking'
  | 'off'
  | 'on';

export interface Push {
  status: PushStatus;
  /** True while a subscribe or unsubscribe is in flight. */
  busy: boolean;
  subscribe: () => Promise<void>;
  unsubscribe: () => Promise<void>;
}

function isIos(): boolean {
  const { userAgent, maxTouchPoints } = navigator;
  // iPadOS 13+ reports itself as a Mac with a touch screen.
  return /iPhone|iPad|iPod/.test(userAgent) || (/Macintosh/.test(userAgent) && maxTouchPoints > 1);
}

function isInstalled(): boolean {
  const standalone = (navigator as Navigator & { standalone?: boolean }).standalone === true;
  return standalone || window.matchMedia('(display-mode: standalone)').matches;
}

function canPush(): boolean {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

/** The environment-only part of the status; undefined means "ask the browser's subscription". */
function environmentStatus(): PushStatus | undefined {
  if (!window.isSecureContext) return 'needs-https';
  if (isIos() && !isInstalled()) return 'install-first';
  if (!canPush()) return 'unsupported';
  if (Notification.permission === 'denied') return 'blocked';
  return undefined;
}

function urlBase64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const base64 = value
    .replace(/-/g, '+')
    .replace(/_/g, '/')
    .padEnd(Math.ceil(value.length / 4) * 4, '=');
  const raw = atob(base64);
  return Uint8Array.from(raw, (char) => char.charCodeAt(0));
}

function readStoredId(): string | null {
  try {
    return localStorage.getItem(SUBSCRIPTION_ID_KEY);
  } catch {
    return null;
  }
}

function writeStoredId(id: string | null): void {
  try {
    if (id === null) localStorage.removeItem(SUBSCRIPTION_ID_KEY);
    else localStorage.setItem(SUBSCRIPTION_ID_KEY, id);
  } catch {
    // unavailable storage: unsubscribing falls back to re-subscribing for the id
  }
}

async function register(subscription: PushSubscription): Promise<string> {
  const { endpoint, keys } = subscription.toJSON();
  if (!endpoint || !keys?.p256dh || !keys.auth)
    throw new Error('The browser returned an incomplete subscription');
  const saved = await api.createWebpushSubscription({
    endpoint,
    keys: { p256dh: keys.p256dh, auth: keys.auth },
  });
  return saved.id;
}

async function browserSubscribe(vapidKey: string): Promise<PushSubscription> {
  const { pushManager } = await navigator.serviceWorker.ready;
  const options = { userVisibleOnly: true, applicationServerKey: urlBase64ToBytes(vapidKey) };
  try {
    return await pushManager.subscribe(options);
  } catch (error) {
    // The server's VAPID key changed (e.g. an old database was restored): drop the stale one.
    if (!(error instanceof DOMException) || error.name !== 'InvalidStateError') throw error;
    await (await pushManager.getSubscription())?.unsubscribe();
    return pushManager.subscribe(options);
  }
}

/** Whether this device receives push notifications, and how to turn that on or off. */
export function usePush(): Push {
  const live = useLiveReady();
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    enabled: live,
    staleTime: Infinity,
  });
  const webpush = settings.data?.notifications.channels.webpush;
  const vapidKey = webpush?.vapid_public_key ?? null;
  const enabled = webpush?.enabled === true && vapidKey !== null;

  const [subscribed, setSubscribed] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [, rerender] = useState(0);

  const environment = enabled ? environmentStatus() : 'hidden';
  const asking = enabled && environment === undefined;

  useEffect(() => {
    if (!asking) return;
    let cancelled = false;
    void navigator.serviceWorker.ready
      .then((registration) => registration.pushManager.getSubscription())
      .then((subscription) => {
        if (!cancelled) setSubscribed(subscription !== null);
      })
      .catch(() => {
        if (!cancelled) setSubscribed(false);
      });
    return () => {
      cancelled = true;
    };
  }, [asking]);

  const subscribe = useCallback(async () => {
    if (vapidKey === null) return;
    setBusy(true);
    try {
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        rerender((n) => n + 1); // a denial turns the status into `blocked`
        return;
      }
      const subscription = await browserSubscribe(vapidKey);
      writeStoredId(await register(subscription));
      setSubscribed(true);
    } finally {
      setBusy(false);
    }
  }, [vapidKey]);

  const unsubscribe = useCallback(async () => {
    setBusy(true);
    try {
      const { pushManager } = await navigator.serviceWorker.ready;
      const subscription = await pushManager.getSubscription();
      // Without a stored id, registering the same endpoint again returns the same id.
      const id = readStoredId() ?? (subscription ? await register(subscription) : null);
      await subscription?.unsubscribe();
      setSubscribed(false);
      writeStoredId(null);
      try {
        if (id !== null) await api.deleteSubscription(id);
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404)) throw error; // already gone
      }
    } finally {
      setBusy(false);
    }
  }, []);

  let status: PushStatus;
  if (environment !== undefined) status = environment;
  else if (subscribed === null) status = 'checking';
  else status = subscribed ? 'on' : 'off';
  return { status, busy, subscribe, unsubscribe };
}

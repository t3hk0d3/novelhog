import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import { useLiveReady } from './useLiveEvents';

/** localStorage key for this device's subscription id (spec 5.6). */
export const SUBSCRIPTION_ID_KEY = 'novelhog.pushSubscriptionId';

/** How long to wait for the service worker before treating push as unsupported. */
export const WORKER_READY_TIMEOUT_MS = 10_000;

/** The service worker did not become ready (registration failed or it never activated). */
export class WorkerUnavailable extends Error {
  constructor() {
    super('Notifications are unavailable: the service worker did not start');
    this.name = 'WorkerUnavailable';
  }
}

/**
 * Why the bell is shown the way it is:
 * `hidden` (server has Web Push off, or settings not loaded: no bell), `needs-https`,
 * `install-first` (iOS outside an installed PWA), `unsupported` (browser lacks Push, or the
 * service worker did not start within `WORKER_READY_TIMEOUT_MS`),
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

/** `navigator.serviceWorker.ready`, which never settles when registration failed, with a timeout. */
async function workerReady(): Promise<ServiceWorkerRegistration> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      reject(new WorkerUnavailable());
    }, WORKER_READY_TIMEOUT_MS);
  });
  try {
    return await Promise.race([navigator.serviceWorker.ready, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

function sameKey(key: ArrayBuffer | null, expected: Uint8Array): boolean {
  if (key === null) return false;
  const actual = new Uint8Array(key);
  return actual.length === expected.length && actual.every((byte, i) => byte === expected[i]);
}

/**
 * Makes the server know this device's existing subscription again (spec 11.1: a restored or
 * reset database, or a rotated subscription). A subscription made with another VAPID key is
 * replaced first. Re-posting is an idempotent upsert; the returned id is stored.
 */
async function resync(pushManager: PushManager, subscription: PushSubscription, vapidKey: string) {
  let current = subscription;
  const key = urlBase64ToBytes(vapidKey);
  if (!sameKey(current.options.applicationServerKey, key)) {
    await current.unsubscribe();
    current = await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  }
  writeStoredId(await register(current));
}

async function browserSubscribe(vapidKey: string): Promise<PushSubscription> {
  const { pushManager } = await workerReady();
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
  const [workerMissing, setWorkerMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [, rerender] = useState(0);

  const environment = enabled ? environmentStatus() : 'hidden';
  const asking = enabled && environment === undefined;

  useEffect(() => {
    if (!asking) return;
    let cancelled = false;
    const check = async () => {
      let pushManager: PushManager;
      try {
        ({ pushManager } = await workerReady());
      } catch {
        if (!cancelled) setWorkerMissing(true);
        return;
      }
      const subscription = await pushManager.getSubscription();
      if (subscription !== null && Notification.permission === 'granted') {
        await resync(pushManager, subscription, vapidKey);
      }
      if (!cancelled) setSubscribed(subscription !== null);
    };
    check().catch((error: unknown) => {
      // The server does not have this device: show "off" so Turn on registers it again.
      console.warn('Push subscription check failed', error);
      if (!cancelled) setSubscribed(false);
    });
    return () => {
      cancelled = true;
    };
  }, [asking, vapidKey]);

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
      try {
        writeStoredId(await register(subscription));
      } catch (error) {
        // Do not leave a browser subscription the server does not know: the bell would show "on".
        await subscription.unsubscribe().catch(() => false);
        throw error;
      }
      setSubscribed(true);
    } catch (error) {
      if (error instanceof WorkerUnavailable) setWorkerMissing(true);
      throw error;
    } finally {
      setBusy(false);
    }
  }, [vapidKey]);

  const unsubscribe = useCallback(async () => {
    setBusy(true);
    try {
      const { pushManager } = await workerReady();
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
  else if (workerMissing) status = 'unsupported';
  else if (subscribed === null) status = 'checking';
  else status = subscribed ? 'on' : 'off';
  return { status, busy, subscribe, unsubscribe };
}

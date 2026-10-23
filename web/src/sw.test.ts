import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

type Handler = (event: Record<string, unknown>) => void;

const source = readFileSync(resolve(__dirname, '../public/sw.js'), 'utf8');

interface FakeClient {
  url: string;
  focus: ReturnType<typeof vi.fn>;
  navigate: ReturnType<typeof vi.fn>;
}

function client(url: string): FakeClient {
  return { url, focus: vi.fn().mockResolvedValue(undefined), navigate: vi.fn() };
}

const handlers: Record<string, Handler> = {};
const showNotification = vi.fn();
const pushSubscribe = vi.fn();
const fetchMock = vi.fn();
const matchAll = vi.fn();
const openWindow = vi.fn();

beforeEach(() => {
  Object.keys(handlers).forEach((key) => Reflect.deleteProperty(handlers, key));
  showNotification.mockReset().mockResolvedValue(undefined);
  matchAll.mockReset().mockResolvedValue([]);
  openWindow.mockReset().mockResolvedValue(undefined);
  pushSubscribe.mockReset().mockResolvedValue({
    toJSON: () => ({ endpoint: 'https://push/new', keys: { p256dh: 'pk', auth: 'ak' } }),
  });
  fetchMock.mockReset().mockImplementation((url: string) =>
    Promise.resolve(
      url === '/api/v1/settings'
        ? Response.json({
            notifications: { channels: { webpush: { enabled: true, vapid_public_key: 'BAUD' } } },
          })
        : Response.json({ id: 'SB2' }, { status: 201 }),
    ),
  );
  const self = {
    addEventListener: (type: string, handler: Handler) => {
      handlers[type] = handler;
    },
    location: { origin: 'https://h' },
    registration: { showNotification, pushManager: { subscribe: pushSubscribe } },
    clients: { matchAll, openWindow },
  };
  runInNewContext(source, { self, URL, atob, fetch: fetchMock });
});

/** Runs a handler and awaits whatever it passed to event.waitUntil. */
async function dispatch(type: string, event: Record<string, unknown>): Promise<void> {
  let pending: Promise<unknown> | undefined;
  handlers[type]?.({
    ...event,
    waitUntil: (promise: Promise<unknown>) => {
      pending = promise;
    },
  });
  await pending;
}

describe('sw.js', () => {
  describe('push', () => {
    it('shows a notification with the payload title, body and icon', async () => {
      const payload = { title: 'Novel', body: '3 new chapters', icon: 'https://x/c.png', url: 'u' };
      await dispatch('push', { data: { json: () => payload } });
      expect(showNotification).toHaveBeenCalledWith('Novel', {
        body: '3 new chapters',
        icon: 'https://x/c.png',
        data: { url: 'u' },
      });
    });

    it('falls back to the app title and icon when the payload is not JSON', async () => {
      await dispatch('push', {
        data: {
          json: () => {
            throw new SyntaxError('bad');
          },
        },
      });
      expect(showNotification).toHaveBeenCalledWith(
        'novelhog',
        expect.objectContaining({ icon: '/icons/192.png' }),
      );
    });
  });

  describe('notificationclick', () => {
    const notificationFor = (url: string) => ({ close: vi.fn(), data: { url } });

    it('closes the notification and opens the url when no tab is open on it', async () => {
      const notification = notificationFor('https://h/novels/NV1');
      matchAll.mockResolvedValue([client('https://h/')]);
      await dispatch('notificationclick', { notification });
      expect(notification.close).toHaveBeenCalled();
      expect(openWindow).toHaveBeenCalledWith('https://h/novels/NV1');
    });

    it('opens the app root instead of a cross-origin url', async () => {
      await dispatch('notificationclick', {
        notification: notificationFor('https://evil.example/x'),
      });
      expect(openWindow).toHaveBeenCalledWith('https://h/');
    });

    it('focuses an open tab on the url without navigating', async () => {
      const tab = client('https://h/novels/NV1');
      matchAll.mockResolvedValue([client('https://h/'), tab]);
      await dispatch('notificationclick', {
        notification: notificationFor('https://h/novels/NV1'),
      });
      expect(tab.focus).toHaveBeenCalled();
      expect(tab.navigate).not.toHaveBeenCalled();
      expect(openWindow).not.toHaveBeenCalled();
    });

    it('focuses a tab on the same page and navigates it to the url fragment', async () => {
      const tab = client('https://h/novels/NV1');
      matchAll.mockResolvedValue([tab]);
      await dispatch('notificationclick', {
        notification: notificationFor('https://h/novels/NV1#exports'),
      });
      expect(tab.focus).toHaveBeenCalled();
      expect(tab.navigate).toHaveBeenCalledWith('https://h/novels/NV1#exports');
    });
  });

  describe('pushsubscriptionchange', () => {
    it('resubscribes with the server VAPID key', async () => {
      await dispatch('pushsubscriptionchange', {});
      expect(fetchMock).toHaveBeenCalledWith('/api/v1/settings');
      const options = pushSubscribe.mock.calls[0]?.[0] as {
        userVisibleOnly: boolean;
        applicationServerKey: Uint8Array;
      };
      expect(options.userVisibleOnly).toBe(true);
      expect([...options.applicationServerKey]).toEqual([4, 5, 3]);
    });

    it('POSTs the new subscription', async () => {
      await dispatch('pushsubscriptionchange', {});
      const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
      expect(url).toBe('/api/v1/webpush/subscriptions');
      expect(init.method).toBe('POST');
      expect(JSON.parse(init.body as string)).toEqual({
        endpoint: 'https://push/new',
        keys: { p256dh: 'pk', auth: 'ak' },
      });
    });

    it('does nothing when Web Push is off on the server', async () => {
      fetchMock.mockImplementation(() =>
        Promise.resolve(
          Response.json({
            notifications: { channels: { webpush: { enabled: false, vapid_public_key: null } } },
          }),
        ),
      );
      await dispatch('pushsubscriptionchange', {});
      expect(pushSubscribe).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
  });
});

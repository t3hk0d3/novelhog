import { vi } from 'vitest';

/** The browser pieces usePush talks to, faked; stubs them onto window and navigator. */
export function fakeBrowser({ permission = 'default', subscribed = false, secure = true } = {}) {
  const subscription = {
    endpoint: 'https://fcm.googleapis.com/send/abc',
    toJSON: () => ({
      endpoint: 'https://fcm.googleapis.com/send/abc',
      keys: { p256dh: 'pk', auth: 'ak' },
    }),
    unsubscribe: vi.fn().mockResolvedValue(true),
  };
  const pushManager = {
    getSubscription: vi.fn().mockResolvedValue(subscribed ? subscription : null),
    subscribe: vi.fn().mockResolvedValue(subscription),
  };
  const requestPermission = vi.fn().mockImplementation(() => {
    notification.permission = 'granted';
    return Promise.resolve('granted');
  });
  const notification = { permission, requestPermission };
  vi.stubGlobal('Notification', notification);
  vi.stubGlobal('PushManager', vi.fn());
  vi.stubGlobal('isSecureContext', secure);
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: { ready: Promise.resolve({ pushManager }), register: vi.fn() },
  });
  return { subscription, pushManager, requestPermission, notification };
}

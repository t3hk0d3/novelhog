/* novelhog service worker (spec 8.6, 10): shows Web Push messages and opens/focuses their url.
   No caching and no exports: handlers are registered on `self`. */

self.addEventListener('push', (event) => {
  let message = {};
  try {
    const parsed = event.data ? event.data.json() : {};
    if (parsed && typeof parsed === 'object') message = parsed;
  } catch {
    // a push without a JSON payload still shows a generic notification
  }
  const title = message.title || 'novelhog';
  event.waitUntil(
    self.registration.showNotification(title, {
      body: message.body,
      icon: message.icon || '/icons/192.png',
      data: { url: message.url },
    }),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const requested = event.notification.data && event.notification.data.url;
  if (!requested) return;
  // Only same-origin urls are opened; anything else falls back to the app root.
  let target = new URL('/', self.location.origin);
  try {
    const parsed = new URL(requested, self.location.origin);
    if (parsed.origin === target.origin) target = parsed;
  } catch {
    // unparsable url: keep the root
  }
  const url = target.href;
  const withoutHash = (value) => value.split('#')[0];
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (windows) => {
      const open = windows.find((client) => withoutHash(client.url) === withoutHash(url));
      if (!open) return self.clients.openWindow(url);
      await open.focus();
      if (open.url !== url) await open.navigate(url);
      return undefined;
    }),
  );
});

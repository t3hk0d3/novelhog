/* novelhog service worker (spec 8.6, 10): shows Web Push messages and opens/focuses their url.
   No caching and no exports: handlers are registered on `self`. */

self.addEventListener('push', (event) => {
  let message = {};
  try {
    message = event.data ? event.data.json() : {};
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
  const url = event.notification.data && event.notification.data.url;
  if (!url) return;
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

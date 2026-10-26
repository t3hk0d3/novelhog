/* novelhog service worker (spec 8.6, 10): shows Web Push messages and opens/focuses their url,
   and registers a subscription the browser replaced. No caching and no exports: handlers are
   registered on `self`. */

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

function urlBase64ToBytes(value) {
  const base64 = value
    .replace(/-/g, '+')
    .replace(/_/g, '/')
    .padEnd(Math.ceil(value.length / 4) * 4, '=');
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0));
}

/* The browser replaced or dropped the subscription: subscribe again with the server's current
   VAPID key and register it (an idempotent upsert). The app re-registers on its next load too. */
async function resubscribe() {
  const settings = await fetch('/api/v1/settings');
  if (!settings.ok) return;
  const body = await settings.json();
  const webpush = body && body.notifications && body.notifications.channels.webpush;
  if (!webpush || !webpush.enabled || !webpush.vapid_public_key) return;
  const subscription = await self.registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToBytes(webpush.vapid_public_key),
  });
  const { endpoint, keys } = subscription.toJSON();
  await fetch('/api/v1/webpush/subscriptions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ endpoint, keys: { p256dh: keys.p256dh, auth: keys.auth } }),
  });
}

self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil(resubscribe());
});

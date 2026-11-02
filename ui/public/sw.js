// HUD service worker: only so approval notifications work on Android, where Chrome shows them through
// a service worker and nothing else. No caching, no push server: the open HUD page raises them.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const tabs = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const hud = tabs.find((c) => new URL(c.url).origin === self.location.origin);
    if (hud) return hud.focus();
    return self.clients.openWindow("/");
  })());
});

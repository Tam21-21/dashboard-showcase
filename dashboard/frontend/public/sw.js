// Retired service worker. The old one passed every request through fetch(): it cached nothing, and a
// fetch made by a service worker can keep the browser's password window from opening, which left the
// iPhone home screen app on a blank page. Chrome no longer needs a service worker to install the app.
// A browser that still holds the old worker fetches this file on its next visit, and it removes itself.
self.addEventListener('install', function () {
  self.skipWaiting();
});
self.addEventListener('activate', function () {
  self.registration.unregister();
});

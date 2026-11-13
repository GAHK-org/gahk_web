// gahk.dk once served the intranet, with a service worker and push notifications on this origin.
// The intranet now lives on intern.gahk.dk. Browsers that still run the old worker fetch this file
// when they check for an update; it takes over, drops the old push subscription and removes itself.
// Residents turn push notifications on again on intern.gahk.dk.
self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    self.registration.pushManager
      .getSubscription()
      .then(function (subscription) {
        return subscription ? subscription.unsubscribe() : false;
      })
      .catch(function () {
        return false;
      })
      .then(function () {
        return self.registration.unregister();
      })
  );
});

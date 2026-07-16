/* =========================================================
   Jodala Microfinance -- PWA install/service-worker bootstrap
   ========================================================= */
'use strict';

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Non-fatal -- the app works fine without an active service worker,
      // it just won't get the offline shell / instant-repeat-load benefit.
    });
  });
}

// Surface the "Add to Home Screen" / install prompt as a button instead of
// letting the browser show its own mini-infobar, so it fits the app's UI.
// Chromium browsers fire this event when the install criteria are met;
// Safari/iOS never fires it (no beforeinstallprompt support there), so the
// button simply never appears on iOS -- users there install via the native
// Share -> Add to Home Screen action instead.
let deferredInstallPrompt = null;

window.addEventListener('beforeinstallprompt', (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  document.querySelectorAll('[data-pwa-install]').forEach(btn => btn.classList.remove('d-none'));
});

window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  document.querySelectorAll('[data-pwa-install]').forEach(btn => btn.classList.add('d-none'));
});

function initPwaInstallButtons() {
  document.querySelectorAll('[data-pwa-install]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!deferredInstallPrompt) return;
      deferredInstallPrompt.prompt();
      await deferredInstallPrompt.userChoice;
      deferredInstallPrompt = null;
    });
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initPwaInstallButtons);
} else {
  initPwaInstallButtons();
}

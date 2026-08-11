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

// Financial records must always come from the live server, so transactions
// are deliberately not queued or written while offline. Make that state
// obvious instead of letting a user discover it only after submitting a form.
function updateOfflineNotice() {
  let notice = document.getElementById('offlineNotice');
  if (!notice) {
    notice = document.createElement('div');
    notice.id = 'offlineNotice';
    notice.setAttribute('role', 'status');
    notice.style.cssText = [
      'display:none', 'position:fixed', 'top:0', 'left:0', 'right:0',
      'z-index:3000', 'padding:9px 16px', 'text-align:center',
      'font:600 13px/1.35 Arial,sans-serif', 'color:#fff',
      'background:#9c2f2f', 'box-shadow:0 2px 8px rgba(0,0,0,.2)'
    ].join(';');
    notice.textContent = 'You are offline. Saved pages and app assets remain available, but payments and updates require a connection.';
    document.body.prepend(notice);
  }
  notice.style.display = navigator.onLine ? 'none' : 'block';
}

window.addEventListener('online', updateOfflineNotice);
window.addEventListener('offline', updateOfflineNotice);
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', updateOfflineNotice);
} else {
  updateOfflineNotice();
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

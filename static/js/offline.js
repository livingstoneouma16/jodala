/* =========================================================
   Jodala Microfinance -- Offline repayments (cache + outbox)
   ---------------------------------------------------------
   Scope: browsing active loans/members/clients/recent repayments, and
   recording/voiding repayments, all with no connection. Everything else
   in the app still requires a live connection (see static/sw.js).

   Two IndexedDB object stores:
     - 'bundle'  : one row, the last-synced snapshot from
                   /repayments/api/offline-bundle (loans/members/clients/
                   recent repayments).
     - 'outbox'  : queued record/void actions, flushed to the server in
                   the order they were queued as soon as we're back online.
                   Each item carries a client-generated client_ref (UUID)
                   the server uses to dedupe retried/duplicate flushes and
                   to park anything that no longer applies cleanly (loan
                   closed since, etc) into a review queue instead of
                   silently dropping or double-applying it.

   This file loads on every page (via base.html) so the pending-sync badge
   and connectivity banner are consistent everywhere, but only the
   Repayments pages actually read/write the bundle and outbox contents.
   ========================================================= */
'use strict';

const Offline = (() => {
  const DB_NAME = 'jodala-offline';
  const DB_VERSION = 1;
  let dbPromise = null;

  function openDB() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('bundle')) db.createObjectStore('bundle');
        if (!db.objectStoreNames.contains('outbox')) {
          const store = db.createObjectStore('outbox', { keyPath: 'client_ref' });
          store.createIndex('status', 'status');
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
    return dbPromise;
  }

  async function tx(storeName, mode) {
    const db = await openDB();
    return db.transaction(storeName, mode).objectStore(storeName);
  }

  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0;
      return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }

  // ---- Bundle (cached read data) ------------------------------------
  async function saveBundle(data) {
    const store = await tx('bundle', 'readwrite');
    store.put(data, 'data');
  }

  async function getBundle() {
    const store = await tx('bundle', 'readonly');
    return new Promise((resolve) => {
      const req = store.get('data');
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => resolve(null);
    });
  }

  async function refreshBundle() {
    try {
      const data = await API.get('/repayments/api/offline-bundle');
      await saveBundle(data);
      return data;
    } catch (e) {
      return null; // offline or failed -- caller falls back to whatever's cached
    }
  }

  // ---- Outbox (queued writes) -----------------------------------------
  async function queueAction(actionType, payload) {
    const item = {
      client_ref: uuid(),
      action_type: actionType, // 'record_repayment' | 'void_repayment'
      payload,
      status: 'pending', // 'pending' | 'conflict'
      error: null,
      queued_at: new Date().toISOString(),
    };
    const store = await tx('outbox', 'readwrite');
    store.add(item);
    updateBadge();
    if ('serviceWorker' in navigator && 'SyncManager' in window) {
      try {
        const reg = await navigator.serviceWorker.ready;
        await reg.sync.register('jodala-outbox-sync');
      } catch (e) { /* best-effort; foreground flush covers this too */ }
    }
    return item;
  }

  async function listOutbox() {
    const store = await tx('outbox', 'readonly');
    return new Promise((resolve) => {
      const req = store.getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => resolve([]);
    });
  }

  async function removeAction(clientRef) {
    const store = await tx('outbox', 'readwrite');
    store.delete(clientRef);
  }

  async function markConflict(clientRef, message) {
    const store = await tx('outbox', 'readwrite');
    const req = store.get(clientRef);
    req.onsuccess = () => {
      const item = req.result;
      if (item) { item.status = 'conflict'; item.error = message; store.put(item); }
    };
  }

  async function pendingCount() {
    const items = await listOutbox();
    return items.filter(i => i.status === 'pending').length;
  }

  let flushing = false;
  async function flushOutbox() {
    if (flushing || !navigator.onLine) return;
    flushing = true;
    try {
      const items = (await listOutbox())
        .filter(i => i.status === 'pending')
        .sort((a, b) => a.queued_at.localeCompare(b.queued_at));

      for (const item of items) {
        const url = item.action_type === 'void_repayment'
          ? `/repayments/api/${item.payload.repayment_id}/void`
          : '/repayments/api';
        try {
          await API.post(url, { ...item.payload, client_ref: item.client_ref, queued_at: item.queued_at });
          await removeAction(item.client_ref);
        } catch (e) {
          if (e.isNetworkError) {
            // No connection -- stop here, keep everything queued, try again next flush.
            break;
          }
          // Server rejected it outright (validation error, or a 409 conflict
          // parked server-side for admin review) -- retrying the same
          // payload won't fix it, so stop auto-retrying this item and
          // surface it instead of looping forever.
          await markConflict(item.client_ref, e.message);
        }
      }
    } finally {
      flushing = false;
      updateBadge();
      window.dispatchEvent(new CustomEvent('offline:synced'));
    }
  }

  async function updateBadge() {
    const count = await pendingCount();
    document.querySelectorAll('[data-offline-badge]').forEach(el => {
      el.textContent = count;
      el.classList.toggle('d-none', count === 0);
    });
  }

  function updateConnectivityUI() {
    const online = navigator.onLine;
    document.querySelectorAll('[data-offline-indicator]').forEach(el => {
      el.classList.toggle('d-none', online);
    });
    if (online) flushOutbox();
  }

  window.addEventListener('online', updateConnectivityUI);
  window.addEventListener('offline', updateConnectivityUI);
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (event) => {
      if (event.data && event.data.type === 'jodala-flush-outbox') flushOutbox();
    });
  }
  document.addEventListener('DOMContentLoaded', () => {
    updateConnectivityUI();
    updateBadge();
    if (navigator.onLine) flushOutbox();
  });
  // Belt-and-braces periodic retry, since Background Sync isn't available
  // in every browser (notably Safari/iOS) -- catches "came back online
  // while the tab was already open and idle" too.
  setInterval(() => { if (navigator.onLine) flushOutbox(); }, 30000);

  return {
    getBundle, refreshBundle, queueAction, listOutbox, removeAction,
    pendingCount, flushOutbox, uuid,
    isOnline: () => navigator.onLine,
  };
})();

/* =========================================================
   Jodala Microfinance — app.js
   Core JS: auth headers, API helpers, sidebar, theme, toasts
   ========================================================= */

'use strict';

// ── Token Management ──────────────────────────────────────
const Auth = {
  getToken() {
    return document.cookie.split('; ')
      .find(r => r.startsWith('access_token='))
      ?.split('=')[1] || localStorage.getItem('jd_token');
  },
  setToken(token) { localStorage.setItem('jd_token', token); },
  clear() { localStorage.removeItem('jd_token'); }
};

// ── API Helper ────────────────────────────────────────────
const API = {
  async request(method, url, body = null, opts = {}) {
    const headers = { 'Content-Type': 'application/json' };
    const token = Auth.getToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;

    const config = { method, headers, ...opts };
    if (body) config.body = JSON.stringify(body);

    try {
      const res = await fetch(url, config);
      if (res.status === 401) {
        window.location.href = '/auth/login';
        return null;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || data.message || `HTTP ${res.status}`);
      return data;
    } catch (err) {
      if (err.message !== 'Failed to fetch') throw err;
      throw new Error('Network error — check connection');
    }
  },
  get(url)           { return this.request('GET', url); },
  post(url, body)    { return this.request('POST', url, body); },
  put(url, body)     { return this.request('PUT', url, body); },
  delete(url)        { return this.request('DELETE', url); },

  async download(url, filename) {
    const token = Auth.getToken();
    const headers = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(url, { headers });
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
  }
};

// ── Toast Notifications ───────────────────────────────────
const Toast = {
  show(message, type = 'info', duration = 4000) {
    const icons = { success: 'bi-check-circle-fill', error: 'bi-x-circle-fill',
                    warning: 'bi-exclamation-triangle-fill', info: 'bi-info-circle-fill' };
    const colors = { success: '#52B788', error: '#E63946', warning: '#F4A261', info: '#4895EF' };

    const toast = document.createElement('div');
    toast.className = `jd-toast ${type}`;
    toast.innerHTML = `
      <i class="bi ${icons[type]}" style="color:${colors[type]};font-size:18px;flex-shrink:0"></i>
      <span style="flex:1;font-size:13px">${message}</span>
      <button onclick="this.parentElement.remove()" style="background:none;border:none;font-size:16px;cursor:pointer;opacity:.5;padding:0;line-height:1">×</button>`;

    const container = document.getElementById('toastContainer');
    if (container) {
      container.appendChild(toast);
      setTimeout(() => toast.remove(), duration);
    }
  },
  success(msg, dur) { this.show(msg, 'success', dur); },
  error(msg, dur)   { this.show(msg, 'error', dur || 5000); },
  warning(msg, dur) { this.show(msg, 'warning', dur); },
  info(msg, dur)    { this.show(msg, 'info', dur); }
};

// ── Sidebar Toggle ────────────────────────────────────────
function initSidebar() {
  const sidebar  = document.getElementById('sidebar');
  const wrapper  = document.getElementById('mainWrapper');
  const toggle   = document.getElementById('sidebarToggle');
  const overlay  = document.getElementById('sidebarOverlay');
  const closeBtn = document.getElementById('sidebarClose');
  if (!sidebar || !toggle) return;

  const isMobile = () => window.innerWidth < 769;

  const collapsed = localStorage.getItem('jd_sidebar') === 'collapsed';
  if (collapsed && !isMobile()) { sidebar.classList.add('collapsed'); wrapper?.classList.add('expanded'); }

  function openMobile() {
    sidebar.classList.add('mobile-open');
    overlay?.classList.add('visible');
    document.body.classList.add('sidebar-locked');
  }

  function closeMobile() {
    sidebar.classList.remove('mobile-open');
    overlay?.classList.remove('visible');
    document.body.classList.remove('sidebar-locked');
  }

  toggle.addEventListener('click', () => {
    if (isMobile()) {
      sidebar.classList.contains('mobile-open') ? closeMobile() : openMobile();
    } else {
      sidebar.classList.toggle('collapsed');
      wrapper?.classList.toggle('expanded');
      localStorage.setItem('jd_sidebar', sidebar.classList.contains('collapsed') ? 'collapsed' : 'open');
    }
  });

  closeBtn?.addEventListener('click', closeMobile);
  overlay?.addEventListener('click', closeMobile);
  document.getElementById('tabbarMore')?.addEventListener('click', openMobile);

  // Close on outside click (mobile) — kept as a fallback alongside the overlay
  document.addEventListener('click', e => {
    if (isMobile() && sidebar.classList.contains('mobile-open')
        && !sidebar.contains(e.target) && e.target !== toggle) {
      closeMobile();
    }
  });

  // Close the drawer automatically when a nav link is tapped
  sidebar.querySelectorAll('.nav-link-item').forEach(link => {
    link.addEventListener('click', () => { if (isMobile()) closeMobile(); });
  });

  // Close on Escape, and reset drawer state when resizing past the mobile breakpoint
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && sidebar.classList.contains('mobile-open')) closeMobile();
  });
  window.addEventListener('resize', () => {
    if (isMobile()) {
      sidebar.classList.remove('collapsed');
      wrapper?.classList.remove('expanded');
    } else {
      if (sidebar.classList.contains('mobile-open')) closeMobile();
      if (localStorage.getItem('jd_sidebar') === 'collapsed') {
        sidebar.classList.add('collapsed');
        wrapper?.classList.add('expanded');
      }
    }
  });
}

// ── Theme Toggle ──────────────────────────────────────────
function initTheme() {
  const btn  = document.getElementById('themeToggle');
  const icon = document.getElementById('themeIcon');
  if (!btn) return;

  const current = localStorage.getItem('jd_theme') || 'light';
  document.documentElement.setAttribute('data-bs-theme', current);
  icon.className = current === 'dark' ? 'bi bi-sun-fill' : 'bi bi-moon-fill';

  btn.addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-bs-theme', next);
    icon.className = next === 'dark' ? 'bi bi-sun-fill' : 'bi bi-moon-fill';
    localStorage.setItem('jd_theme', next);
  });
}

// ── Global Quick Search ────────────────────────────────────
// The navbar search box (#globalSearch) only renders a <input> in
// base.html -- nothing was ever wired up to it. This hits
// /dashboard/search as the person types and shows a categorized
// (Members / Clients / Loans) results dropdown they can click or
// arrow-key + Enter into.
function initGlobalSearch() {
  const input = document.getElementById('globalSearch');
  const panel = document.getElementById('globalSearchResults');
  if (!input || !panel) return;

  let activeIndex = -1;
  let requestId = 0;

  function close() {
    panel.classList.remove('visible');
    panel.innerHTML = '';
    activeIndex = -1;
  }

  function setActive(items, index) {
    activeIndex = index;
    items.forEach((el, i) => el.classList.toggle('active', i === activeIndex));
    if (items[activeIndex]) items[activeIndex].scrollIntoView({ block: 'nearest' });
  }

  function renderGroup(label, iconClass, items, renderSub) {
    if (!items.length) return '';
    const rows = items.map(item => `
      <a class="gsr-item" href="${item.url}">
        <i class="bi ${iconClass}"></i>
        <div class="gsr-item-text">
          <div class="gsr-item-title">${escapeHtml(item._title)}</div>
          <div class="gsr-item-sub">${renderSub(item)}</div>
        </div>
        ${statusPill(item.status)}
      </a>`).join('');
    return `<div class="gsr-section-label">${label}</div>${rows}`;
  }

  function render(data) {
    const members = data.members || [];
    const clients = data.clients || [];
    const loans = data.loans || [];
    members.forEach(m => { m._title = m.full_name; });
    clients.forEach(c => { c._title = c.full_name; });
    loans.forEach(l => { l._title = l.loan_number; });

    if (!members.length && !clients.length && !loans.length) {
      panel.innerHTML = '<div class="gsr-empty">No matches found</div>';
      panel.classList.add('visible');
      return;
    }

    panel.innerHTML =
      renderGroup('Members', 'bi-person-fill', members,
        m => `${escapeHtml(m.member_number)} · ${escapeHtml(m.phone || '—')}`) +
      renderGroup('Clients', 'bi-building', clients,
        c => `${escapeHtml(c.client_number)} · ${escapeHtml(c.phone || '—')}`) +
      renderGroup('Loans', 'bi-cash-coin', loans,
        l => `${escapeHtml(l.borrower_name)} · ${fmtCurrency(l.outstanding_balance)} outstanding`);
    panel.classList.add('visible');
    activeIndex = -1;
  }

  const runSearch = debounce(async (q) => {
    const myRequestId = ++requestId;
    try {
      const data = await API.get(`/dashboard/search?q=${encodeURIComponent(q)}`);
      if (myRequestId !== requestId) return; // a newer keystroke already superseded this
      if (data) render(data);
    } catch (_) {
      if (myRequestId === requestId) {
        panel.innerHTML = '<div class="gsr-error">Search failed — try again</div>';
        panel.classList.add('visible');
      }
    }
  }, 300);

  input.addEventListener('input', () => {
    const q = input.value.trim();
    if (q.length < 2) { requestId++; close(); return; }
    panel.innerHTML = '<div class="gsr-loading">Searching…</div>';
    panel.classList.add('visible');
    runSearch(q);
  });

  input.addEventListener('focus', () => {
    if (panel.innerHTML && input.value.trim().length >= 2) panel.classList.add('visible');
  });

  input.addEventListener('keydown', e => {
    const items = Array.from(panel.querySelectorAll('.gsr-item'));
    if (e.key === 'Escape') {
      close();
      input.blur();
    } else if (e.key === 'ArrowDown' && items.length) {
      e.preventDefault();
      setActive(items, Math.min(activeIndex + 1, items.length - 1));
    } else if (e.key === 'ArrowUp' && items.length) {
      e.preventDefault();
      setActive(items, Math.max(activeIndex - 1, 0));
    } else if (e.key === 'Enter' && activeIndex >= 0 && items[activeIndex]) {
      e.preventDefault();
      window.location.href = items[activeIndex].getAttribute('href');
    }
  });

  document.addEventListener('click', e => {
    if (!panel.contains(e.target) && e.target !== input) close();
  });
}

// ── Notifications ─────────────────────────────────────────
async function loadNotifications() {
  try {
    const data = await API.get('/dashboard/notifications');
    if (!data) return;

    const dot  = document.getElementById('notifDot');
    const badge = document.getElementById('notifBadge');
    const list  = document.getElementById('notifList');

    if (data.length > 0) {
      if (dot)   { dot.style.display = 'block'; }
      if (badge) { badge.textContent = data.length; badge.style.display = 'inline'; }
    }

    if (list) {
      if (data.length === 0) {
        list.innerHTML = '<div class="text-center p-3 text-muted small"><i class="bi bi-bell-slash d-block mb-1" style="font-size:24px;opacity:.3"></i>No new notifications</div>';
      } else {
        const typeIcon = { info: 'bi-info-circle', warning: 'bi-exclamation-triangle',
                           alert: 'bi-bell', success: 'bi-check-circle' };
        const typeColor = { info: '#4895EF', warning: '#F4A261', alert: '#E63946', success: '#52B788' };
        list.innerHTML = data.map(n => `
          <div class="d-flex gap-2 p-3 border-bottom" style="font-size:12px">
            <i class="bi ${typeIcon[n.notification_type] || 'bi-bell'}" style="color:${typeColor[n.notification_type] || '#6B8070'};font-size:16px;flex-shrink:0;margin-top:1px"></i>
            <div>
              <div style="font-weight:600">${n.title}</div>
              <div style="color:var(--text-muted)">${n.message}</div>
              <div style="color:var(--text-muted);margin-top:2px">${timeAgo(n.created_at)}</div>
            </div>
          </div>`).join('');
      }
    }
  } catch (_) {}
}

document.getElementById('markAllRead')?.addEventListener('click', async () => {
  try {
    await API.post('/dashboard/notifications/mark-all-read', {});
    document.getElementById('notifDot').style.display = 'none';
    document.getElementById('notifBadge').style.display = 'none';
    document.getElementById('notifList').innerHTML =
      '<div class="text-center p-3 text-muted small">All caught up!</div>';
  } catch (_) {}
});

// ── Utility Functions ─────────────────────────────────────
// Escape untrusted strings before interpolating them into innerHTML.
// Always use this for any member/loan/client/user supplied text (names,
// notes, descriptions, etc.) that gets built into HTML via template
// literals. Never insert unescaped user data into innerHTML.
const ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '/': '&#x2F;' };
function escapeHtml(value) {
  if (value === null || value === undefined) return '';
  return String(value).replace(/[&<>"'/]/g, ch => ESCAPE_MAP[ch]);
}

// Safely embed an untrusted string as a single-quoted JS string literal
// argument inside an inline HTML event handler attribute, e.g.
//   onclick="doThing('${jsAttr(m.full_name)}')"
// Inline event handler attributes are parsed TWICE by the browser: once as
// HTML (which decodes entities) and then the decoded result is parsed as
// JS. Escaping with escapeHtml() alone is NOT sufficient here, because
// entity-decoding happens before the JS parser runs, so a raw quote
// smuggled in via an HTML entity is decoded back into a real quote before
// JS sees it and can still break out of the string. jsAttr() first escapes
// the value so it is safe as JS string *content*, then HTML-escapes that
// result so it is also safe as the surrounding HTML attribute value.
function jsAttr(value) {
  const jsEscaped = String(value === null || value === undefined ? '' : value)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r')
    .replace(/</g, '\\x3C')
    .replace(/>/g, '\\x3E');
  return escapeHtml(jsEscaped);
}

function timeAgo(iso) {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1)  return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function fmt(n, decimals = 2) {
  if (n == null) return '—';
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtCurrency(n) {
  if (n == null) return '—';
  const sym = window.CURRENCY_SYM || 'Ksh ';
  return sym + fmt(n);
}

function fmtDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
}

function statusPill(status) {
  return `<span class="status-pill status-${escapeHtml(status)}">${escapeHtml(String(status).replace('_', ' '))}</span>`;
}

function setLoading(el, loading) {
  if (!el) return;
  if (loading) {
    el.disabled = true;
    el._origHTML = el.innerHTML;
    el.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Loading…';
  } else {
    el.disabled = false;
    if (el._origHTML) el.innerHTML = el._origHTML;
  }
}

// ── Confirm Dialog ────────────────────────────────────────
function confirm(message, onConfirm, type = 'danger') {
  const id = 'confirmModal';
  let modal = document.getElementById(id);
  if (!modal) {
    modal = document.createElement('div');
    modal.id = id;
    modal.className = 'modal fade';
    modal.setAttribute('tabindex', '-1');
    modal.innerHTML = `
      <div class="modal-dialog modal-sm modal-dialog-centered">
        <div class="modal-content">
          <div class="modal-body p-4 text-center">
            <i class="bi bi-exclamation-triangle-fill text-warning" style="font-size:36px"></i>
            <p class="mt-3 mb-0" id="confirmMsg"></p>
          </div>
          <div class="modal-footer justify-content-center border-0 pt-0">
            <button class="btn btn-sm btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button class="btn btn-sm" id="confirmOk"></button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(modal);
  }
  document.getElementById('confirmMsg').textContent = message;
  const okBtn = document.getElementById('confirmOk');
  okBtn.className = `btn btn-sm btn-${type}`;
  okBtn.textContent = 'Confirm';
  const bsModal = new bootstrap.Modal(modal);
  okBtn.onclick = () => { bsModal.hide(); onConfirm(); };
  bsModal.show();
}

// ── Debounce ──────────────────────────────────────────────
function debounce(fn, ms = 300) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// ── Pagination Helper ─────────────────────────────────────
function renderPagination(containerId, currentPage, totalPages, onPage) {
  const c = document.getElementById(containerId);
  if (!c) return;
  c.innerHTML = '';

  const makeBtn = (label, page, { disabled = false, active = false, isIcon = false } = {}) => {
    const btn = document.createElement('button');
    btn.className = `page-btn${active ? ' active' : ''}`;
    if (isIcon) {
      btn.innerHTML = label;
    } else {
      btn.textContent = label;
    }
    if (disabled) {
      btn.disabled = true;
    } else {
      btn.addEventListener('click', () => onPage(page));
    }
    return btn;
  };

  c.appendChild(makeBtn('<i class="bi bi-chevron-left"></i>', currentPage - 1, { disabled: currentPage === 1, isIcon: true }));

  const range = [];
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || (i >= currentPage - 1 && i <= currentPage + 1)) range.push(i);
    else if (range[range.length-1] !== '…') range.push('…');
  }

  range.forEach(p => {
    if (p === '…') {
      const span = document.createElement('span');
      span.className = 'page-btn';
      span.style.pointerEvents = 'none';
      span.textContent = '…';
      c.appendChild(span);
    } else {
      c.appendChild(makeBtn(String(p), p, { active: p === currentPage }));
    }
  });

  c.appendChild(makeBtn('<i class="bi bi-chevron-right"></i>', currentPage + 1, { disabled: currentPage === totalPages, isIcon: true }));
}

// ── Number Input Formatter ────────────────────────────────
document.addEventListener('input', e => {
  if (e.target.matches('[data-fmt="currency"]')) {
    let v = e.target.value.replace(/[^0-9.]/g, '');
    const parts = v.split('.');
    if (parts.length > 2) v = parts[0] + '.' + parts.slice(1).join('');
    e.target.value = v;
  }
});

// ── Idle Timeout Auto-Logout ──────────────────────────────
// Reads the admin-configured timeout (Settings > Notifications) off
// <body data-idle-timeout-minutes>, set by templates/base.html from
// core/database.py:get_company_branding(). A value of 0 disables it.
function initIdleTimer() {
  const minutes = parseInt(document.body.dataset.idleTimeoutMinutes || '0', 10);
  if (!minutes || minutes <= 0) return;

  const IDLE_MS = minutes * 60 * 1000;
  const WARNING_MS = Math.min(60000, Math.floor(IDLE_MS / 4)); // warn up to 60s before logout
  let idleTimer, warningTimer, countdownInterval;
  let warningModalEl, warningModal, countdownEl;

  function ensureWarningModal() {
    if (warningModalEl) return;
    warningModalEl = document.createElement('div');
    warningModalEl.className = 'modal fade';
    warningModalEl.id = 'idleWarningModal';
    warningModalEl.tabIndex = -1;
    warningModalEl.setAttribute('data-bs-backdrop', 'static');
    warningModalEl.innerHTML = `
      <div class="modal-dialog modal-dialog-centered modal-sm">
        <div class="modal-content">
          <div class="modal-body text-center p-4">
            <i class="bi bi-clock-history" style="font-size:28px;color:var(--brand-light)"></i>
            <p class="mt-2 mb-1" style="font-size:13px;font-weight:600">You've been inactive</p>
            <p class="text-muted mb-3" style="font-size:12px">
              You'll be signed out in <span id="idleCountdown">60</span>s for your security.
            </p>
            <button class="btn btn-brand btn-sm w-100" id="stayLoggedInBtn">
              <i class="bi bi-check2 me-1"></i>Stay Signed In
            </button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(warningModalEl);
    countdownEl = warningModalEl.querySelector('#idleCountdown');
    warningModal = new bootstrap.Modal(warningModalEl);
    warningModalEl.querySelector('#stayLoggedInBtn').addEventListener('click', () => {
      warningModal.hide();
      resetIdleTimer();
    });
  }

  function doLogout() {
    window.location.href = '/auth/logout';
  }

  function showWarning() {
    ensureWarningModal();
    let secondsLeft = Math.floor(WARNING_MS / 1000);
    countdownEl.textContent = secondsLeft;
    warningModal.show();
    countdownInterval = setInterval(() => {
      secondsLeft -= 1;
      if (countdownEl) countdownEl.textContent = Math.max(secondsLeft, 0);
      if (secondsLeft <= 0) {
        clearInterval(countdownInterval);
        doLogout();
      }
    }, 1000);
  }

  function clearAllTimers() {
    clearTimeout(idleTimer);
    clearTimeout(warningTimer);
    clearInterval(countdownInterval);
  }

  function resetIdleTimer() {
    clearAllTimers();
    if (warningModal) warningModal.hide();
    warningTimer = setTimeout(showWarning, IDLE_MS - WARNING_MS);
    idleTimer = setTimeout(doLogout, IDLE_MS);
  }

  // Any of these count as "active" -- reset the clock, throttled so a
  // continuous mousemove/scroll doesn't fire this on every event.
  const resetThrottled = debounce(resetIdleTimer, 1000);
  ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart', 'click'].forEach(evt => {
    document.addEventListener(evt, resetThrottled, { passive: true });
  });

  resetIdleTimer();
}

// ── Init ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initSidebar();
  initTheme();
  initGlobalSearch();
  loadNotifications();
  initIdleTimer();

  // Refresh notifications every 2 minutes
  setInterval(loadNotifications, 120000);

  // Tooltips
  document.querySelectorAll('[data-bs-toggle="tooltip"]')
    .forEach(el => new bootstrap.Tooltip(el));
});

// ── Global exports ────────────────────────────────────────
window.API = API;
window.Toast = Toast;
window.Auth = Auth;
window.fmt = fmt;
window.fmtCurrency = fmtCurrency;
window.fmtDate = fmtDate;
window.statusPill = statusPill;
window.setLoading = setLoading;
window.confirm = confirm;
window.debounce = debounce;
window.renderPagination = renderPagination;
window.timeAgo = timeAgo;

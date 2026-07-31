/* =========================================================
   Jodala Microfinance — webauthn.js
   Fingerprint / passkey login (WebAuthn). Talks to the browser's own
   navigator.credentials API (which in turn talks to the device's
   fingerprint sensor / Face ID / Windows Hello) and to the /auth/webauthn/*
   endpoints. Depends on nothing else except `fetch` — safe to include on
   the login page, which loads before app.js's Auth/API helpers exist.
   ========================================================= */

'use strict';

const WebAuthnClient = {
  /** True if this browser can do WebAuthn at all. Doesn't guarantee a
   *  fingerprint sensor specifically is present -- that's only knowable by
   *  actually attempting a ceremony, since browsers don't expose hardware
   *  details up front for privacy reasons. */
  isSupported() {
    return typeof window.PublicKeyCredential !== 'undefined'
      && typeof navigator.credentials !== 'undefined';
  },

  /** Best-effort hint for whether a platform authenticator (fingerprint/
   *  Face ID/Windows Hello built into this device, as opposed to a separate
   *  USB security key) is available, to decide whether to even show a
   *  "Sign in with fingerprint" button. Not supported in every browser --
   *  callers should treat `null`/rejection as "unknown, show the button
   *  anyway and let the ceremony itself fail gracefully if unsupported." */
  async platformAuthenticatorAvailable() {
    if (!this.isSupported()) return false;
    try {
      return await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
    } catch {
      return null;
    }
  },

  // -- base64url <-> ArrayBuffer, since WebAuthn's browser API speaks
  //    ArrayBuffers but JSON (and our server) only speaks base64url strings.
  _b64uToBuf(b64u) {
    const pad = '='.repeat((4 - (b64u.length % 4)) % 4);
    const b64 = (b64u + pad).replace(/-/g, '+').replace(/_/g, '/');
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  },
  _bufToB64u(buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },

  async _postJson(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  },

  /** Registers the current device's fingerprint sensor for the logged-in
   *  user. Call this from Settings/Profile, behind a normal authenticated
   *  request (browser cookie already carries the session). Returns once the
   *  server has stored the new credential. Throws on any failure, including
   *  the user cancelling the OS fingerprint prompt. */
  async registerDevice(deviceName) {
    if (!this.isSupported()) {
      throw new Error('This browser does not support fingerprint/passkey login.');
    }

    const optionsJson = await this._postJson('/auth/webauthn/register/options', {});
    const options = JSON.parse(typeof optionsJson === 'string' ? optionsJson : JSON.stringify(optionsJson));

    const publicKey = {
      ...options,
      challenge: this._b64uToBuf(options.challenge),
      user: { ...options.user, id: this._b64uToBuf(options.user.id) },
      excludeCredentials: (options.excludeCredentials || []).map(c => ({
        ...c, id: this._b64uToBuf(c.id),
      })),
    };

    let credential;
    try {
      credential = await navigator.credentials.create({ publicKey });
    } catch (err) {
      throw new Error(err.name === 'NotAllowedError'
        ? 'Cancelled, or your device has no fingerprint sensor set up.'
        : 'Could not read your fingerprint. Please try again.');
    }
    if (!credential) throw new Error('No credential was created.');

    const credentialJson = {
      id: credential.id,
      rawId: this._bufToB64u(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: this._bufToB64u(credential.response.clientDataJSON),
        attestationObject: this._bufToB64u(credential.response.attestationObject),
        transports: credential.response.getTransports ? credential.response.getTransports() : [],
      },
      clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {},
    };

    return this._postJson('/auth/webauthn/register/verify', {
      credential: credentialJson,
      device_name: deviceName || null,
    });
  },

  /** Prompts the device's fingerprint sensor and, on success, logs the
   *  matching user in (server sets the same auth cookie password login
   *  would). `username` is optional -- pass it if the user already typed one
   *  on the login form, so the OS only offers relevant devices. */
  async login(username, nextPath) {
    if (!this.isSupported()) {
      throw new Error('This browser does not support fingerprint/passkey login.');
    }

    const optionsJson = await this._postJson('/auth/webauthn/login/options', { username: username || null });
    const options = JSON.parse(typeof optionsJson === 'string' ? optionsJson : JSON.stringify(optionsJson));

    const publicKey = {
      ...options,
      challenge: this._b64uToBuf(options.challenge),
      allowCredentials: (options.allowCredentials || []).map(c => ({
        ...c, id: this._b64uToBuf(c.id),
      })),
    };

    let assertion;
    try {
      assertion = await navigator.credentials.get({ publicKey });
    } catch (err) {
      throw new Error(err.name === 'NotAllowedError'
        ? 'Cancelled, or no matching fingerprint was recognized.'
        : 'Could not read your fingerprint. Please try again.');
    }
    if (!assertion) throw new Error('No credential was returned.');

    const credentialJson = {
      id: assertion.id,
      rawId: this._bufToB64u(assertion.rawId),
      type: assertion.type,
      response: {
        clientDataJSON: this._bufToB64u(assertion.response.clientDataJSON),
        authenticatorData: this._bufToB64u(assertion.response.authenticatorData),
        signature: this._bufToB64u(assertion.response.signature),
        userHandle: assertion.response.userHandle ? this._bufToB64u(assertion.response.userHandle) : null,
      },
      clientExtensionResults: assertion.getClientExtensionResults ? assertion.getClientExtensionResults() : {},
    };

    return this._postJson('/auth/webauthn/login/verify', {
      credential: credentialJson,
      next: nextPath || null,
    });
  },
};

window.WebAuthnClient = WebAuthnClient;

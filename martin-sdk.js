/*!
 * Martin CDP SDK — lightweight first-party event collection.
 * Usage:
 *   <script src="/martin-sdk.js" data-endpoint="https://app.smartwithmartin.ai"></script>
 *   martin.identify('user-123', { plan: 'pro', consent_marketing: true, consent_analytics: true });
 *   martin.track('Appointment Booked', { revenue: 0, consent_analytics: true });
 *
 * First-party by design: events post server-side to Martin, which applies the PHI
 * guard + consent gate before anything is stored. Never send raw PHI in properties.
 */
(function (w, d) {
  var cfg = (d.currentScript && d.currentScript.dataset) || {};
  var BASE = (cfg.endpoint || w.MARTIN_ENDPOINT || '').replace(/\/$/, '');
  var LS_ANON = 'martin_anon_id';

  function uuid() {
    return 'a-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }
  function anonId() {
    try {
      var v = w.localStorage.getItem(LS_ANON);
      if (!v) { v = uuid(); w.localStorage.setItem(LS_ANON, v); }
      return v;
    } catch (e) { return uuid(); }
  }
  function post(path, body) {
    body.anonymousId = body.anonymousId || anonId();
    body.time = body.time || (Date.now() / 1000);
    var url = BASE + '/api/cdp/' + path;
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(url, new Blob([JSON.stringify(body)], { type: 'application/json' }));
        return Promise.resolve();
      }
    } catch (e) {}
    return fetch(url, {
      method: 'POST', keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).catch(function () {});
  }

  var martin = {
    _externalId: null,
    identify: function (externalId, traits) {
      this._externalId = externalId || this._externalId;
      return post('identify', { type: 'identify', externalId: this._externalId, traits: traits || {} });
    },
    track: function (event, properties) {
      return post('track', { event: event, externalId: this._externalId, properties: properties || {} });
    },
    page: function (name, properties) {
      var p = properties || {}; p.page = name || (d.title);
      return this.track('Page Viewed', p);
    },
    reset: function () { this._externalId = null; try { w.localStorage.removeItem(LS_ANON); } catch (e) {} }
  };

  // auto page view (opt out with data-autopage="off")
  if (cfg.autopage !== 'off') {
    if (d.readyState === 'complete') martin.page();
    else w.addEventListener('load', function () { martin.page(); });
  }
  w.martin = martin;
})(window, document);

/*! MarkHub tag for www.solix.com: product-interest tracking and form linking, under the site's CookieYes consent.
 *
 * Loaded by one Google Tag Manager "Custom HTML" tag on All Pages:
 *   <script src="https://<markhub-api-host>/api/markhub.js" async></script>
 *
 * Consent: follows the CookieYes banner already on solix.com. Nothing is tracked and no identifier is stored until
 * the visitor accepts the "Analytics" category; withdrawing it later stops tracking and removes the identifier.
 * MarkHub shows no banner of its own (markhubConfig.ownBanner = true turns one on, for a site without a consent tool).
 *
 * After consent: an anonymous visitor id, and page views, engaged reading (45s active), deep scroll (75%),
 * demo/contact/trial clicks, resource views, PDF downloads (by file) and lead-form views, batched to /api/track.
 * Adds a hidden "markhub_visitor_id" field to every form, so the WordPress plugin can link the submission to the
 * visitor's browsing. ?solix_internal=1 marks this browser as Solix staff (kept out of every metric).
 *
 * Optional settings, before this script loads: window.markhubConfig = { api: "https://...", ownBanner: false }
 */
(function () {
  "use strict";
  if (window.markhub && window.markhub.loaded) return;

  var cfg = window.markhubConfig || {};
  var script = document.currentScript || document.querySelector('script[src*="/api/markhub.js"]');
  var API = (cfg.api || (script && script.src ? script.src.replace(/\/api\/markhub\.js.*$/, "") : "")).replace(/\/$/, "");
  var PRIVACY = cfg.privacyUrl || "/privacy-policy/";
  var CONSENT_KEY = "markhub-consent"; // "all" | "essential"
  var VID_KEY = "markhub_vid";
  var SESSION_KEY = "markhub_session";
  var TOUCH_KEY = "markhub_touch";
  var INTERNAL_KEY = "markhub_internal";
  var SESSION_IDLE_MS = 30 * 60 * 1000;
  var FLUSH_MS = 4000;

  // --- storage that never throws (private windows, blocked storage) ---------------------------------------------
  function get(area, k) { try { return window[area].getItem(k); } catch (e) { return null; } }
  function set(area, k, v) { try { window[area].setItem(k, v); } catch (e) { /* ignore */ } }
  function del(area, k) { try { window[area].removeItem(k); } catch (e) { /* ignore */ } }

  function uid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  // --- consent --------------------------------------------------------------------------------------------------
  // www.solix.com already asks through its CookieYes banner: MarkHub follows that choice (the "Analytics" category)
  // and shows nothing of its own. Its own banner below is only for a site without a consent tool, and only when
  // markhubConfig.ownBanner is true.
  var gpc = navigator.globalPrivacyControl === true;
  var cmp = null; // "cookieyes" once detected

  function cookieYes() {
    try {
      if (typeof window.getCkyConsent === "function") {
        var c = window.getCkyConsent();
        if (c && c.categories) return { done: !!c.isUserActionCompleted, analytics: c.categories.analytics === true };
      }
    } catch (e) { /* fall through to the cookie */ }
    var m = document.cookie.match(/(?:^|;\s*)cookieyes-consent=([^;]*)/);
    if (!m) return null;
    var v = decodeURIComponent(m[1]);
    return { done: /action:yes/.test(v), analytics: /analytics:yes/.test(v) };
  }

  function consent() {
    if (cmp === "cookieyes") {
      var c = cookieYes();
      if (!c) return null;
      return c.analytics ? "all" : c.done ? "essential" : null;
    }
    return get("localStorage", CONSENT_KEY) || (gpc ? "essential" : null);
  }
  function granted() { return consent() === "all"; }

  function announce(value) {
    if (cmp) return; // the consent tool updates Google Consent Mode and GTM itself
    var analytics = value === "all" ? "granted" : "denied";
    window.dataLayer = window.dataLayer || [];
    window.dataLayer.push({ event: "markhub_consent", analytics: analytics });
    if (typeof window.gtag === "function") {
      window.gtag("consent", "update", { analytics_storage: analytics, ad_storage: analytics, ad_user_data: analytics, ad_personalization: analytics });
    }
  }

  function choose(value) {
    set("localStorage", CONSENT_KEY, value);
    if (value !== "all") {
      del("localStorage", VID_KEY);
      del("sessionStorage", SESSION_KEY);
      queue = [];
    }
    hideBanner();
    announce(value);
    if (value === "all") start();
  }

  var banner;
  function hideBanner() { if (banner && banner.parentNode) banner.parentNode.removeChild(banner); banner = null; }
  function showBanner() {
    if (banner || !document.body) return;
    banner = document.createElement("div");
    banner.setAttribute("role", "dialog");
    banner.setAttribute("aria-live", "polite");
    banner.setAttribute("aria-label", "Cookie preferences");
    banner.id = "markhub-consent";
    banner.innerHTML =
      '<style>#markhub-consent{position:fixed;left:16px;right:16px;bottom:16px;z-index:2147483000;max-width:720px;margin:0 auto;' +
      "background:#0b1f3a;color:#fff;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.25);padding:16px 18px;font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;" +
      "display:flex;flex-wrap:wrap;gap:12px;align-items:center}" +
      "#markhub-consent p{margin:0;flex:1 1 320px}#markhub-consent a{color:#9cc7ff;text-decoration:underline}" +
      "#markhub-consent .mh-b{display:flex;gap:8px;flex-wrap:wrap}" +
      "#markhub-consent button{font:inherit;cursor:pointer;border-radius:6px;padding:8px 14px;border:1px solid #9cc7ff}" +
      "#markhub-consent .mh-a{background:#fff;color:#0b1f3a;border-color:#fff;font-weight:600}" +
      "#markhub-consent .mh-e{background:transparent;color:#fff}</style>" +
      "<p>We use cookies to understand which solutions interest you and to improve our site. " +
      'Essential cookies are always on. <a href="' + PRIVACY + '">Privacy policy</a></p>' +
      '<div class="mh-b"><button type="button" class="mh-e">Essential only</button>' +
      '<button type="button" class="mh-a">Accept analytics</button></div>';
    banner.querySelector(".mh-a").addEventListener("click", function () { choose("all"); });
    banner.querySelector(".mh-e").addEventListener("click", function () { choose("essential"); });
    document.body.appendChild(banner);
  }

  // --- identity, session, context --------------------------------------------------------------------------------
  function visitorId() {
    if (!granted()) return null;
    var id = get("localStorage", VID_KEY);
    if (!id) { id = uid(); set("localStorage", VID_KEY, id); }
    return id;
  }

  function sessionId() {
    var s = null, now = Date.now();
    try { s = JSON.parse(get("sessionStorage", SESSION_KEY) || "null"); } catch (e) { s = null; }
    if (!s || now - s.last > SESSION_IDLE_MS) s = { id: uid(), last: now, fresh: true };
    else s.fresh = false;
    s.last = now;
    set("sessionStorage", SESSION_KEY, JSON.stringify({ id: s.id, last: s.last }));
    return s;
  }

  // Where the visitor came from: read on the landing page, kept for the session.
  var touch = (function () {
    var t = null;
    try { t = JSON.parse(get("sessionStorage", TOUCH_KEY) || "null"); } catch (e) { t = null; }
    if (t) return t;
    t = { landing: location.pathname, referrer: document.referrer && document.referrer.indexOf(location.host) === -1 ? document.referrer.slice(0, 300) : "" };
    var p = new URLSearchParams(location.search);
    ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"].forEach(function (k) { if (p.get(k)) t[k] = p.get(k).slice(0, 120); });
    set("sessionStorage", TOUCH_KEY, JSON.stringify(t));
    return t;
  })();

  // Staff: ?solix_internal=1 marks this browser as Solix's own (kept out of every MarkHub metric), ?solix_internal=0
  // clears it. Signing in to MarkHub can't mark it here: www.solix.com has its own browser storage.
  var internalFlag = (location.search.match(/[?&]solix_internal=([01])\b/) || [])[1];
  if (internalFlag === "1") set("localStorage", INTERNAL_KEY, "1");
  else if (internalFlag === "0") del("localStorage", INTERNAL_KEY);

  function context() {
    var c = {};
    for (var k in touch) if (touch[k]) c[k] = touch[k];
    c.lang = (document.documentElement.lang || "en").slice(0, 8);
    c.site = location.host;
    if (get("localStorage", INTERNAL_KEY) === "1") c.internal = 1;
    return c;
  }

  // --- event queue -----------------------------------------------------------------------------------------------
  var queue = [], timer = null, session = null;

  function send(body, beacon) {
    if (!API) return;
    var url = API + "/api/track", data = JSON.stringify(body);
    if (beacon && navigator.sendBeacon && navigator.sendBeacon(url, new Blob([data], { type: "text/plain" }))) return;
    try { fetch(url, { method: "POST", body: data, headers: { "Content-Type": "text/plain" }, keepalive: true, mode: "cors", credentials: "omit" }); } catch (e) { /* ignore */ }
  }

  function flush(beacon) {
    if (timer) { clearTimeout(timer); timer = null; }
    var vid = visitorId();
    if (!vid || !queue.length) { queue = []; return; }
    var batch = queue.splice(0, 20);
    send({ visitor_id: vid, session_id: (session || sessionId()).id, context: context(), events: batch }, beacon);
    if (queue.length) flush(beacon);
  }

  function track(type, meta, path) {
    if (!granted()) return;
    var ev = { type: type, path: path || location.pathname };
    if (meta) ev.meta = meta;
    queue.push(ev);
    if (!timer) timer = setTimeout(function () { timer = null; flush(true); }, FLUSH_MS);
    if (queue.length >= 20) flush(true);
  }

  // --- what gets tracked -----------------------------------------------------------------------------------------
  var CTA_RX = /\/(products\/request-demo|company\/contact-sales|company\/contact-us)\b|free-trial|request-demo|contact-sales/i;
  var LEAD_FORM_RX = /demo_email|cts_email|new_email|email/;
  var started = false;

  function stampForms() {
    var vid = visitorId();
    if (!vid) return;
    var forms = document.getElementsByTagName("form");
    for (var i = 0; i < forms.length; i++) {
      var f = forms[i], input = f.querySelector('input[name="markhub_visitor_id"]');
      if (!input) {
        input = document.createElement("input");
        input.type = "hidden";
        input.name = "markhub_visitor_id";
        f.appendChild(input);
      }
      input.value = vid;
    }
  }

  function watchForms() {
    var seen = [];
    var forms = document.getElementsByTagName("form");
    for (var i = 0; i < forms.length; i++) {
      var f = forms[i];
      var names = Array.prototype.map.call(f.querySelectorAll("input[name]"), function (x) { return x.name; }).join(" ");
      if (!LEAD_FORM_RX.test(names) || /search/i.test(f.className + " " + (f.id || ""))) continue;
      seen.push(f);
    }
    if (!seen.length) return;
    if (!("IntersectionObserver" in window)) { track("form_view", { form: seen[0].id || seen[0].name || "form" }); return; }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { track("form_view", { form: e.target.id || e.target.getAttribute("name") || "form" }); io.unobserve(e.target); }
      });
    }, { threshold: 0.4 });
    seen.forEach(function (f) { io.observe(f); });
  }

  function watchReading() {
    var active = 0, last = Date.now(), engagedSent = false, scrollSent = false;
    function bump() { last = Date.now(); }
    ["mousemove", "keydown", "scroll", "touchstart"].forEach(function (e) { window.addEventListener(e, bump, { passive: true }); });
    var tick = setInterval(function () {
      if (document.visibilityState === "visible" && Date.now() - last < 15000) active += 5;
      if (!engagedSent && active >= 45) { engagedSent = true; track("engaged"); }
      if (engagedSent && scrollSent) clearInterval(tick);
    }, 5000);
    window.addEventListener("scroll", function () {
      if (scrollSent) return;
      var h = document.documentElement, depth = (h.scrollTop + window.innerHeight) / Math.max(h.scrollHeight, 1);
      if (depth >= 0.75) { scrollSent = true; track("deep_scroll"); }
    }, { passive: true });
  }

  var wired = false;

  function start() {
    if (started || !granted()) return;
    started = true;
    session = sessionId();
    if (session.fresh) track("session_start");
    track(/^\/resources\//.test(location.pathname) ? "resource_view" : "page_view", { title: (document.title || "").slice(0, 150) });
    stampForms();
    if (wired) return; // consent withdrawn and given again: the listeners below are already in place
    wired = true;
    watchForms();
    watchReading();
    document.addEventListener("submit", function () { if (started) stampForms(); }, true);
    document.addEventListener("click", function (e) {
      var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
      if (!a) return;
      var href = a.getAttribute("href") || "";
      if (CTA_RX.test(href)) track("cta_click", { cta: (a.textContent || "").trim().slice(0, 60), to: href.slice(0, 150) });
      else if (/\.pdf(\?|#|$)/i.test(href)) {
        // The file's own path (/documents/<kind>/<slug>.pdf) is what the page map knows the product of.
        var filePath = "";
        try { filePath = new URL(href, location.href).pathname; } catch (err) { filePath = ""; }
        track("resource_download", { file: href.slice(0, 150) }, filePath || undefined);
      }
    }, true);
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flush(true); });
    window.addEventListener("pagehide", function () { flush(true); });
  }

  function stop() {
    started = false;
    queue = [];
    del("localStorage", VID_KEY);
    del("sessionStorage", SESSION_KEY);
    var inputs = document.querySelectorAll('input[name="markhub_visitor_id"]');
    for (var i = 0; i < inputs.length; i++) inputs[i].parentNode.removeChild(inputs[i]);
  }

  function useCookieYes() {
    cmp = "cookieyes";
    // Accept / Reject / Customise in the CookieYes banner, now or later in the session.
    document.addEventListener("cookieyes_consent_update", function (e) {
      var accepted = (e && e.detail && e.detail.accepted) || [];
      if (accepted.indexOf("analytics") !== -1) start();
      else stop();
    });
    if (granted()) start();
  }

  // A browser marked internal tells the form plugin so, with or without analytics consent: it identifies no one,
  // it only keeps Solix's own test submissions out of the numbers.
  function stampInternal() {
    if (get("localStorage", INTERNAL_KEY) !== "1") return;
    var forms = document.getElementsByTagName("form");
    for (var i = 0; i < forms.length; i++) {
      if (forms[i].querySelector('input[name="markhub_internal"]')) continue;
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "markhub_internal";
      input.value = "1";
      forms[i].appendChild(input);
    }
  }

  function boot() {
    stampInternal();
    document.addEventListener("submit", stampInternal, true);
    document.addEventListener("click", stampInternal, true); // AJAX forms are sent from a button click, not a submit
    if (cfg.ownBanner !== true) {
      // CookieYes loads asynchronously: its cookie or API is usually there at once, otherwise wait for it.
      if (cookieYes() || typeof window.getCkyConsent === "function") { useCookieYes(); return; }
      var tries = 0;
      var wait = setInterval(function () {
        tries++;
        if (cookieYes() || typeof window.getCkyConsent === "function" || tries > 40) {
          clearInterval(wait);
          useCookieYes(); // no consent tool after 10s: nothing is tracked (granted() stays false)
        }
      }, 250);
      document.addEventListener("cookieyes_banner_load", function () { if (!cmp) { clearInterval(wait); useCookieYes(); } });
      return;
    }
    var c = consent();
    if (c) announce(c);
    if (c === "all") start();
    else if (!c) showBanner();
  }

  window.markhub = {
    loaded: true,
    openConsent: function () {
      if (cmp === "cookieyes" && typeof window.revisitCkyConsent === "function") window.revisitCkyConsent();
      else showBanner();
    },
    consent: consent,
    visitorId: visitorId,
    track: track,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();

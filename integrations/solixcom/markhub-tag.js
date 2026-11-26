/*! MarkHub tag for www.solix.com: cookie consent, product-interest tracking and form linking.
 *
 * Loaded by one Google Tag Manager "Custom HTML" tag on All Pages:
 *   <script src="https://solix-backend-mf5w.onrender.com/api/markhub.js" async></script>
 *
 * What it does
 * - Shows a small cookie banner until the visitor chooses. Nothing is tracked and no identifier is stored before
 *   "Accept"; "Essential only" (or the browser's Global Privacy Control signal) keeps it that way.
 * - After consent: an anonymous visitor id, and page views, engaged reading (45s active), deep scroll (75%),
 *   demo/contact/download clicks, resource views and lead-form views, batched to MarkHub's /api/track.
 * - Adds a hidden "markhub_visitor_id" field to every form, so the WordPress plugin can link the submission to the
 *   visitor's browsing (and therefore to their product interest) when it forwards the lead.
 * - Pushes "markhub_consent" to the GTM dataLayer ({ analytics: "granted" | "denied" }), so other tags (Clarity,
 *   LinkedIn Insight, GA) can be gated on the same choice, and updates Google Consent Mode when gtag is present.
 * - window.markhub.openConsent() reopens the banner (link it from the footer as "Cookie settings").
 * - ?solix_internal=1 marks this browser as Solix staff, so its activity stays out of every metric.
 *
 * Optional settings, before this script loads: window.markhubConfig = { api: "https://...", privacyUrl: "/privacy-policy/" }
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
  var gpc = navigator.globalPrivacyControl === true;
  function consent() { return get("localStorage", CONSENT_KEY) || (gpc ? "essential" : null); }
  function granted() { return consent() === "all"; }

  function announce(value) {
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

  if (/[?&]solix_internal=1\b/.test(location.search)) set("localStorage", INTERNAL_KEY, "1");

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

  function track(type, meta) {
    if (!granted()) return;
    var ev = { type: type, path: location.pathname };
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

  function start() {
    if (started || !granted()) return;
    started = true;
    session = sessionId();
    if (session.fresh) track("session_start");
    track(/^\/resources\//.test(location.pathname) ? "resource_view" : "page_view", { title: (document.title || "").slice(0, 150) });
    stampForms();
    watchForms();
    watchReading();
    document.addEventListener("submit", stampForms, true);
    document.addEventListener("click", function (e) {
      var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
      if (!a) return;
      var href = a.getAttribute("href") || "";
      if (CTA_RX.test(href)) track("cta_click", { cta: (a.textContent || "").trim().slice(0, 60), to: href.slice(0, 150) });
      else if (/\.pdf(\?|$)/i.test(href)) track("resource_download", { file: href.slice(0, 150) });
    }, true);
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") flush(true); });
    window.addEventListener("pagehide", function () { flush(true); });
  }

  function boot() {
    var c = consent();
    if (c) announce(c);
    if (c === "all") start();
    else if (!c) showBanner();
  }

  window.markhub = {
    loaded: true,
    openConsent: showBanner,
    consent: consent,
    visitorId: visitorId,
    track: track,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();

// The live page's behaviour. Loaded as a FILE, not inline.
//
// ⛔ hazync.org SENDS `script-src 'self'` WITH NO 'unsafe-inline'. An inline <script> in public.html
//    is blocked by the browser with no visible error on the page -- the styling still works, because
//    style-src does allow inline, so the only symptom is the status stuck on "connecting…" next to a
//    broken image. That is what shipped the first time. Keep this in its own file.
//
// ⚠ The CSP also sets connect-src 'self' and img-src 'self' data:, so meta.json and frame.png are
//   fetchable only because they sit on the SAME origin as this page. Serving either from elsewhere
//   would fail the same silent way.
(function () {
  // A frame older than this is no longer "live"; older than DEAD_S and the fleet has almost
  // certainly stopped. Both are generous next to a 1 Hz renderer.
  var STALE_S = 30, DEAD_S = 300, POLL_MS = 2000;

  var img = document.getElementById('frame'),
      statusEl = document.getElementById('status'),
      banner = document.getElementById('banner'),
      renderedEl = document.getElementById('rendered'),
      lastRendered = null;

  function ago(s) {
    if (s < 60) return Math.round(s) + 's ago';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    return (s / 3600).toFixed(1) + ' hours ago';
  }

  function setState(cls, text, bannerText) {
    statusEl.className = cls;
    statusEl.textContent = text;
    if (bannerText) {
      banner.className = 'show ' + cls;
      banner.textContent = bannerText;
    } else {
      banner.className = '';
    }
  }

  function tick() {
    // ⛔ Cache-bust BOTH. Without it the browser serves one frame for ever and the page looks
    // frozen rather than broken -- the exact failure this page exists to make visible.
    fetch('meta.json?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('meta ' + r.status);
        return r.json();
      })
      .then(function (m) {
        var age = Date.now() / 1000 - m.rendered_at;
        renderedEl.textContent = 'frame rendered ' + new Date(m.rendered_at * 1000)
          .toISOString().replace('T', ' ').slice(0, 19) + ' UTC';

        if (age > DEAD_S) {
          setState('dead', 'not live',
            'This frame is ' + ago(age) + '. The fleet is not running, or the feed has stopped ' +
            '— what you are looking at is the last frame produced, not the present.');
        } else if (age > STALE_S) {
          setState('stale', 'delayed',
            'Last frame ' + ago(age) + ' — the feed is behind.');
        } else {
          setState('live', 'live · ' + ago(age), null);
        }

        if (m.rendered_at !== lastRendered) {
          lastRendered = m.rendered_at;
          img.src = 'frame.png?t=' + m.rendered_at;
        }
      })
      .catch(function () {
        // ⚠ A FAILED FETCH IS NOT PROOF THE FLEET IS DOWN — it is proof we cannot tell. Saying
        // "offline" here would be asserting something we do not know.
        setState('dead', 'no signal',
          'Cannot reach the live feed from this browser. This says nothing about whether the ' +
          'fleet is running.');
      });
  }

  tick();
  setInterval(tick, POLL_MS);
})();

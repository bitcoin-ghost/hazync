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
  var STALE_S = 30, DEAD_S = 300, POLL_MS = 1000;

  var img = document.getElementById('frame'),
      statusEl = document.getElementById('status'),
      banner = document.getElementById('banner'),
      renderedEl = document.getElementById('rendered'),
      lastRendered = null,
      shareBtn = document.getElementById('share'),
      shareMsg = document.getElementById('sharemsg'),
      state = { rendered_at: null, age: null, live: false };

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

        state.rendered_at = m.rendered_at;
        state.age = age;
        state.live = age <= STALE_S;

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



  // ── Share ───────────────────────────────────────────────────────────────────────────────────────
  //
  // ⛔ A LINK TO frame.png IS NOT A LINK TO THIS MOMENT. It is overwritten about once a second, so a
  //    recipient opening it later sees a different frame — or a stale one. So we share the IMAGE
  //    BYTES as they are right now, plus a link to the live page. The bytes are the moment; the link
  //    is where to watch it continue.
  //
  // ⚠ The frame has its own UTC timestamp drawn into it, so a shared image says when it is from even
  //   after it has been passed along. The share text says so too, because a stale frame passed to
  //   someone else would otherwise read as live.
  //
  // No new server surface: this fetches a file the page already loads, from the same origin, which is
  // all `connect-src 'self'` permits anyway.

  function say(msg) {
    shareMsg.textContent = msg || '';
    if (msg) setTimeout(function () {
      if (shareMsg.textContent === msg) shareMsg.textContent = '';
    }, 6000);
  }

  function shareText() {
    if (!state.rendered_at) return 'Hazync — zkVM bitcoin block proofs';
    if (state.live) return 'Hazync — proving bitcoin blocks, live right now';
    return 'Hazync — the last frame produced, ' + ago(state.age) + ' (not live)';
  }

  function stamp() {
    var d = state.rendered_at ? new Date(state.rendered_at * 1000) : new Date();
    return d.toISOString().replace(/[-:]/g, '').slice(0, 15) + 'Z';
  }

  function fallback(blob, name) {
    // No file sharing here — hand over the PNG and put the link on the clipboard instead.
    var url = URL.createObjectURL(blob),
        a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 10000);

    var link = location.origin + location.pathname;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(link).then(
        function () { say('Image saved, and the link is on your clipboard.'); },
        function () { say('Image saved. The link is ' + link); });
    } else {
      say('Image saved. The link is ' + link);
    }
  }

  shareBtn.addEventListener('click', function () {
    shareBtn.disabled = true;
    say('Capturing\u2026');

    // Cache-bust so we share what is on screen, not whatever the browser held.
    fetch('frame.png?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('frame ' + r.status);
        return r.blob();
      })
      .then(function (blob) {
        var name = 'hazync-' + stamp() + '.png',
            file = new File([blob], name, { type: 'image/png' }),
            payload = {
              files: [file],
              title: 'Hazync live',
              text: shareText(),
              url: location.origin + location.pathname
            };

        if (navigator.canShare && navigator.canShare({ files: [file] }) && navigator.share) {
          return navigator.share(payload).then(
            function () { say('Shared.'); },
            function (err) {
              // ⚠ A CANCELLED SHARE IS NOT A FAILURE. Reporting an error when someone simply
              //   dismissed the sheet is worse than saying nothing.
              if (err && err.name === 'AbortError') { say(''); return; }
              fallback(blob, name);
            });
        }
        fallback(blob, name);
      })
      .catch(function (err) {
        say('Could not capture the frame (' + (err && err.message ? err.message : 'unknown') + ').');
      })
      .then(function () { shareBtn.disabled = false; });
  });

  tick();
  setInterval(tick, POLL_MS);
})();

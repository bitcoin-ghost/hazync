// Runs the REAL live.js against a mock browser and reports what the Share button did.
//
// Loading the shipped file rather than a copy of its logic is the point: a test that re-implements
// the handler passes while the page is broken. Usage:
//
//   node share_harness.js <scenario>
//
// Scenarios: share-ok | share-cancelled | no-file-share | fetch-fails | stale
// Prints one JSON object describing the outcome.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const scenario = process.argv[2] || 'share-ok';
const out = { scenario, shared: null, downloaded: null, clipboard: null, messages: [], errors: [] };

const listeners = {};
const els = {};
function el(id) {
  if (!els[id]) {
    els[id] = {
      id, textContent: '', disabled: false, className: '', src: '', href: '', download: '',
      style: {}, addEventListener: (ev, fn) => { listeners[id + ':' + ev] = fn; },
      appendChild() {}, remove() {}, click() { out.downloaded = this.download; },
      setAttribute() {},
    };
  }
  return els[id];
}

class FakeBlob { constructor(parts, opts) { this.parts = parts; this.type = (opts || {}).type; } }
class FakeFile extends FakeBlob {
  constructor(parts, name, opts) { super(parts, opts); this.name = name; }
}

const sandbox = {
  console,
  setTimeout: (fn, ms) => (ms > 100 ? 0 : setTimeout(fn, 0)),   // skip the 6 s message clear
  setInterval: () => 0,
  Date,
  URL: { createObjectURL: () => 'blob:mock', revokeObjectURL: () => {} },
  File: FakeFile,
  Blob: FakeBlob,
  Error,
  location: { origin: 'https://hazync.org', pathname: '/live/' },
  document: {
    getElementById: el,
    createElement: () => el('_a'),
    body: { appendChild() {}, removeChild() {} },
  },
  navigator: {},
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
};

// Per-scenario browser
const blob = new FakeBlob(['png-bytes'], { type: 'image/png' });
sandbox.fetch = (u) => {
  if (String(u).startsWith('meta.json')) {
    const age = scenario === 'stale' ? 900 : 2;
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ rendered_at: Date.now() / 1000 - age, bytes: 1234 }),
    });
  }
  if (scenario === 'fetch-fails') return Promise.resolve({ ok: false, status: 503 });
  return Promise.resolve({ ok: true, blob: () => Promise.resolve(blob) });
};

if (scenario === 'no-file-share') {
  sandbox.navigator = {
    clipboard: { writeText: (t) => { out.clipboard = t; return Promise.resolve(); } },
  };
} else {
  sandbox.navigator = {
    canShare: () => true,
    share: (p) => {
      if (scenario === 'share-cancelled') {
        const e = new Error('cancelled'); e.name = 'AbortError';
        return Promise.reject(e);
      }
      out.shared = {
        title: p.title, text: p.text, url: p.url,
        fileName: p.files && p.files[0] && p.files[0].name,
        fileType: p.files && p.files[0] && p.files[0].type,
      };
      return Promise.resolve();
    },
    clipboard: { writeText: (t) => { out.clipboard = t; return Promise.resolve(); } },
  };
}

const src = fs.readFileSync(path.join(__dirname, 'live.js'), 'utf8');
vm.createContext(sandbox);
try {
  vm.runInContext(src, sandbox);
} catch (e) {
  out.errors.push('load: ' + e.message);
}

// let the initial tick() settle so `state` is populated, then press Share
setTimeout(() => {
  const click = listeners['share:click'];
  if (!click) { out.errors.push('no click handler bound to #share'); return done(); }
  click();
  setTimeout(done, 60);
}, 60);

function done() {
  out.messages = [els.sharemsg ? els.sharemsg.textContent : null];
  out.buttonReEnabled = els.share ? els.share.disabled === false : null;
  console.log(JSON.stringify(out));
}

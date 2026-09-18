# Coordinator reference

**Generated. Do not edit by hand.** Regenerate with `python3 scripts/gen-coordinator-reference.py`; CI runs `--check`, which fails when this page and the code disagree. The code is authoritative: `coordinator/server.py` for the HTTP API and its configuration, `coordinator/hazync` for the worker CLI (shipped as `hazync-worker`), `coordinator/run-workers.sh` for the launcher (shipped as `hazync-run-workers.sh`). Routes, variables, defaults, read sites, signature checks and status codes are extracted from the source; the one-line meanings are kept in the generator and checked against the code in both directions.

Out of scope: `coordinator/sponsor_bot.py` (see `docs/SPONSOR_BOT.md`) and the deploy units under `coordinator/deploy/` (see `coordinator/deploy/RUNBOOK.md`).

## HTTP API

Paths are as `server.py` sees them. The public coordinator is behind nginx, which maps them as follows (`coordinator/deploy/nginx-hazync.conf`):

| nginx location | proxied to |
|---|---|
| `= /hazync/api/state` | `http://127.0.0.1:8899/api/state` |
| `/hazync/api/` | `http://127.0.0.1:8899/api/` |

Nginx limits the API to `10r/s` per client address; `client_max_body_size 8m`.

Applies to every route, from `H` in `server.py`:

- Every response carries `Access-Control-Allow-Origin: *`.
- `GET /api/*` is limited per client IP by `rate_ok(ip, "r", RATE_MAX_GET)`, and POST by `rate_ok(ip)` (`RATE_MAX`), both over `RATE_WINDOW`; over the limit is `429`. `X-Forwarded-For` is used only from `TRUSTED_PROXIES`, and `RATE_EXEMPT` bypasses the limit.
- A POST body over `MAX_BODY` is `413`; an unknown POST path is `404`.
- A POST whose `User-Agent` starts `hazync-worker/` has that version recorded against the caller's key (`note_client_version`).
- Signatures are ed25519, `pubkey` 32 bytes and `sig` 64 bytes, hex.

### GET

| path | query | handler | status codes | purpose |
|---|---|---|---|---|
| `/api/bot/<rest>` | — | — | — | The sponsor bot's API (#351), read side: `/api/bot/queue` (sponsorships that hold, oldest payment first, each with `todo`, the heights nobody has proven), `/api/bot/blocks?h=` (up to 2,000 heights: `covered`, `proofs` by key and time, a live `claimed`, `held`), `/api/bot/sponsorship/<id>` (name, status, `held`). Only a direct loopback connection with no `X-Forwarded-For`, signed with the key in `SPONSOR_BOT_PUBKEY_FILE`: `X-Hazync-Bot-Ts`, `X-Hazync-Bot-Nonce`, `X-Hazync-Bot-Sig` over `bot_message` (method, path with query, ts, nonce, body hash); a nonce is used once. `401` unsigned or stale, `403` wrong key, replay or not loopback, `503` no key configured. Not rate limited. |
| `/api/state` | `slim` | `state_cached()` | 200 | Board snapshot: `progress`, `blocked` (the frontier's next block and why), the board window, `leaderboard`, `recent` submissions, `claims`, `fold_claims` (#333), `frontier_proof`, `timeline`, `signatures`, `verify_mode`. `?slim=1` omits `vranges`. Coalesced for `STATE_CACHE_TTL` (`state_cached`). |
| `/api/vranges` | — | `vranges_cached()` | 200, 304 | The full verified-range index (`lo`, `hi`, `handle`, `fold`, `proof` link), built by `build_vranges`. Single-flight cache for `VRANGES_CACHE_TTL`; weak `ETag`, `304` on `If-None-Match`. |
| `/api/blockstatus` | `prover` | `known_handles_cached()`, `block_status_cached()` | 200, 304, 404 | Every block's furthest state as runs `[lo, hi, status]` (3 proven, 4 folded, 5 anchored). `?prover=<handle>` keeps only ranges that prover proved; `404` for an unknown handle. `ETag`/`304`. |
| `/api/block/<height>` | — | `block_detail()` | 200, 400, 404 | Everything about one block: proofs covering it, who anchored it, a live claim, a public sponsor. `400` unless the segment is 1-9 digits; `404` above the chain tip. A live fold claim covering the block is `fold_claim` (#333). |
| `/api/sponsor` | — | `sponsor_info()` | 200 | Sponsorship settings: `open`, `max_blocks`, `payments` (always `false`), `priced`, `bands`, `btc_usd`, `name_max`. See `docs/SPONSORSHIP.md`. |
| `/api/sponsor/quote` | `lo`, `hi` | `sponsor_quote()` | 200 | Minimum USD and sats for `?lo=&hi=`. Answers while sponsorship is closed; refuses spans that are not open (proven, anchored, claimed or already held). |
| `/api/sponsor/status/<token>` | — | `sponsor_status()` | 200, 404 | One sponsorship, looked up by the sha256 of its private token (`[A-Za-z0-9_-]{16,128}`); `404` otherwise. |
| `/api/sponsors` | — | `sponsors_public()` | 200 | Public table of sponsorships whose status is paid, proving or proven and whose settled amount covers the minimum (`SPONSOR_PUBLIC_SQL`). |
| `/api/pick` | — | `pick()` | 200, 404 | Advice only, claims nothing: the first block after the frontier that is not proven here or at a peer, not held, not (on the first pass) being proven at a peer, and has a witness. `404` if none. |
| `/api/meta` | — | `expected_method_id()`, `frontier_hi()`, `source_sha256()` | 200 | Pre-flight: `method_id` (the `method-id` output of `HAZYNC_HOST`), `frontier`, `reproduce` (`reproduce/METHOD_ID`) and `source_sha256` of the running `server.py`. |
| `/api/foldable` | `limit` | `foldable()` | 200 | Sibling pairs of the canonical fold tree whose parent is not yet verified. `?limit=` clamped to 1-32, default 32 (`FOLDABLE_DEFAULT`; 8 before #333). A pair under a live fold claim is left out. |
| `/api/spine` | — | `spine_head()` | 200, 404 | Spine head metadata (`lo`, `hi`, `out_tip`, `out_leaves`, `range_work`, `sha256`, `bytes`, `handle`, `pubkey`, `ts`); `404` before the first spine. |
| `/api/spine/segments` | — | `spine_segments()`, `spine_head()` | 200 | Who absorbed each block into the spine, as runs keyed on pubkey (#244). Cached for `VRANGES_CACHE_TTL`. |
| `/api/spine/proof` | — | `spine_head()` | 200, 404 | The spine receipt bytes, served as `hazync-spine-1-<hi>.hzk`. Check with `hazync-verify`. |
| `/api/proof/<id>` | — | `parse_any_range()` | 200, 404 | A retained verified receipt for range id `n` or `lo-hi`, served as `hazync-<lo>[-<hi>].hzk`. The id is shape-checked by `parse_any_range` before it becomes a path. Re-verify with `host verify-any`. |
| `/api/witnesses` | `from`, `count` | `bulk_plan()`, `bundle_path()` | 200, 400 | Bulk bundle sync (#69): a streamed tar of `MANIFEST.json` (`served`, `missing`) then the bundles for `?from=&count=`, `count` at most `BULK_MAX`. `400` on a malformed request. |
| `/api/witness/<height>` | — | `parse_range()`, `bundle_path()` | 200, 404 | One witness: the bridge bundle `bundle_<n>.json` if present, else the legacy `block_<n>.json`. Accepts a height or a range id (its `lo`). `404` if neither exists. |
| `/<path>` | — | — | 200, 404 | Static files under `COORD_WEB`, contained to that directory; `/` serves `index.html`. Not rate limited. |

### POST

| path | body fields read | handler | signature checked (key over message, as named in the handler) | status codes | purpose |
|---|---|---|---|---|---|
| `/api/bot/<rest>` | — | — | sponsor bot key over `bot_message` | — | The sponsor bot's API (#351), write side, signed and loopback-only as above: `/api/bot/key` `{pubkey, sponsorship_id, handle}` registers a key in `sponsor_keys` only for a sponsorship that holds and only with the handle its name gives (the trial key, `sponsorship_id` null, only as `SPONSOR: Hazync trial`), never for a second sponsorship (`409`); `/api/bot/proving` `{sponsorship_id}` moves `paid` to `proving` only while it holds (`409` otherwise); `/api/bot/reconcile` marks proven every hold whose span is covered. |
| `/api/submit` | `range`, `pubkey`, `sig`, `receipt`, `handle` | `submit()` | `pk` over `receipt` | 200, 400, 403, 409, 413, 422 | Submit a receipt for range id `range`, signed over the receipt bytes. A range wider than one block must be tiled exactly by verified ranges (a fold, #281). Blocks held for a sponsorship are accepted only from that sponsorship's registered key. Verified with `host verify-any`; a bad signature or proof is `422`, not `403`. No prior claim is required. |
| `/api/claim` | `pubkey`, `handle`, `sig`, `ts`, `nonce` | `claim()` | `pk` over `f'claim:{nonce}:{ts}'.encode()` | 200, 400, 403, 409, 429 | Take the earliest block that is not proven or held, width 1; frontier+1 is re-offered first when a cover of it cannot seam (#281). A claim signed by its key over `claim:<nonce>:<ts>` (within `BEAT_SKEW`) speaks for that key: the per-key cap and the re-take wait count signed and unsigned claims apart, and a signature that does not verify is refused, not treated as unsigned (#310). Unsigned claims are accepted unless `CLAIM_REQUIRE_SIG=1`. A `nonce` makes a retried claim return the same block (#268). Advisory: `submit` never requires it. |
| `/api/spine` | `pubkey`, `sig`, `receipt`, `handle` | `submit_spine()` | `pk` over `receipt` | 200, 400, 403, 409, 413 | Submit a new spine head, signed over the receipt. `host verify-range` must pass (full genesis pin), then `verify-any` supplies `lo`/`hi`; `lo` must be 1 and `hi` must exceed the current head (`409` otherwise). Recorded as a `spine:1-<hi>` submission. |
| `/api/beat` | `range`, `pubkey`, `sig`, `ts` | `beat()` | `pk` over `f'{rid}:{ts}'.encode()` | 200, 400, 401, 403, 409 | Keep the caller's own claim alive. Signed over `<range>:<ts>` with `ts` as integer seconds within `BEAT_SKEW`. Only the assignee of a live claim may beat it, and not past `CLAIM_MAX`. Rejected beats are logged (`log_beat_rejection`). |
| `/api/rotate` | `sig_old`, `sig_new`, `ts`, `handle`, `old_pubkey`, `new_pubkey` | `rotate()` | `old` over `msg`; `new` over `msg` | 200, 400, 403, 409, 410 | Off by default: `410` unless `ROTATE_ENABLED=1` (#311). Key rotation (#113): both `old_pubkey` and `new_pubkey` sign `hazync-rotate-v1:<old>:<new>:<ts>` (`rotate_message`), `ts` within `ROTATE_MAX_SKEW`. Refuses keys on the moderation list, an old key that already rotated (`409`) and cycles. Records an edge in `rotations`; `vranges` and `submissions` are not rewritten, the old key keeps working, and its work resolves to the head. Used by `hazync rotate`. |
| `/api/sponsor` | `amount_sats`, `lo`, `hi`, `name` | `sponsor_request()` | **none** | 202, 400, 503 | Sponsorship request `{lo, hi, name, amount_sats}`. `503` unless `SPONSOR_OPEN=1`; `amount_sats` must reach the span's minimum. Returns `202` with the private status token once; only its sha256 is stored. Without payments connected it only records the request. With BTCPay connected it opens an invoice for the pledge in sats (status `invoiced`, `checkout_url` in the answer) that holds the blocks while it is open; `503` if BTCPay fails (nothing recorded), `429` over the unpaid-hold caps. |
| `/api/foldclaim` | `pubkey`, `result`, `handle`, `ts`, `nonce`, `sig` | `fold_claim()` | `pk` over `f'foldclaim:{rid}:{nonce}:{ts}'.encode()` | 200, 400, 403, 409, 429 | Reserve one pair offered by `/api/foldable` for `FOLD_CLAIM_TTL` seconds, so other folders are not offered it (#333). Signed by its key over `foldclaim:<result>:<nonce>:<ts>` within `BEAT_SKEW`. Only a key with a verified submission may hold one (`403` with `unproven` otherwise), at most `FOLD_CLAIM_CAP` at a time (`429`); `409` if another key holds the pair or it is already proven. The holder asking again gets its claim back, never extended. Held in memory. Advisory: `submit` accepts a valid fold from anyone, and a verified fold releases its claim. |

`OPTIONS` on any path: CORS pre-flight: `204` with `Access-Control-Allow-Origin: *`.

Status codes are those the handler can return directly; the global `404`, `413` and `429` above apply as well.

⚠ The comment above the dispatch in `H.do_POST` says allocation endpoints are gone ("no claim, no heartbeat"). It predates `/api/claim` and `/api/beat` coming back; the dispatch table is what runs.

## Coordinator environment (`coordinator/server.py`)

In the order the file reads them. "—" means no default in the call: the variable is unset unless provided.

| variable | default as written | read in | meaning |
|---|---|---|---|
| `COORD_PORT` | `'8899'` | constant `PORT` | Listen port. |
| `COORD_BIND` | `'0.0.0.0'` | constant `BIND` | Listen address. Use `127.0.0.1` behind a reverse proxy; a non-loopback bind in an insecure mode refuses to start (see `COORD_ALLOW_PUBLIC_INSECURE`). |
| `COORD_DB` | `'coordinator.db'` | constant `DB` | SQLite database path. |
| `COORD_WEB` | `os.path.join(os.path.dirname(__file__), 'web')` | constant `WEB` | Directory served as static files. |
| `TIP_HEIGHT` | `'958301'` | constant `TIP_FLOOR` | Floor for `chain_tip()`, which bounds acceptable range ids and the progress denominator. |
| `TIP_CACHE_TTL` | `'300'` | constant `TIP_TTL` | Seconds `_servable_high()` caches the highest block with a bundle on disk. |
| `TIP_FILE` | `'/var/lib/hazync/node_tip'` | constant `TIP_FILE` | File holding the node's height, written by `deploy/hazync-node-tip.*`. |
| `TIP_FILE_MAX_AGE` | `'3600'` | constant `TIP_FILE_AGE` | Seconds after which `TIP_FILE` is ignored as stale. |
| `RANGE_SIZE` | `'1000'` | constant `RANGE_SIZE` | Width of a board row, the legacy claim grid, and the default `BULK_MAX`. |
| `SEED_RANGES` | `'60'` | constant `SEED` | Number of `RANGE_SIZE` rows `init_db()` seeds. |
| `WITNESS_DIR` | `os.path.join(os.path.dirname(__file__), 'witnesses')` | constant `WITNESS` | Legacy per-block witnesses, `block_<n>.json`. |
| `HAZYNC_BRIDGE_OUT` | `''` | constant `BRIDGE_DIR` | Archive-node bundle directory, `bundle_<n>.json`; empty means none. |
| `BULK_MAX` | `str(RANGE_SIZE)` | constant `BULK_MAX` | Maximum `count` for one `GET /api/witnesses`. |
| `HAZYNC_HOST` | `''` | constant `HOST_BIN` | Host binary run for `verify-any`, `verify-range` and `method-id`. |
| `VERIFY_MODE` | `'mock' if not HOST_BIN else 'real'` | constant `VERIFY` | `real` runs the host; `mock` accepts without verifying and still needs `COORD_ALLOW_MOCK`. |
| `COORD_STATE` | `os.path.join(os.path.dirname(__file__), 'state')` | constant `STATE_DIR` | Scratch directory for receipts while they are verified. |
| `COORD_PROOFS` | `os.path.join(os.path.dirname(__file__), 'proofs')` | constant `PROOFS_DIR` | Retained receipts, `proof_<id>.bin`, served by `/api/proof/`. |
| `COORD_SPINE` | `os.path.join(os.path.dirname(__file__), 'spine')` | constant `SPINE_DIR` | `spine.bin` and `spine.json`. |
| `GENESIS_TIP` | `'6fe28c0ab6f1b372c1a6a246ae63f74f931e8365e15a089c68d6190000000000'` | constant `GENESIS_TIP` | Genesis block hash (internal byte order) used by `is_genesis_anchored`. |
| `CLAIM_TTL` | `'3600'` | constant `CLAIM_TTL` | Seconds a claim stays live after `COALESCE(last_beat, claimed_at)`; a claim that has never beaten is released sooner, at `CLAIM_GRACE`. |
| `CLAIM_MAX` | `'86400'` | constant `CLAIM_MAX` | Hard cap in seconds from `claimed_at`, whatever the beats. |
| `CLAIM_GRACE` | `'600'` | constant `CLAIM_GRACE` | Seconds before a claim that has never beaten is released (#296). |
| `CLAIM_OPEN_MAX` | `'4'` | constant `CLAIM_OPEN_MAX` | Live claims one key may hold at once; a further claim is refused with 429 until one is proven or lapses. `0` means no limit. |
| `CLAIM_RETAKE_WAIT` | `'3600'` | constant `CLAIM_RETAKE_WAIT` | Seconds before a key may re-take a block its own claim let lapse without a heartbeat; other keys are offered it at once. `0` means straight away. |
| `CLAIM_REQUIRE_SIG` | `'0'` | constant `CLAIM_REQUIRE_SIG` | `1` refuses claims that are not signed by their key (#310). Off by default: workers up to v0.21.4 do not sign claims. |
| `FOLD_CLAIM_TTL` | `'60'` | constant `FOLD_CLAIM_TTL` | Seconds a fold claim (`POST /api/foldclaim`) holds its pair. Held in memory, so a restart forgets them (#333). |
| `FOLD_CLAIM_CAP` | `'2'` | constant `FOLD_CLAIM_CAP` | Live fold claims one key may hold at once; a further one is refused with 429 (#333). |
| `BEAT_SKEW` | `'120'` | constant `BEAT_SKEW` | Allowed distance in seconds between a beat's signed `ts` and server time. |
| `MAX_ATTEMPTS` | `'3'` | constant `MAX_ATTEMPTS` | Failure count at which `/api/state` flags the frontier blocker as needing attention. |
| `MAX_ENV_FAILURES` | `'12'` | constant `MAX_ENV_FAILURES` | Intended cap for environmental failures. ⚠ Read into a constant that nothing in the file uses. |
| `CLAIM_WIDTH` | `'1'` | constant `CLAIM_WIDTH` | Second accepted claim-id grid in `parse_range`, beside `RANGE_SIZE`. |
| `MAX_BODY` | `str(8 << 20)` | constant `MAX_BODY` | Maximum POST body and base64 receipt length, in bytes (`413` above it). |
| `MAX_HANDLE` | `'48'` | constant `MAX_HANDLE` | Handle length cap. A registered sponsor key gets room for `SPONSOR: ` plus a 40-character name if that is longer (`handle_cap`). |
| `RATE_MAX` | `'120'` | constant `RATE_MAX` | POST requests per IP per window. |
| `RATE_MAX_GET` | `'600'` | constant `RATE_MAX_GET` | `GET /api/*` requests per IP per window. |
| `RATE_WINDOW` | `'60'` | constant `RATE_WINDOW` | Rate-limit window, seconds. |
| `RATE_MAP_MAX` | `'50000'` | constant `RATE_MAP_MAX` | Rate map size at which aged-out keys are evicted. |
| `STATE_CACHE_TTL` | `'1.5'` | constant `STATE_TTL` | Seconds `/api/state` is coalesced. |
| `TRUSTED_PROXIES` | `'127.0.0.1,::1'` | constant `TRUSTED_PROXIES` | Peers whose `X-Forwarded-For` is believed, comma-separated. |
| `HANDLE_DENY` | `'satoshi,satoshinakamoto,admin,administrator,official,bitcoinghost,bitcoinghostofficial,hazync,moderator,mod,root,system,team,support,staff'` | constant `HANDLE_DENY` | Reserved handles, compared after reducing to lowercase letters and digits. |
| `MOD_BLOCK_FILE` | `os.path.join(os.path.dirname(__file__), 'mod_block.txt')` | constant `MOD_BLOCK_FILE` | Takedown list of pubkeys hidden from the public board; re-read on every call. |
| `VRANGES_CACHE_TTL` | `'120'` | constant `VRANGES_TTL` | Cache TTL for `/api/vranges` and `/api/spine/segments`. |
| `VERIFY_CONCURRENCY` | `str(max(1, os.cpu_count() or 2))` | constant `_verify_sem` | Maximum concurrent receipt verifications (`_verify_sem`). |
| `RATE_EXEMPT` | `''` | constant `RATE_EXEMPT` | IPs exempt from rate limiting, comma-separated. |
| `DB_BUSY_TIMEOUT` | `'15'` | constant `DB_BUSY_TIMEOUT` | SQLite busy timeout, seconds. |
| `PEER_COORDINATORS` | `''` | constant `PEERS` | Peer coordinator base URLs, comma-separated; enables peer sync. |
| `PEER_TTL` | `'300'` | constant `PEER_TTL` | Seconds peers' proven and busy heights are cached. |
| `PEER_BUSY_MAX_WIDTH` | `'10000'` | constant `PEER_BUSY_MAX_WIDTH` | Widest peer claim honoured as busy, blocks. |
| `PEER_BUSY_MAX_TOTAL` | `'200000'` | constant `PEER_BUSY_MAX_TOTAL` | Most busy heights accepted from peers. |
| `COORD_ALLOW_UNSIGNED` | — | `__main__`, `verify_sig()` | If set and no ed25519 library is present, `verify_sig` accepts everything. Development only. |
| `ROTATE_MAX_SKEW` | `'300'` | constant `ROTATE_MAX_SKEW` | Allowed distance in seconds between a rotation's `ts` and server time. |
| `ROTATE_ENABLED` | `'0'` | constant `ROTATE_ENABLED` | `1` turns on `POST /api/rotate`. Default `0`: rotation answers `410`, because a stolen `key.hex` can sign both halves of a rotation (#311). |
| `COORD_ALLOW_MOCK` | — | `__main__`, `verify_receipt()`, `verify_spine()` | Required for `VERIFY_MODE=mock` to accept anything. |
| `FRONTIER_CACHE_TTL` | `'2'` | constant `FRONTIER_TTL` | Seconds the frontier chain is cached (single-flight). |
| `FRONTIER_SETTLE` | `'30'` | constant `FRONTIER_SETTLE` | Seconds a verified cover of frontier+1 must predate the frontier snapshot before `claim()` re-offers it and `/api/state` calls it unseamable (#339). |
| `CACHE_MAX_STALE` | `'60'` | constant `CACHE_MAX_STALE` | Seconds past its TTL that `/api/state` and `/api/blockstatus` may still be served while they rebuild in the background; beyond it the caller rebuilds. |
| `SPONSOR_OPEN` | `'0'` | constant `SPONSOR_OPEN` | `1` opens `POST /api/sponsor`. |
| `SPONSOR_MAX_BLOCKS` | `'1000'` | constant `SPONSOR_MAX_BLOCKS` | Largest span one sponsorship may cover. |
| `SPONSOR_PRICE_BANDS` | `list(SPONSOR_PRICE_BANDS_DEFAULT) (when unset)` | constant `SPONSOR_PRICE_BANDS` | JSON `[[lo, hi, usd_per_block], ...]`; set but invalid means unpriced. |
| `SPONSOR_BTC_USD` | — | constant `SPONSOR_BTC_USD` | Dollars per bitcoin for converting minimums to sats when payments are not connected (with BTCPay, its store rate is used); unset means no sats minimum. |
| `SPONSOR_HOLD_ALERT` | `str(6 * 3600)` | constant `SPONSOR_HOLD_ALERT` | Age in seconds after which a hold on the frontier's next block is reported in `/api/state`. |
| `BTCPAY_URL` | `''` | constant `BTCPAY_URL` | BTCPay Server base URL, e.g. `https://donate.hazync.org`. Payments are connected only with this, `BTCPAY_STORE_ID` and a readable `BTCPAY_API_KEY_FILE`. |
| `BTCPAY_STORE_ID` | `''` | constant `BTCPAY_STORE_ID` | The BTCPay store sponsorship invoices are opened in. |
| `BTCPAY_API_KEY_FILE` | `''` | constant `BTCPAY_API_KEY_FILE` | File holding a Greenfield API key for that store only (create and view invoices, view store settings). Kept in a file so the unit never shows it. |
| `BTCPAY_TIMEOUT` | `'15'` | constant `BTCPAY_TIMEOUT` | Seconds a call to BTCPay may take. |
| `SPONSOR_INVOICE_MINUTES` | `'30'` | constant `SPONSOR_INVOICE_MINUTES` | Minutes a sponsorship invoice stays payable, and holds its blocks. |
| `SPONSOR_INVOICE_WATCH` | `str(3 * 86400)` | constant `SPONSOR_INVOICE_WATCH` | Seconds after expiry the poller keeps asking BTCPay about an invoice, so a late payment is credited. |
| `SPONSOR_POLL_S` | `'30'` | constant `SPONSOR_POLL_S` | Seconds between payment polls. |
| `SPONSOR_CONFIRMING_HOLD` | `str(2 * 3600)` | constant `SPONSOR_CONFIRMING_HOLD` | Seconds a payment BTCPay has seen but not confirmed keeps an unpaid hold, re-armed each poll. |
| `SPONSOR_UNPAID_HOLD_MAX` | `'5000'` | constant `SPONSOR_UNPAID_HOLD_MAX` | Most blocks all unpaid invoices together may hold. |
| `SPONSOR_OPEN_INVOICES_PER_IP` | `'3'` | constant `SPONSOR_OPEN_INVOICES_PER_IP` | Most unpaid invoices one address may have open. |
| `SPONSOR_RETURN_URL` | `'https://hazync.org/sponsors/'` | constant `SPONSOR_RETURN_URL` | Where the checkout sends a sponsor back to; `#t=<token>` is appended. |
| `SPONSOR_RATE_TTL` | `'60'` | constant `SPONSOR_RATE_TTL` | Seconds BTCPay's bitcoin price is cached. |
| `SPONSOR_RATE_STALE` | `'600'` | constant `SPONSOR_RATE_STALE` | Seconds the last good BTCPay price is still used when BTCPay stops answering. |
| `SPONSOR_BOT_PUBKEY_FILE` | `''` | constant `SPONSOR_BOT_PUBKEY_FILE` | File holding the sponsor bot's ed25519 public key (hex). Unset or unreadable: every `/api/bot/` request is refused `503`. |
| `SPONSOR_BOT_SKEW` | `'120'` | constant `SPONSOR_BOT_SKEW` | Seconds a sponsor bot request's timestamp may be off; its nonce is remembered twice as long. |
| `BEAT_LOG_WINDOW` | `'600'` | constant `BEAT_LOG_WINDOW` | Seconds identical beat rejections are collapsed into one log line. |
| `COORD_ALLOW_PUBLIC_INSECURE` | — | `__main__` | Allows a non-loopback bind in an insecure mode. Not for production. |
| `PEER_SYNC_INTERVAL` | `'300'` | `__main__` | Seconds between peer sync passes (only when `PEER_COORDINATORS` is set). |

Startup (`__main__`) refuses a non-loopback `COORD_BIND` when `VERIFY_MODE` is not `real`, `COORD_ALLOW_MOCK` or `COORD_ALLOW_UNSIGNED` is set, or the ed25519 library is missing, unless `COORD_ALLOW_PUBLIC_INSECURE` is set.

## Worker CLI (`coordinator/hazync`, shipped as `hazync-worker`)

Every request sends `User-Agent: hazync-worker/{VERSION}`. `VERSION` is `"dev"` in the source and is stamped with the release by `scripts/package-release.sh`; a `dev` build refuses to POST to the default public coordinator (`_guard_dev_writes`).

### Commands

From `main()`'s dispatch table; usage and summary from the module docstring.

| command | handler | usage | summary |
|---|---|---|---|
| `id` | `cmd_id()` | `hazync id` | create/show your signing identity (ed25519) |
| `pick` | `cmd_pick()` | `hazync pick` | show the board frontier (does not claim anything) |
| `prove` | `cmd_prove()` | `hazync prove <id>` | prove that range on your GPU  -> a receipt .bin |
| `selftest` | `cmd_selftest()` | `hazync selftest` | pre-flight: prover present, guest id matches, can verify a real proof |
| `submit` | `cmd_submit()` | `hazync submit <id>` | sign the receipt with your key and submit it for verification |
| `run` | `cmd_run()` | `hazync run [id]` | claim + prove + submit (id optional: takes the earliest free block) |
| `spine` | `cmd_spine()` | `hazync spine [n]` | advance the genesis-anchored spine n steps, each absorbing the widest chunk available (default: all) |
| `fold` | `cmd_fold()` | `hazync fold [n]` | fold n adjacent proven ranges into wider ones (default 1) — helps everyone |
| `rotate` | `cmd_rotate()` | `hazync rotate <key.hex>` | move an older key's blocks onto this box's identity (both keys sign; off unless the coordinator sets ROTATE_ENABLED=1, #311) |
| `notify` | `cmd_notify()` | `hazync notify new` | push to your phone (ntfy) when this worker stops or cannot work; also: test \| off \| <topic or URL> |

### Environment

| variable | default as written | read in | meaning |
|---|---|---|---|
| `HAZYNC_HOME` | `os.path.expanduser('~/.hazync')` | constant `HOME` | Identity (`key.hex`, `handle`) and receipts. A different directory is a different contributor. |
| `COORD_URL` | `DEFAULT_COORD` | constant `COORD` | Coordinator base URL. |
| `HAZYNC_HOST` | `''` | `_find_host()` | Prover binary. Unset: looked for beside the CLI, in `$HAZYNC_HOME/bin` and `$HAZYNC_HOME`, then `hazync-*` names on `PATH`. Never the working directory, and the bare name `host` only beside the CLI (#312). |
| `WITNESS_DIR` | `str(HOME / 'witnesses')` | constant `WITNESS` | Legacy per-block witnesses for the replay path. |
| `BUNDLE_DIR` | `str(HOME / 'bundles')` | constant `BUNDLES` | Bundles fetched from the coordinator for the bridge path. |
| `HAZYNC_ALLOW_DEV_WRITES` | — | `_guard_dev_writes()` | `1`, `true` or `yes` lets a source checkout (`VERSION = "dev"`) POST to the default public coordinator. |
| `HAZYNC_NTFY` | `''` / — | `_ntfy_url()`, `cmd_notify()` | An ntfy topic or URL for alerts when this worker stops or cannot work; overrides what `hazync notify` saved. `off` disables. |
| `HAZYNC_NTFY_REPEAT` | `'3600'` | `notify()` | Seconds before the same problem is pushed again. |
| `HAZYNC_GPU_LOCK` | `'/tmp/hazync-gpu.lock'` | `gpu_lock()` | Lock file serialising GPU jobs on one box; `none` disables it. |
| `HAZYNC_STALL_MIN` | `'600'` | `_prove_watched()`, `_prove_distributed()` | Minimum seconds without segment progress before a prove is killed. |
| `HAZYNC_FIRST_PROGRESS` | `'1800'` | `_prove_watched()` | Seconds allowed before the first segment completes. |
| `HAZYNC_ASSEMBLY_MIN` | `'1800'` | `_prove_watched()` | Floor, in seconds, of the silent lift-and-join budget after the last segment. |
| `HAZYNC_PROVE_TIMEOUT` | `'21600' if watch else '5400'` / `'21600'` | `_prove_watched()`, `_prove_distributed()` | Outer bound, seconds, for one host invocation (6 h for a watched prove, 90 min otherwise). |
| `HAZYNC_TICK` | `'60'` | `_prove_watched()`, `_prove_distributed()` | Seconds between progress lines; a beat is sent on a tick only if a segment finished since the last one. |
| `HAZYNC_PORT` | `'9110'` | `cmd_prove()` | Port `seg-serve` listens on for a distributed run (`--distributed`); workers dial it. Default 9110, the same default the host itself uses. |
| `HAZYNC_FOLD_CONCURRENCY` | `'1'` | `_fold_conc()` | Folds run concurrently within one tree level. |
| `HAZYNC_SPINE_VRANGES_TTL` | `'300'` | constant `SPINE_VRANGES_TTL` | Seconds `hazync spine` reuses its copy of `/api/vranges`. |

### Set for the host binary

| variable | value | set in | meaning |
|---|---|---|---|
| `HAZYNC_RANGE` | `str(lo)` | `_prove_distributed()` | The board block `seg-serve` serves as a mode-6 bridge range (#361/#364). Its presence is what makes the run distributed rather than a fixture prove. |
| `HAZYNC_PORT` | `str(port)` | `_prove_distributed()` | Port `seg-serve` listens on, and the port local workers dial. |
| `HAZYNC_PROGRESS_EVERY` | `'1' (unless already set)` | `_run_with_seg_retry()` | Makes the host print one line per completed segment, which the watchdog reads (#256). |
| `HAZYNC_BRIDGE_OUT` | `BUNDLES` / `str(wd)` | `cmd_prove()`, `cmd_selftest()` | Bundle directory for `prove-range-bridge`. |
| `HAZYNC_WITNESS_DIR` | `WITNESS` | `cmd_prove()` | Witness directory for the replay path `prove-range`. |
| `HAZYNC_SEG_PO2` | `str(_po2)` | `_run_with_seg_retry()` | Segment size for this attempt, stepping down on failure; an inherited value sets the first attempt. |
| `HAZYNC_WORKER_ID` | `f'local{d}'` | `_prove_distributed()` | Log label for each local worker (`local0`, `local1`, …), so a fleet's timings can be attributed per card. |
| `CUDA_VISIBLE_DEVICES` | `str(d)` | `_prove_distributed()` | Pins each local worker to one card. ⛔ One prove per card: two do not fit on a 46 GB card (#97), which is what `gpu_lock` exists to prevent. |

## Launcher (`coordinator/run-workers.sh`, shipped as `hazync-run-workers.sh`)

`run-workers.sh [N] [--stop]`: N defaults to `4`. It checks the host's guest id against `/api/meta` and runs a GPU smoke prove before starting any loop, then restarts each loop's command until it exits `78` (`EX_CONFIG`). Exit `75` (`EX_TEMPFAIL`, nothing to claim right now) waits 30 s and is not a failure. With alerts set up (`hazync notify`), a loop pushes after `NOTIFY_FAIL_STREAK` failures in a row, on recovery and when it stops, and the launcher pushes when it refuses to start.

### Environment

| variable | default as written | meaning |
|---|---|---|
| `COORD_URL` | `https://api.hazync.org` | Coordinator base URL, used for the `/api/meta` guest-id pre-flight. |
| `LOG_DIR` | `$HOME/hazync-workers` | Per-worker logs `worker_<i>.log` and bundle directories `bundles_<i>`. |
| `MODE` | `prove` | What the loops run; see the modes table. |
| `HAZYNC_HOST` | (required) | Prover binary. Required, and must be executable. |
| `HAZYNC_BASE` | `$HOME/hazync-build` | Core/secp source root, exported to the workers. |
| `SKIP_GPU_SMOKE` | `(empty)` | Non-empty skips the pre-flight `prove-block` on a box with a GPU (#261). |
| `HAZYNC_HOME` | `$HOME/.hazync` | Where the handle check looks for `handle`. |
| `NOTIFY_FAIL_STREAK` | `5` | Failures in a row before a worker loop pushes an alert (`hazync notify`); exit 75, nothing to claim right now, does not count. |

### Set for the workers

| variable | value | meaning |
|---|---|---|
| `BUNDLE_DIR` | `$LOG_DIR/bundles_$i` | One bundle directory per worker loop. |

### Modes

| `MODE` | loops |
|---|---|
| `prove` | every loop runs `hazync run` |
| `fold` | every loop runs `hazync fold` |
| `mixed` | with N > 1 loop N-1 folds, and with N > 2 loop N advances the spine; the rest prove. Run it on ONE box: the spine needs one worker fleet-wide |
| `spine` | every loop runs `hazync spine` |
| `distributed` | every loop runs `hazync run --distributed`: claim one board block, serve it across many cards (mode 6) and submit it. Continuous by the same means as the other modes — the CLI exits `EX_TEMPFAIL` (75) when the board has nothing and the loop waits 30 s |

⚠ The script's own header comment lists `MODE` as `prove | fold | mixed`, omitting `spine`, `distributed`; the `case` statement accepts the modes above.

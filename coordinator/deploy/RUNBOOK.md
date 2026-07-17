# Hazync Proof Party — coordinator deploy runbook

Stand up the coordinator on a cheap CPU box and wire it under **one domain** (`bitcoinghost.org/hazync`).
No subdomain, no second website: the box is invisible backend infrastructure, reached through an nginx
proxy — exactly like the existing `/api/pool/vmN/` proxies.

```
  bitcoinghost.org/hazync          the page (story + live board), served from the web root
  bitcoinghost.org/hazync/api/…    proxied to the coordinator box (state / claim / submit / witness)
        one URL for people · one box for data
```

The coordinator is **verify-only (CPU, no GPU)**. Proving + folding happen on contributors' GPU boxes.

---

## 0. Build the `host` binary (needs muscle, briefly)

Building `host` (RISC0 + Bitcoin Core) wants real RAM/CPU — a $10/mo box will choke. Build it once on a
capable box (or reuse a GPU box), then copy just the binary to the cheap coordinator.

```bash
git clone https://github.com/bitcoin-ghost/hazync /opt/hazync && cd /opt/hazync
./provision-vps.sh                 # CPU build (do NOT set GPU=1 — the coordinator only verifies)
# → /opt/hazync/prover/target/release/host
```

Verifying is light, so the cheap coordinator box runs the binary fine — only the *build* needs muscle.

## 1. Coordinator box

```bash
sudo useradd -r -m -d /opt/hazync -s /usr/sbin/nologin hazync   # or reuse an existing user
sudo mkdir -p /opt/hazync/witnesses /opt/hazync/coordinator-state
# place the repo at /opt/hazync (host binary at /opt/hazync/prover/target/release/host)

# witness window: the blocks people can prove right now (rolling window; start small)
./coordinator/deploy/gen-witness-window.sh 1000 /opt/hazync/witnesses

sudo chown -R hazync:hazync /opt/hazync
sudo cp coordinator/deploy/hazync-coordinator.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hazync-coordinator
curl -s localhost:8899/api/state | head -c 300      # smoke test
```

Set `TIP_HEIGHT` in the unit to the real chain tip. `RANGE_SIZE=1000`. The unit binds `127.0.0.1`
(behind the proxy); if the web box is a different machine, set `COORD_BIND` to the private-network IP
and firewall `:8899` to the web box only.

## 2. Wire the single domain (on the WEB box)

Paste `coordinator/deploy/nginx-hazync.conf` into the `bitcoinghost.org` `server { }` block in
`/etc/nginx/sites-enabled/bitcoinghost` (set `proxy_pass` to the coordinator's IP if it's a separate
box), then:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s https://bitcoinghost.org/hazync/api/state | head -c 300   # now reachable via the domain
```

## 3. Go-live page (one page) — DONE

`hazync.html` already carries the live Proof Party (`#party` section) in one scroll, wired to the proxied
API (`/hazync/api/...`), and `hazync-party.html` redirects to it. Until this proxy is live it shows a
clearly-labelled **sample-data preview**; the moment `/hazync/api/state` returns real progress it flips to
live data automatically. Nothing to do here except stand up steps 1–2.

## 4. Prove the loop as a downloader

On any box (the coordinator itself can prove the tiny early blocks on CPU — no GPU needed to seed):

```bash
export COORD_URL=https://bitcoinghost.org/hazync
export HAZYNC_HOST=/path/to/host WITNESS_DIR=/tmp/w
./coordinator/hazync id yourname
./coordinator/hazync run 1          # claim → fetch witness → prove → sign → submit → verify
```

Watch the frontier tick up and your name land on the board. That's the public onboarding path proven
end to end.

## 5. Seed real proofs

Prove blocks 1..N to build a genuine genesis frontier. Tiny early blocks are CPU-provable (~60–110s
each) — no GPU capital needed to seed. Scale with a GPU box later.

## 6. Then post to Delving

Once the page feels right and the board shows real (even if small) frontier data.

---

### Notes
- **Rate limiting** trusts `X-Forwarded-For` from the proxy (`RATE_MAX`/`RATE_WINDOW`).
- **Witness window** = the claimable set. Blocks outside it 404 and the CLI says so; grow it with
  `gen-witness-window.sh` or co-locate an archive/bridge node (the archive decision) when it's worth the
  disk.
- **Back up** `coordinator.db` (the signed ledger) — it's the record of who proved what.

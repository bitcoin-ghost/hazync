---
name: Board or prover problem
about: A worker that fails, stalls or is rejected, or a board that looks wrong
---

<!-- Most rejections are a guest id that differs from the coordinator's. Run `hazync-worker selftest`
     first and include its output. -->

**What happened**


**Versions**
- `hazync-worker` release (`grep '^VERSION' hazync-worker`):
- Your guest id (`hazync-host-x86_64-linux-gnu-cuda method-id`, or the CPU host):
- Coordinator's guest id (`curl -s https://bitcoinghost.org/hazync/api/meta`):
- Segment size (`<host> seg-po2`, and `HAZYNC_SEG_PO2` if you set it):

**Machine**
- GPU (`nvidia-smi -L`) and driver, or CPU only:
- RAM, and provider if rented:
- Launcher: `hazync-run-workers.sh` with `MODE=`, or `hazync-worker` directly:

**Work**
- Block or range (for example `39413` or `1-2`):
- Handle shown on the board:
- Approximate time (UTC):

**Alert** (if `hazync-worker notify` pushed one: its title and text)

```
```

**`hazync-worker selftest` output**

```
```

**Worker log** (the lines around the failure; `hazync-run-workers.sh` writes `worker_<i>.log` under `LOG_DIR`, default `~/hazync-workers`)

```
```

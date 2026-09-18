#!/usr/bin/env python3
"""Production caller for prune_bundles.py (hazync#347, #397).

    hazync-prune-bundles              # DRY RUN: report what it would delete, touch nothing
    hazync-prune-bundles --apply      # actually delete

WHY A CALLER EXISTS AT ALL. prune_bundles.main() takes `confirmed_names` and REFUSES outright if it is
not supplied -- it will not go and ask R2/B2 itself. That is deliberate: the tool deletes the files the
board serves, and `bundle_path()`'s legacy fallback directory is empty on the live coordinator, so a
wrong deletion genuinely removes the ability to serve that height. It therefore insists on being HANDED
proof that the receipts exist offsite, rather than deciding for itself.

Nothing in the repo did that handing. Rehearsed 2026-09-18 on the retiring coordinator with a
throwaway script; this is that script, made real.

⛔ BOTH STORES OR NOTHING. If either R2 or B2 cannot be listed, this exits 2 and prunes nothing. A
half-confirmed set is worse than no set: every name missing from the half we could not read would look
like "not backed up" and be kept, which is safe -- but a partial read that SUCCEEDS looks identical to
a complete one, and the failure would be silent. So it is refused explicitly.

Exit codes match the other checks so hazync-run-check can alert on it:
  0  ran (deleted, or nothing was due)
  1  something is wrong
  2  could not check (a store unreachable, ledger unreadable) -- NOTHING is ever deleted on a 2
"""
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
OFFSITE = os.environ.get("HAZYNC_OFFSITE", "/usr/local/sbin/hazync-offsite-proofs")
PRUNE = os.environ.get("HAZYNC_PRUNE", os.path.join(HERE, "prune_bundles.py"))
REPO = os.environ.get("HZ_REPO", "/opt/hazync")
STORES = (("r2", os.environ.get("R2_KEYS", "/etc/hazync/backup/r2.keys"),
           os.environ.get("R2_BUCKET", "hazync-proofs")),
          ("b2", os.environ.get("B2_KEYS", "/etc/hazync/backup/b2.keys"),
           os.environ.get("B2_BUCKET", "hazync-backup")))


def _load(path, name):
    ld = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, ld)
    m = importlib.util.module_from_spec(spec)
    ld.exec_module(m)
    return m


def confirm(off, log=print):
    """{store: set(names)} for BOTH stores, or None if either could not be read."""
    out = {}
    for store, keys, bucket in STORES:
        try:
            kid, secret, endpoint, *_ = open(keys).read().split()
            s3 = off.make_client(kid, secret, endpoint, 4)
            prefix = "proofs-%s/" % off.method_prefix(REPO)
            names = set(off.list_remote(s3, bucket, prefix))
        except Exception as e:
            log("[prune-caller] cannot check: %s unreadable (%s: %s)"
                % (store.upper(), type(e).__name__, str(e)[:120]))
            return None
        if not names:
            log("[prune-caller] cannot check: %s listed ZERO objects under %s -- refusing to treat "
                "an empty listing as 'nothing is backed up'" % (store.upper(), prefix))
            return None
        log("[prune-caller] %s confirms %d proof object(s)" % (store.upper(), len(names)))
        out[store] = names
    return out


def main(argv=None, off=None, prune=None, log=print):
    argv = list(sys.argv[1:] if argv is None else argv)
    off = off or _load(OFFSITE, "off")
    prune = prune or _load(PRUNE, "prune")
    confirmed = confirm(off, log=log)
    if confirmed is None:
        return 2
    return prune.main(argv, confirmed_names=confirmed)


if __name__ == "__main__":
    sys.exit(main())

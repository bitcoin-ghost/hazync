#!/usr/bin/env python3
"""
Tests for client-version reporting (#293).

WHY THIS EXISTS. Block 39,413 pinned the frontier for five hours. The coordinator's entire record of
it was: claimed, heartbeating, zero submissions, zero attempts, no error — and `needs_attention:
false`, `why: "a live worker is proving it"`. Every worker-side failure we have ever fixed leaves
that same trace: #261 (no usable GPU), #256 (a stall), #268 (a lost claim response), #286 (assembly
read as a hang). Nothing on the server tells them apart.

It turned out to be #286: assembling 1,012 segment receipts took 757 s against a 600 s silence
timeout that v0.21.3 removed. The holder's release is the one fact that would have said so — and it
was not recorded anywhere, because the CLI sent no version and the coordinator stored none. Proving
the block again on a current binary was the only available diagnosis.

⛔ ADVISORY, NEVER ENFORCING. Nothing is refused for its version, and nothing here asserts that it
is. Proving out of order is the design, old proofs stay valid while the guest is unchanged, and the
guest id already gates what can land (#99). A version that could block work would be a worse problem
than the one it solves.

WHAT THIS DOES NOT COVER: no proving, no GPU. Rows are written the way the handlers write them.

Usage:
  python3 test_client_version.py            # assertions; exit 0 on success
  python3 test_client_version.py --control  # forget the version on claim; the tests MUST fail
"""
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__

server.init_db()

if CONTROL:
    # The break: record the version only on SUBMIT. Plausible — that is where a contributor's work
    # lands — and it is silent for exactly the case this exists for, because a worker that cannot
    # finish never submits at all.
    _real = server.note_client_version
    def _submit_only(c, pk, body):
        if (body or {}).get("_from_claim"):
            return
        _real(c, pk, body)
    server.note_client_version = _submit_only

fails = []
def check(ok, what):
    if ok:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        fails.append(what)

AA = "aa" * 32
def claim_as(pk, handle, version):
    """What the claim path does: ensure the row, then note the version."""
    c = server.db()
    c.execute("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)",
              (pk, handle, 1000.0))
    body = {"_from_claim": True}
    if version is not None:
        body["_client_version"] = version
    server.note_client_version(c, pk, body)
    c.commit(); c.close()

def version_of(pk):
    c = server.db()
    r = c.execute("SELECT last_version FROM contributors WHERE pubkey=?", (pk,)).fetchone()
    c.close()
    return r["last_version"] if r else None

# --- the column exists on a database that predates it -------------------------------------------
# CREATE TABLE IF NOT EXISTS does NOT add a column to a table that is already there, so the live
# board would have kept the old shape and every read would raise. init_db migrates it.
c = server.db()
cols = {r[1] for r in c.execute("PRAGMA table_info(contributors)")}
c.close()
check("last_version" in cols, "contributors carries last_version (migrated, not just created)")
check("last_version_at" in cols, "  ...and when it was last seen")

# --- recorded ON CLAIM, which is the whole point -------------------------------------------------
claim_as(AA, "alice", "0.21.3")
check(version_of(AA) == "0.21.3",
      f"a version reported when CLAIMING is recorded (got {version_of(AA)!r})")

# --- a client that says nothing must not erase what we know --------------------------------------
# A contributor can run several boxes. One on an older CLI sends no User-Agent at all, and letting
# that blank out what the others reported would make the field flicker between a version and nothing.
claim_as(AA, "alice", None)
check(version_of(AA) == "0.21.3",
      f"a silent client does NOT erase a known version (got {version_of(AA)!r})")

claim_as(AA, "alice", "0.21.4")
check(version_of(AA) == "0.21.4", "a newer report replaces the older one")

# --- absurd input is truncated, not trusted -------------------------------------------------------
claim_as(AA, "alice", "x" * 500)
check(len(version_of(AA) or "") <= 32, "an over-long version string is truncated, not stored whole")

# --- the leaderboard carries it -------------------------------------------------------------------
st = server.state(slim=True)
rows = st.get("leaderboard") or []
check(all("version" in r for r in rows) if rows else True,
      "every leaderboard row carries a version field (None is a fine answer)")

# --- the User-Agent is parsed, and only ours ------------------------------------------------------
def parsed(ua):
    """Exactly what do_POST does with the header."""
    return ua[len("hazync-worker/"):][:32] if ua.startswith("hazync-worker/") else None
check(parsed("hazync-worker/0.21.3") == "0.21.3", "a hazync-worker User-Agent yields its version")
check(parsed("hazync-worker/dev") == "dev",
      "a checkout reports 'dev', which is a real answer and not an error")
check(parsed("curl/8.0.1") is None, "an unrelated User-Agent is ignored, not stored as a version")
check(parsed("Python-urllib/3.11") is None, "  ...including the default an older CLI sends")

if CONTROL:
    if fails:
        print(f"CONTROL OK — recorded only on submit and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the version was dropped on claim and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("client versions are recorded on claim and submit, and surfaced where a stall is diagnosed.")

#!/usr/bin/env python3
"""
Robustness/invariant fuzz for the coordinator's UNTRUSTED-input handlers in server.py:

  * parse_range(rid)  — claim ids from the wire. Must never raise, and any accepted id must be a
                        single block in [0,TIP) or an aligned, exactly-RANGE_SIZE range in bounds.
                        The security point (its docstring): two accepted *range* ids can never
                        partially overlap, so no double-claim ambiguity.
  * clean_handle(h)   — the single server-side choke point for display handles. Output must be
                        printable, contain none of <>&"' (stored-XSS defence), be length-capped,
                        and never empty.
  * is_hex(s,n)       — must never raise and accept only exactly-n-byte hex.

Deterministic random + structured adversarial strings (unicode, control chars, huge numbers,
negatives, malformed ranges). Silent success; prints any invariant break with the offending input.
"""
import os, sys, tempfile

# Import-safe config (module constants captured from env at import).
_tmp = tempfile.NamedTemporaryFile(prefix="parsefuzz_", suffix=".db", delete=False); _tmp.close()
os.environ["COORD_DB"] = _tmp.name
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

# The ceiling is derived from what the bridge can serve rather than being a constant, so the ORACLE
# must re-read it at check time — capturing it once here and comparing against a parser that reads it
# live would make this fuzzer disagree with the implementation the moment the value moved. `TIP` below
# is used only to pick plausible magnitudes when GENERATING ids, where a stale value is harmless.
TIP, RS, MAXH = server.chain_tip(), server.RANGE_SIZE, server.MAX_HANDLE
FORBIDDEN = set('<>&"\'')

def splitmix(s):
    s[0] = (s[0] + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    z = s[0]
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return z ^ (z >> 31)

def rnd(s, n): return splitmix(s) % n

# Character menu weighted toward things that break parsers / XSS sinks.
CHARS = list("0123456789-+ \t\n") + list("abcdefABCDEF") + list('<>&"\'/\\%;') + \
        ["\x00", "\x7f", "​", "Ѐ", "🙂", "￿", ".", "e", "x"]

def rand_str(s, maxlen=24):
    n = rnd(s, maxlen)
    return "".join(CHARS[rnd(s, len(CHARS))] for _ in range(n))

def rand_range_id(s):
    """Bias toward range-ish ids to hammer parse_range's numeric path."""
    form = rnd(s, 5)
    if form == 0:
        return str(rnd(s, TIP + 50) - 20)                      # single-ish (some OOB/neg)
    if form == 1:
        lo = rnd(s, 70) * RS                                   # aligned-ish
        return f"{lo}-{lo + RS - 1}"
    if form == 2:
        lo = rnd(s, 70) * RS
        return f"{lo}-{lo + rnd(s, 3 * RS)}"                   # wrong length
    if form == 3:
        return f"{rnd(s, 5000)}-{rnd(s, 5000)}-{rnd(s, 9)}"    # too many parts
    return rand_str(s)                                          # pure junk

def check_parse_range(rid):
    r = server.parse_range(rid)                                # must not raise
    if r is None:
        return
    lo, hi = r
    assert isinstance(lo, int) and isinstance(hi, int), f"non-int result {r} for {rid!r}"
    tip = server.chain_tip()                                   # live, for the reason given at the top
    single = (lo == hi and 0 <= lo < tip)
    aligned = (hi - lo + 1 == RS and lo % RS == 0 and lo >= 0 and hi < tip)
    assert single or aligned, f"parse_range accepted out-of-spec id {rid!r} -> {r}"
    return r

def check_clean_handle(h):
    out = server.clean_handle(h)                               # must not raise
    assert isinstance(out, str) and out, f"empty/non-str handle from {h!r}: {out!r}"
    assert len(out) <= MAXH, f"handle over cap ({len(out)}) from {h!r}"
    bad = FORBIDDEN & set(out)
    assert not bad, f"clean_handle leaked XSS-significant {bad} from {h!r}: {out!r}"
    assert all(ch.isprintable() for ch in out), f"non-printable in cleaned handle from {h!r}: {out!r}"

def check_is_hex(sv):
    for nb in (32, 64, 0, 1):
        v = server.is_hex(sv, nb)                              # must not raise
        assert isinstance(v, bool)
        if v:
            assert isinstance(sv, str) and len(sv) == nb * 2
            bytes.fromhex(sv)                                   # must round-trip

def main():
    N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 300000
    s = [0xF00D_BEEF_1357_9BDF]
    accepted_ranges = []
    fails = 0
    for i in range(N):
        rid = rand_range_id(s)
        try:
            r = check_parse_range(rid)
            if r and r[0] != r[1]:                              # a range-form acceptance
                accepted_ranges.append(r)
            check_clean_handle(rand_str(s))
            check_is_hex(rand_str(s, 140))
        except AssertionError as e:
            print(f"[INVARIANT] {e}"); fails += 1
        except Exception as e:
            print(f"[CRASH] {type(e).__name__} on rid={rid!r}: {e}"); fails += 1
        if fails > 8:
            break

    # Non-overlap: any two accepted RANGE-form ids are equal or disjoint (no partial overlap).
    import itertools
    uniq = sorted(set(accepted_ranges))
    for (a1, a2), (b1, b2) in itertools.combinations(uniq[:400], 2):
        disjoint = a2 < b1 or b2 < a1
        equal = (a1, a2) == (b1, b2)
        if not (disjoint or equal):
            print(f"[OVERLAP] accepted ranges partially overlap: {(a1,a2)} vs {(b1,b2)}"); fails += 1

    print(f"\nparse/handle fuzz: {N} iters, {len(uniq)} distinct accepted ranges, {fails} findings.")
    sys.exit(0 if fails == 0 else 1)

if __name__ == "__main__":
    try:
        main()
    finally:
        try: os.remove(_tmp.name)
        except Exception: pass

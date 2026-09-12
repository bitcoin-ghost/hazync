#!/usr/bin/env bash
# Does this binary carry <method-id> in its bytes, exactly once?
#
# A CUDA host cannot run without libcuda.so.1, so on a GPU-less release box "ask the binary which
# guest it carries" is unanswerable. The id is stored as [u32;8] LITTLE-ENDIAN, which puts the raw 32
# bytes on disk in natural order, so it can be read out of the file instead.
#
# ⛔ `grep -P '\x..'` silently finds NOTHING here — a check that cannot pass. Use python.
#
# EXACTLY ONCE is the point, not "at least once": a binary carrying two different ids, or the same id
# twice, is not the unambiguous artifact this is being used to attest.
#
# Extracted (#275) because release.sh had this pasted in two places — host_is_current's staleness
# fallback and step 4's attestation — and they have to agree, since step 4 trusts it to gate
# publication. scripts/test-release-attest.sh drives THIS file, so the thing tested is the thing run.
set -uo pipefail
BIN="${1:?usage: embeds-method-id.sh <binary> <method-id-hex>}"
ID="${2:?usage: embeds-method-id.sh <binary> <method-id-hex>}"
[ -f "$BIN" ] || exit 1
command -v python3 >/dev/null || exit 1
[[ "$ID" =~ ^[0-9a-f]{64}$ ]] || exit 1
n=$(python3 -c "import sys;print(open(sys.argv[1],'rb').read().count(bytes.fromhex(sys.argv[2])))" \
    "$BIN" "$ID" 2>/dev/null) || exit 1
[ "$n" = "1" ]

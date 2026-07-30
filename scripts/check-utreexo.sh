#!/bin/bash
# The accumulator's hash construction exists in THREE places and they must agree byte for byte:
#
#   accumulator/src/lib.rs              host oracle (Rust)   — hash_leaf + parent
#   prover/methods/guest/src/utreexo.rs guest Stump  (Rust)  — parent
#   prover/methods/guest/verify_input.cpp guest leaves (C++) — the real leaf preimages
#
# A drift here does not fail loudly. The host builds proofs the guest cannot verify, so every block
# stops proving — or, worse, only SOME do, because the tag only matters once a preimage length
# collides. This is the same failure shape as the RangeState mirrors (#32), which is why it gets the
# same treatment: a check rather than a comment saying "MUST stay byte-identical".
#
# Checked:
#   1. the tag CONSTANTS are declared and equal in all three
#   2. parent() writes TAG_NODE before the children, in both Rust copies
#   3. both C++ leaf builders (spend-side coin_leaf and creation-side tx_out_leaves) write TAG_LEAF first
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

RS_HOST=accumulator/src/lib.rs
RS_GUEST=prover/methods/guest/src/utreexo.rs
CPP=prover/methods/guest/verify_input.cpp
bad=0

val() { grep -oP "$2" "$1" | head -1; }

leaf_host=$(val  "$RS_HOST"  'TAG_LEAF: u8 = \K0x[0-9a-fA-F]+')
node_host=$(val  "$RS_HOST"  'TAG_NODE: u8 = \K0x[0-9a-fA-F]+')
leaf_guest=$(val "$RS_GUEST" 'TAG_LEAF: u8 = \K0x[0-9a-fA-F]+')
node_guest=$(val "$RS_GUEST" 'TAG_NODE: u8 = \K0x[0-9a-fA-F]+')
leaf_cpp=$(val   "$CPP"      'TAG_LEAF = \K0x[0-9a-fA-F]+')

for v in leaf_host node_host leaf_guest node_guest leaf_cpp; do
    if [ -z "${!v}" ]; then
        echo "FAIL $v: no tag constant found — domain separation has been removed or renamed"
        bad=1
    fi
done
[ $bad -eq 0 ] || exit 1

if [ "$leaf_host" != "$leaf_guest" ] || [ "$leaf_host" != "$leaf_cpp" ]; then
    echo "FAIL TAG_LEAF differs: host=$leaf_host guest=$leaf_guest cpp=$leaf_cpp"
    bad=1
else
    echo "  ok   TAG_LEAF $leaf_host agrees across host, guest and C++"
fi
if [ "$node_host" != "$node_guest" ]; then
    echo "FAIL TAG_NODE differs: host=$node_host guest=$node_guest"
    bad=1
else
    echo "  ok   TAG_NODE $node_host agrees across host and guest"
fi
if [ "$leaf_host" = "$node_host" ]; then
    echo "FAIL TAG_LEAF == TAG_NODE — that is not domain separation, it is a shared prefix"
    bad=1
fi

# The constants existing proves nothing if nothing writes them. Assert the tag is hashed FIRST in each
# construction: a tag appended after the children separates nothing.
for f in "$RS_HOST" "$RS_GUEST"; do
    if ! grep -Pzoq 'fn parent\([^)]*\)[^{]*\{\s*let mut h = Sha256::new\(\);\s*h\.update\(\[TAG_NODE\]\);' "$f"; then
        echo "FAIL $f: parent() does not write TAG_NODE before the children"
        bad=1
    fi
done
if ! grep -Pzoq 'fn hash_leaf\([^)]*\)[^{]*\{\s*let mut h = Sha256::new\(\);\s*h\.update\(\[TAG_LEAF\]\);' "$RS_HOST"; then
    echo "FAIL $RS_HOST: hash_leaf() does not write TAG_LEAF first"
    bad=1
fi

# Both C++ leaf builders. Two sites, and missing either one splits the accumulator against itself:
# spend-side leaves would not match creation-side leaves and no block with a spend could prove.
cpp_tag_writes=$(grep -c 'h.Write(&TAG_LEAF, 1);' "$CPP")
if [ "$cpp_tag_writes" -ne 2 ]; then
    echo "FAIL $CPP: expected 2 TAG_LEAF writes (coin_leaf + tx_out_leaves), found $cpp_tag_writes"
    bad=1
else
    echo "  ok   both C++ leaf builders write TAG_LEAF first"
fi

[ $bad -eq 0 ] && echo "utreexo hash construction agrees across all three implementations."
exit $bad

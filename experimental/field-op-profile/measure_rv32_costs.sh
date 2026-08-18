#!/usr/bin/env bash
# Per-operation rv32im instruction cost, for the two field representations. hazync#129.
#
# The op-count profile (run.sh) answers "how often". This answers "how much", which is the half that
# decides whether the rewrite pays. Counts alone are misleading: the mul:add ratio is 1.02:1 by count
# and about 20:1 by cost.
#
# Instructions, not host time, because in the zkVM a retired instruction is trace. Compiled with the
# same toolchain the guest uses, then counted from objdump. Nothing runs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECP_SRC="${SECP_SRC:-$HOME/hazync-build/secp256k1}"
GCC="${RV32_GCC:-$(find "$HOME/.risc0/toolchains" -name 'riscv32-unknown-elf-gcc' 2>/dev/null | head -1)}"
[ -n "$GCC" ] || { echo "FATAL: no rv32 toolchain under ~/.risc0/toolchains" >&2; exit 1; }
OBJDUMP="$(dirname "$GCC")/riscv32-unknown-elf-objdump"
W="$(mktemp -d)"

count_fns () {   # count_fns <objfile> <regex>
    "$OBJDUMP" -d "$1" | awk -v want="$2" '
      /^[0-9a-f]+ <[^.]/ { n=$2; gsub(/[<>:]/,"",n); if(!(n in seen)){seen[n]=1; ord[++c]=n} cur=n; next }
      /^[ \t]*[0-9a-f]+:/ { if (cur!="") { k[cur]++; if ($3=="mul"||$3=="mulhu"||$3=="mulh") m[cur]++ } }
      END { for(i=1;i<=c;i++) if (ord[i] ~ want) printf "    %-38s %6d%s\n", ord[i], k[ord[i]],
                (m[ord[i]] ? sprintf("   (%d mul/mulhu)", m[ord[i]]) : "") }'
}

echo "  === candidate representation: hand-written, loops unrolled so static == executed ==="
"$GCC" -march=rv32im -mabi=ilp32 -O2 -funroll-all-loops -fno-inline \
    -c "$HERE/addcost.c" -o "$W/addcost.o"
count_fns "$W/addcost.o" '^hz_'

echo "  === stock libsecp 10x26, the backend riscv32im selects ==="
"$GCC" -march=rv32im -mabi=ilp32 -O2 -fno-inline -fno-inline-small-functions \
    -I"$SECP_SRC" -I"$SECP_SRC/src" -I"$SECP_SRC/include" \
    -DECMULT_WINDOW_SIZE=19 -DECMULT_GEN_KB=22 -DUSE_FORCE_WIDEMUL_INT64=1 \
    -DENABLE_MODULE_SCHNORRSIG=1 -DENABLE_MODULE_EXTRAKEYS=1 \
    -c "$SECP_SRC/src/secp256k1.c" -o "$W/secp.o"
count_fns "$W/secp.o" 'fe_(mul|sqr)_inner|fe_impl_(add|negate|normalize|mul_int)'

echo
echo "  fe_impl_mul/sqr appear as 2-instruction wrappers; the work is in fe_mul_inner / fe_sqr_inner."
rm -rf "$W"

# testsupport — host-side validation for the bigint2 field backend

Nothing here is compiled into the guest.

| file | what it is |
|---|---|
| `field_bigint2_native.c` | schoolbook host reference for the four coprocessor primitives, so the backend can run with no zkVM |
| `fe_harness.c` | driver exposing the backend's mod-p operations on stdin/stdout, cross-checked against Python arbitrary precision |
| `stub/` | four headers that let `field_bigint2_impl.h` compile standalone — so the harness tests the REAL file, not a copy of it |

Run both gates with `scripts/field-backend-tests.sh`.

## ⛔ Why libsecp's own suite is not enough on its own

The backend is **lazy**: an element is any value in `[0, 2^256)` congruent to it, so `p`, `p+1` and
`2^256-1` are all legal representations. **libsecp's tests cannot construct that state** — no stock
backend has it, and every test generator produces values below `p`. Two deliberately broken backends
pass libsecp's full suite at count 32 and are caught only here:

- `hz_neg` skipping `hz_canon` — wrong for any input `>= p`
- `fe_to_signed30` skipping `hz_canon` — feeds `modinv32` a value outside its contract

Both gates run, in that order, and neither is redundant.

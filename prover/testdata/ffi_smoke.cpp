// CI smoke test for the Hazync FFI (#31): assert an exact return code, from C++.
//   ffi_smoke <proof> <expected_rc>
#include "hazync_verify.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
int main(int argc, char** argv) {
    if (argc < 3) { fprintf(stderr, "usage: ffi_smoke <proof> <expected_rc>\n"); return 2; }
    FILE* f = fopen(argv[1], "rb");
    if (!f) { fprintf(stderr, "cannot open %s\n", argv[1]); return 2; }
    std::vector<uint8_t> buf; uint8_t t[4096]; size_t n;
    while ((n = fread(t, 1, sizeof t, f))) buf.insert(buf.end(), t, t + n);
    fclose(f);
    HazyncState st; memset(&st, 0, sizeof st);
    int rc = hazync_verify_proof(buf.data(), buf.size(), &st);
    int want = atoi(argv[2]);
    if (rc != want) { fprintf(stderr, "FAIL %s: got %d, expected %d\n", argv[1], rc, want); return 1; }
    if (rc == HAZYNC_OK) printf("ok  %s -> HAZYNC_OK, height %u, %u roots\n", argv[1], st.height, st.root_count);
    else                 printf("ok  %s -> %d (state correctly not written)\n", argv[1], rc);
    return 0;
}

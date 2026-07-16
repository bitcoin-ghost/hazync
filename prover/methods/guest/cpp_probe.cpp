#include <vector>
#include <cstdint>
// Exercises the C++ heap (std::vector / operator new) in the zkVM guest.
extern "C" int cpp_probe(const uint8_t* p, int n) {
    std::vector<uint8_t> v(p, p + n);
    v.push_back(42);
    int s = 0;
    for (auto x : v) s += x;
    return s;
}

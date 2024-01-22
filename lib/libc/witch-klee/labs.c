#include <limits.h>
#include <inttypes.h>

extern void __VERIFIER_error_overflow();

long labs(long x) {
    if (x == LONG_MIN)
        __VERIFIER_error_overflow();
    if (x >= 0)
        return x;
    return -x;
}


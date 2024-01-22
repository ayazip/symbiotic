#include <limits.h>
#include <inttypes.h>

extern void __VERIFIER_error_overflow();


int abs(int x) {
    if (x == INT_MIN)
        __VERIFIER_error_overflow();
    if (x >= 0)
        return x;
    return -x;
}

long labs(long x) {
    if (x == LONG_MIN)
        __VERIFIER_error_overflow();
    if (x >= 0)
        return x;
    return -x;
}

intmax_t imaxabs(intmax_t x) {
    if (x == INTMAX_MIN)
        __VERIFIER_error_overflow();
    if (x >= 0)
        return x;
    return -x;
}

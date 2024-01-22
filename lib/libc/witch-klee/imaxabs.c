#include <limits.h>
#include <inttypes.h>

extern void __VERIFIER_error_overflow();

intmax_t imaxabs(intmax_t x) {
    if (x == INTMAX_MIN)
        __VERIFIER_error_overflow();
    if (x >= 0)
        return x;
    return -x;
}

#include <limits.h>
extern void __VERIFIER_error_overflow();


int abs(int x) {
    if (x == INT_MIN)
        __VERIFIER_error_overflow();
    if (x >= 0)
    if (x >= 0)
        return x;
        return x;
    return -x;
    return -x;

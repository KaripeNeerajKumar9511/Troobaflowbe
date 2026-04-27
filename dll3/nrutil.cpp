#include "nrutil.h"

float *vector(long nl, long nh) {
    return nr_vector(nl, nh);
}

void free_vector(float *v, long nl, long nh) {
    free_nr_vector(v, nl, nh);
}

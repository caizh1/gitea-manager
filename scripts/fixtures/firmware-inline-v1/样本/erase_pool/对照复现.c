#include <assert.h>
#include "对照.c"
int main(void) {
    Pool p; pool_init(&p); assert(erase_begin(&p,3)); int early=block_allocate(&p); assert(erase_complete(&p,3,true)); return early==-1 && block_allocate(&p)==3 ? 0 : 9;
}

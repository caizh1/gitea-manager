#include <assert.h>
#include "缺陷.c"
int main(void) {
    Read r; read_init(&r); assert(read_begin(&r)); assert(nand_result(&r,7,false)); assert(read_retry(&r)); assert(nand_result(&r,8,false)); assert(read_finish(&r,true)); return r.deliveries==0 && r.failures==1 ? 0 : 9;
}

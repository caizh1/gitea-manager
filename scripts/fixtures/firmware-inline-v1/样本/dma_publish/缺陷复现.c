#include <assert.h>
#include "缺陷.c"
int main(void) {
    Dma d; dma_init(&d); assert(dma_submit(&d,4096,512)); dma_drain(&d); uint32_t a=0; unsigned n=0; assert(dma_last(&d,&a,&n)); return a==4096 && n==512 ? 0 : 9;
}

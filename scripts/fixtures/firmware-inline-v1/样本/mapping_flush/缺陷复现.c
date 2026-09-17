#include <assert.h>
#include "缺陷.c"
int main(void) {
    Media m; MapCache c; media_format(&m); cache_boot(&c,&m); assert(map_update(&c,2,99)); m.reject=true; assert(!cache_flush_one(&c,2)); m.reject=false; assert(cache_flush_all(&c)); cache_boot(&c,&m); uint32_t v=0; assert(map_lookup(&c,2,&v)); return v==99 ? 0 : 9;
}

#include <assert.h>
#include "对照.c"
int main(void) {
    Engine e; engine_init(&e); int old=request_allocate(&e,111,10); assert(old==0); assert(request_timeout(&e,0,11)); int fresh=request_allocate(&e,222,20); uint32_t value=0; bool ok=backend_read_buffer(&e,0,&value); return fresh!=0 && ok && value==111 ? 0 : 9;
}

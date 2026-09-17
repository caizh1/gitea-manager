#include <assert.h>
#include "对照.c"
int main(void) {
    MapTable t; table_init(&t); uint32_t v[]={101,102,103}; assert(table_update(&t,2,3,v)); return table_equal(&t,2,v,3) ? 0 : 9;
}

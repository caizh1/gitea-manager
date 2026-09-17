#include <assert.h>
#include "对照.c"
int main(void) {
    Frontend f; Command c; frontend_init(&f); command_init(&c,1024); assert(command_append(&c,4096,512)); bool accepted=execute_command(&f,&c); return !accepted && f.successes==0 ? 0 : 9;
}

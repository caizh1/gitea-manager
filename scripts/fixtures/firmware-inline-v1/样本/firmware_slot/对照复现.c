#include <assert.h>
#include "对照.c"
int main(void) {
    Firmware f; firmware_format(&f); assert(firmware_boot(&f)); assert(firmware_stage(&f,1,2,false)); assert(!firmware_verify(&f,1)); bool accepted=firmware_activate(&f,1); bool boot=firmware_boot(&f); return !accepted && boot && f.active==0 ? 0 : 9;
}

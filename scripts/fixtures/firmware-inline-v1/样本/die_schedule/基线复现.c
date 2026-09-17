#include <assert.h>
#include "基线.c"
int main(void) {
    Scheduler s; scheduler_init(&s); for(unsigned i=0;i<4;++i) assert(scheduler_issue(&s)>=0); return scheduler_max_depth(&s)==1 ? 0 : 9;
}

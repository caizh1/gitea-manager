#include <assert.h>
#include "基线.c"
int main(void) {
    Request r; request_init(&r); assert(request_split(&r,3)); assert(complete_child(&r,2,0)); assert(complete_child(&r,0,0)); unsigned before=r.host_notifications; assert(complete_child(&r,1,-7)); return before==0 && r.host_notifications==1 && r.result==-7 ? 0 : 9;
}

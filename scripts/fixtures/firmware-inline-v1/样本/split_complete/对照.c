#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>

/* 合成设备模型，无真实 MMIO 或 NAND 依赖。
 * 入口由单线程事件调度器串行调用；事件之间允许调度其他入口。
 * 日志仅作观测，容量耗尽不会改变设备行为。
 */
#define TRACE_CAP 64u
typedef struct {
    unsigned kind;
    unsigned object;
    uint32_t value;
} TraceEntry;
typedef struct {
    TraceEntry entries[TRACE_CAP];
    unsigned used;
    unsigned dropped;
} Trace;

void trace_reset(Trace *t)
{
    memset(t, 0, sizeof(*t));
}

void trace_emit(Trace *t, unsigned kind, unsigned object, uint32_t value)
{
    if (t->used == TRACE_CAP) {
        ++t->dropped;
        return;
    }
    t->entries[t->used++] = (TraceEntry){kind, object, value};
}

unsigned trace_count(const Trace *t, unsigned kind)
{
    unsigned count = 0;
    for (unsigned i = 0; i < t->used; ++i) {
        if (t->entries[i].kind == kind) {
            ++count;
        }
    }
    return count;
}

bool trace_last(const Trace *t, unsigned kind, TraceEntry *out)
{
    if (!out) {
        return false;
    }
    for (unsigned i = t->used; i > 0; --i) {
        if (t->entries[i - 1].kind == kind) {
            *out = t->entries[i - 1];
            return true;
        }
    }
    return false;
}

unsigned trace_copy(const Trace *t, TraceEntry *out, unsigned capacity)
{
    if (!out) {
        return 0;
    }
    unsigned n = t->used < capacity ? t->used : capacity;
    for (unsigned i = 0; i < n; ++i) {
        out[i] = t->entries[i];
    }
    return n;
}

unsigned trace_consume(Trace *t, TraceEntry *out, unsigned capacity)
{
    unsigned count = trace_copy(t, out, capacity);
    if (count == 0) {
        return 0;
    }
    t->used -= count;
    memmove(t->entries, t->entries + count, t->used * sizeof(t->entries[0]));
    return count;
}

unsigned trace_find_object(const Trace *t, unsigned object, TraceEntry *out, unsigned capacity)
{
    if (!out) {
        return 0;
    }
    unsigned written = 0;
    for (unsigned i = 0; i < t->used && written < capacity; ++i) {
        if (t->entries[i].object == object) {
            out[written++] = t->entries[i];
        }
    }
    return written;
}

/* 子请求完成可乱序；主机成功通知只能在全部子请求终结后发送一次。
 * 通知计数是主机观测到的完成消息数；并非内部进度通知。
 */
#define CHILDREN 8u
typedef struct {
    bool submitted;
    bool done;
    int status;
} Child;
typedef struct {
    Child children[CHILDREN];
    unsigned count;
    unsigned remaining;
    unsigned host_notifications;
    int result;
    bool finished;
    Trace trace;
} Request;

void request_init(Request *r)
{
    memset(r, 0, sizeof(*r));
}

static void host_complete(Request *r)
{
    ++r->host_notifications;
    r->finished = true;
    trace_emit(&r->trace, 30, 0, (uint32_t)r->result);
}

bool request_split(Request *r, unsigned count)
{
    if (count == 0 || count > CHILDREN || r->count != 0) {
        return false;
    }
    r->count = count;
    r->remaining = count;
    for (unsigned i = 0; i < count; ++i) {
        r->children[i].submitted = true;
    }
    return true;
}

bool complete_child(Request *r, unsigned id, int status)
{
    if (id >= r->count || !r->children[id].submitted || r->children[id].done) {
        return false;
    }
    Child *c = &r->children[id];
    c->done = true;
    c->status = status;
    if (status != 0 && r->result == 0) {
        r->result = status;
    }
    --r->remaining;
    bool finished = !r->remaining;
    if (finished && !r->finished) {
        host_complete(r);
    }
    trace_emit(&r->trace, 31, id, r->remaining);
    return true;
}

bool complete_empty(Request *r)
{
    if (r->count != 0 || r->finished) {
        return false;
    }
    host_complete(r);
    return true;
}

bool reject_before_submit(Request *r, int error)
{
    if (r->count != 0 || r->finished || error == 0) {
        return false;
    }
    r->result = error;
    host_complete(r);
    return true;
}

unsigned request_completed(const Request *r)
{
    unsigned n = 0;
    for (unsigned i = 0; i < r->count; ++i) {
        n += r->children[i].done ? 1u : 0u;
    }
    return n;
}

bool request_result(const Request *r, int *status)
{
    if (!status || !r->finished) {
        return false;
    }
    *status = r->result;
    return true;
}

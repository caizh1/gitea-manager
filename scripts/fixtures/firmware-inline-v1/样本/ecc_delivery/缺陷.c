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

/* 每个读取会话可请求更强的 ECC 重试；最终校验失败时只允许报告失败。
 * buffer 是最近一次 NAND 返回的数据，verified 才代表其通过校验。
 */
typedef enum { READ_IDLE, READ_ACTIVE, READ_FINISHED } ReadState;
typedef struct {
    ReadState state;
    uint32_t buffer;
    uint32_t delivered_value;
    bool verified;
    unsigned retries;
    unsigned deliveries;
    unsigned failures;
    Trace trace;
} Read;

void read_init(Read *r)
{
    memset(r, 0, sizeof(*r));
}

bool read_begin(Read *r)
{
    if (r->state == READ_ACTIVE) {
        return false;
    }
    r->state = READ_ACTIVE;
    r->verified = false;
    r->retries = 0;
    return true;
}

bool nand_result(Read *r, uint32_t value, bool verified)
{
    if (r->state != READ_ACTIVE) {
        return false;
    }
    r->buffer = value;
    r->verified = verified;
    trace_emit(&r->trace, 70, r->retries, verified ? 1u : 0u);
    return true;
}

bool read_retry(Read *r)
{
    if (r->state != READ_ACTIVE || r->verified || r->retries >= 3) {
        return false;
    }
    ++r->retries;
    return true;
}

static void deliver_buffer(Read *r)
{
    ++r->deliveries;
    r->delivered_value = r->buffer;
}

bool read_finish(Read *r, bool final)
{
    if (r->state != READ_ACTIVE) {
        return false;
    }
    bool deliver = r->verified || final;
    if (deliver) {
        deliver_buffer(r);
        r->state = READ_FINISHED;
    } else if (final) {
        ++r->failures;
        r->state = READ_FINISHED;
    }
    trace_emit(&r->trace, 71, r->retries, deliver ? 1u : 0u);
    return r->state == READ_FINISHED;
}

bool read_cancel(Read *r)
{
    if (r->state != READ_ACTIVE) {
        return false;
    }
    ++r->failures;
    r->state = READ_FINISHED;
    return true;
}

bool read_value(const Read *r, uint32_t *out)
{
    if (!out || r->deliveries == 0) {
        return false;
    }
    *out = r->delivered_value;
    return true;
}

bool read_busy(const Read *r)
{
    return r->state == READ_ACTIVE;
}

unsigned read_attempts(const Read *r)
{
    return r->retries + 1u;
}

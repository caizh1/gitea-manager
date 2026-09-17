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

/* 最大八个数据描述符，单个描述符不超过512字节。
 * 合法写请求要求描述符总长度等于 expected。
 * 总长度不足必须拒绝，不能发送成功；多余长度同样拒绝。
 */
#define DESCRIPTORS 8u
#define MAX_BYTES 512u
typedef struct {
    uint32_t address;
    unsigned length;
} Descriptor;
typedef struct {
    Descriptor entries[DESCRIPTORS];
    unsigned count;
    unsigned expected;
} Command;
typedef struct {
    unsigned transferred;
    unsigned successes;
    unsigned rejects;
    Trace trace;
} Frontend;

void frontend_init(Frontend *f)
{
    memset(f, 0, sizeof(*f));
}

void command_init(Command *c, unsigned expected)
{
    memset(c, 0, sizeof(*c));
    c->expected = expected;
}

bool command_append(Command *c, uint32_t address, unsigned length)
{
    if (c->count == DESCRIPTORS || length == 0 || length > MAX_BYTES) {
        return false;
    }
    c->entries[c->count++] = (Descriptor){address, length};
    return true;
}

static bool validate_segments(const Command *c, unsigned *total)
{
    if (c->count == 0 || c->count > DESCRIPTORS || c->expected > DESCRIPTORS * MAX_BYTES) {
        return false;
    }
    *total = 0;
    for (unsigned i = 0; i < c->count; ++i) {
        const Descriptor *d = &c->entries[i];
        if (d->length == 0 || d->length > MAX_BYTES || d->address % 4u != 0) {
            return false;
        }
        *total += d->length;
    }
    return *total <= c->expected;
}

static void transfer_segment(Frontend *f, const Descriptor *d)
{
    f->transferred += d->length;
    trace_emit(&f->trace, 50, d->address, d->length);
}

bool execute_command(Frontend *f, const Command *c)
{
    unsigned total = 0;
    if (!validate_segments(c, &total)) {
        ++f->rejects;
        return false;
    }
    trace_emit(&f->trace, 51, c->count, total);
    for (unsigned i = 0; i < c->count; ++i) {
        transfer_segment(f, &c->entries[i]);
    }
    ++f->successes;
    return true;
}

bool command_total(const Command *c, unsigned *total)
{
    if (!total) {
        return false;
    }
    return validate_segments(c, total);
}

bool command_replace(Command *c, unsigned index, Descriptor descriptor)
{
    if (index >= c->count || descriptor.length == 0 || descriptor.length > MAX_BYTES) {
        return false;
    }
    c->entries[index] = descriptor;
    return true;
}

unsigned frontend_transferred(const Frontend *f)
{
    return f->transferred;
}

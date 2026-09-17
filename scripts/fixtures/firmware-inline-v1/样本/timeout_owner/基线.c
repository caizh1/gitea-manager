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

/* 单线程事件循环；后台请求以 slot 索引访问上下文，持有引用直到完成回调。
 * 超时只结束主机等待，不撤销后端 DMA。不能在后台引用归零前复用 slot。
 */
#define REQUEST_SLOTS 4u
typedef struct {
    bool active;
    bool host_waiting;
    unsigned backend_refs;
    uint32_t buffer;
    uint32_t generation;
    uint32_t deadline;
} Slot;
typedef struct {
    Slot slots[REQUEST_SLOTS];
    unsigned timed_out;
    unsigned submitted;
    unsigned completed;
    Trace trace;
} Engine;

int request_allocate(Engine *e, uint32_t buffer, uint32_t deadline)
{
    for (unsigned i = 0; i < REQUEST_SLOTS; ++i) {
        Slot *s = &e->slots[i];
        if (!s->active) {
            if (s->generation == UINT32_MAX) {
                continue;
            }
            ++s->generation;
            s->active = true;
            s->host_waiting = true;
            s->backend_refs = 1;
            s->buffer = buffer;
            s->deadline = deadline;
            ++e->submitted;
            return (int)i;
        }
    }
    return -1;
}

void engine_init(Engine *e)
{
    memset(e, 0, sizeof(*e));
}

bool backend_read_buffer(const Engine *e, unsigned id, uint32_t *out)
{
    if (id >= REQUEST_SLOTS || !out) {
        return false;
    }
    const Slot *s = &e->slots[id];
    if (!s->active || s->backend_refs == 0) {
        return false;
    }
    *out = s->buffer;
    return true;
}

bool backend_complete(Engine *e, unsigned id)
{
    if (id >= REQUEST_SLOTS) {
        return false;
    }
    Slot *s = &e->slots[id];
    if (!s->active || s->backend_refs == 0) {
        return false;
    }
    --s->backend_refs;
    if (s->backend_refs == 0) {
        s->host_waiting = false;
        s->active = false;
        ++e->completed;
    }
    trace_emit(&e->trace, 40, id, s->generation);
    return true;
}

unsigned engine_active(const Engine *e)
{
    unsigned n = 0;
    for (unsigned i = 0; i < REQUEST_SLOTS; ++i) {
        if (e->slots[i].active) {
            ++n;
        }
    }
    return n;
}

unsigned engine_waiting(const Engine *e)
{
    unsigned n = 0;
    for (unsigned i = 0; i < REQUEST_SLOTS; ++i) {
        if (e->slots[i].host_waiting) {
            ++n;
        }
    }
    return n;
}

bool engine_quiescent(const Engine *e)
{
    for (unsigned i = 0; i < REQUEST_SLOTS; ++i) {
        if (e->slots[i].backend_refs != 0) {
            return false;
        }
    }
    return true;
}

bool request_extend(Engine *e, unsigned id, uint32_t deadline)
{
    if (id >= REQUEST_SLOTS) {
        return false;
    }
    Slot *s = &e->slots[id];
    if (!s->active || !s->host_waiting || deadline < s->deadline) {
        return false;
    }
    s->deadline = deadline;
    return true;
}

bool request_pending(const Engine *e, unsigned id)
{
    if (id >= REQUEST_SLOTS) {
        return false;
    }
    return e->slots[id].host_waiting;
}

bool request_identity(const Engine *e, unsigned id, uint32_t *generation)
{
    if (id >= REQUEST_SLOTS || !generation || !e->slots[id].active) {
        return false;
    }
    *generation = e->slots[id].generation;
    return true;
}

bool request_expired(const Engine *e, unsigned id, uint32_t now)
{
    if (id >= REQUEST_SLOTS) {
        return false;
    }
    const Slot *s = &e->slots[id];
    return s->host_waiting && now >= s->deadline;
}

unsigned engine_snapshot(const Engine *e, Slot *out, unsigned capacity)
{
    if (!out) {
        return 0;
    }
    unsigned n = capacity < REQUEST_SLOTS ? capacity : REQUEST_SLOTS;
    for (unsigned i = 0; i < n; ++i) {
        out[i] = e->slots[i];
    }
    return n;
}

unsigned engine_backend_refs(const Engine *e)
{
    unsigned n = 0;
    for (unsigned i = 0; i < REQUEST_SLOTS; ++i) {
        n += e->slots[i].backend_refs;
    }
    return n;
}

bool engine_next_deadline(const Engine *e, uint32_t *deadline)
{
    if (!deadline) {
        return false;
    }
    bool found = false;
    uint32_t earliest = UINT32_MAX;
    for (unsigned i = 0; i < REQUEST_SLOTS; ++i) {
        const Slot *s = &e->slots[i];
        if (s->host_waiting && (!found || s->deadline < earliest)) {
            found = true;
            earliest = s->deadline;
        }
    }
    if (found) {
        *deadline = earliest;
    }
    return found;
}

unsigned engine_active_buffers(const Engine *e, uint32_t *out, unsigned capacity)
{
    if (!out) {
        return 0;
    }
    unsigned n = 0;
    for (unsigned i = 0; i < REQUEST_SLOTS; ++i) {
        if (e->slots[i].active && n < capacity) {
            out[n++] = e->slots[i].buffer;
        }
    }
    return n;
}

bool engine_clear_statistics(Engine *e)
{
    if (!engine_quiescent(e)) {
        return false;
    }
    e->timed_out = 0;
    e->submitted = 0;
    e->completed = 0;
    trace_reset(&e->trace);
    return true;
}

bool request_timeout(Engine *e, unsigned id, uint32_t now)
{
    if (!request_expired(e, id, now)) {
        return false;
    }
    Slot *s = &e->slots[id];
    s->host_waiting = false;
    s->active = s->backend_refs != 0;
    ++e->timed_out;
    trace_emit(&e->trace, 41, id, s->generation);
    return true;
}

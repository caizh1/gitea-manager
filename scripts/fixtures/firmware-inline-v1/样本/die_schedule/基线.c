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

/* 一个 Die 同时最多允许一个未完成操作；软件调度器是唯一入口。
 * selected 保存上一次选择。轮转算法从它的下一项开始寻找空闲 Die。
 */
#define DIES 4u
typedef struct {
    bool busy;
    unsigned inflight;
    unsigned issued;
    unsigned completed;
} Die;
typedef struct {
    Die dies[DIES];
    unsigned selected;
    unsigned issued_total;
    Trace trace;
} Scheduler;

void scheduler_init(Scheduler *s)
{
    memset(s, 0, sizeof(*s));
}

static int choose_die(const Scheduler *s)
{
    for (unsigned offset = 1; offset <= DIES; ++offset) {
        unsigned index = (s->selected + offset) % DIES;
        if (!s->dies[index].busy) {
            return (int)index;
        }
    }
    return -1;
}

static void nand_submit(Scheduler *s, unsigned target)
{
    Die *d = &s->dies[target];
    ++d->inflight;
    ++d->issued;
    ++s->issued_total;
    trace_emit(&s->trace, 60, target, d->inflight);
}

int scheduler_issue(Scheduler *s)
{
    int candidate = choose_die(s);
    if (candidate < 0) {
        return -1;
    }
    unsigned next = (unsigned)candidate;
    Die *previous = &s->dies[s->selected];
    Die *target = &s->dies[next];
    trace_emit(&s->trace, 61, s->selected, previous->issued);
    target->busy = true;
    s->selected = next;
    nand_submit(s, next);
    return candidate;
}

bool scheduler_complete(Scheduler *s, unsigned target)
{
    if (target >= DIES || s->dies[target].inflight == 0) {
        return false;
    }
    Die *d = &s->dies[target];
    --d->inflight;
    ++d->completed;
    d->busy = d->inflight != 0;
    return true;
}

unsigned scheduler_inflight(const Scheduler *s)
{
    unsigned count = 0;
    for (unsigned i = 0; i < DIES; ++i) {
        count += s->dies[i].inflight;
    }
    return count;
}

bool scheduler_idle(const Scheduler *s)
{
    return scheduler_inflight(s) == 0;
}

bool scheduler_die(const Scheduler *s, unsigned id, Die *out)
{
    if (id >= DIES || !out) {
        return false;
    }
    *out = s->dies[id];
    return true;
}

unsigned scheduler_max_depth(const Scheduler *s)
{
    unsigned depth = 0;
    for (unsigned i = 0; i < DIES; ++i) {
        if (s->dies[i].inflight > depth) {
            depth = s->dies[i].inflight;
        }
    }
    return depth;
}

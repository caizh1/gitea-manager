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

/* 擦除请求与完成回调是两个事件。两者之间允许主机分配块。
 * 原始 NAND 内容仅在完成回调成功时变成全一；分配器信任可用队列。
 */
#define BLOCKS 8u
typedef struct {
    bool erased;
    bool erasing;
    bool free;
    bool allocated;
    unsigned erase_count;
} Block;
typedef struct {
    Block blocks[BLOCKS];
    Trace trace;
} Pool;

void pool_init(Pool *p)
{
    memset(p, 0, sizeof(*p));
}

bool pool_seed(Pool *p, unsigned id)
{
    if (id >= BLOCKS || p->blocks[id].allocated || p->blocks[id].erasing) {
        return false;
    }
    p->blocks[id].erased = true;
    p->blocks[id].free = true;
    return true;
}

bool erase_begin(Pool *p, unsigned id)
{
    if (id >= BLOCKS) {
        return false;
    }
    Block *b = &p->blocks[id];
    if (b->allocated || b->erasing || b->free) {
        return false;
    }
    b->erasing = true;
    b->free = false;
    trace_emit(&p->trace, 10, id, b->erase_count);
    return true;
}

bool erase_complete(Pool *p, unsigned id, bool success)
{
    if (id >= BLOCKS || !p->blocks[id].erasing) {
        return false;
    }
    Block *b = &p->blocks[id];
    b->erasing = false;
    if (success) {
        b->erased = true;
        ++b->erase_count;
        b->free = !b->allocated;
    }
    trace_emit(&p->trace, 11, id, success ? 1u : 0u);
    return true;
}

int block_allocate(Pool *p)
{
    for (unsigned i = 0; i < BLOCKS; ++i) {
        Block *b = &p->blocks[i];
        if (b->free) {
            b->free = false;
            b->allocated = true;
            trace_emit(&p->trace, 12, i, 0);
            return (int)i;
        }
    }
    return -1;
}

bool block_program(Pool *p, unsigned id)
{
    if (id >= BLOCKS) {
        return false;
    }
    Block *b = &p->blocks[id];
    if (!b->allocated || !b->erased || b->erasing) {
        return false;
    }
    b->erased = false;
    return true;
}

bool block_retire(Pool *p, unsigned id)
{
    if (id >= BLOCKS || !p->blocks[id].allocated) {
        return false;
    }
    p->blocks[id].allocated = false;
    return true;
}

unsigned pool_available(const Pool *p)
{
    unsigned count = 0;
    for (unsigned i = 0; i < BLOCKS; ++i) {
        count += p->blocks[i].free ? 1u : 0u;
    }
    return count;
}

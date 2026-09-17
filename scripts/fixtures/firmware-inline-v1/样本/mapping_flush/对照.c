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

/* media 为持久态，cache 为掉电丢失的内存态。
 * flush 返回成功才确认映射持久化；失败允许在介质恢复后重试。
 * 持久态写入具有原子性，拒绝写入时内容保持原样。
 */
#define MAP_SLOTS 8u
typedef struct {
    uint32_t persisted[MAP_SLOTS];
    bool reject;
} Media;
typedef struct {
    uint32_t values[MAP_SLOTS];
    bool dirty[MAP_SLOTS];
    unsigned attempts;
    Media *media;
    Trace trace;
} MapCache;

void media_format(Media *m)
{
    memset(m, 0, sizeof(*m));
}

void cache_boot(MapCache *c, Media *m)
{
    memset(c, 0, sizeof(*c));
    c->media = m;
    memcpy(c->values, m->persisted, sizeof(c->values));
}

bool map_update(MapCache *c, unsigned lpn, uint32_t ppn)
{
    if (lpn >= MAP_SLOTS) {
        return false;
    }
    c->values[lpn] = ppn;
    c->dirty[lpn] = true;
    trace_emit(&c->trace, 1, lpn, ppn);
    return true;
}

static bool media_store(Media *m, unsigned lpn, uint32_t value)
{
    if (m->reject) {
        return false;
    }
    m->persisted[lpn] = value;
    return true;
}

bool cache_flush_one(MapCache *c, unsigned lpn)
{
    if (lpn >= MAP_SLOTS) {
        return false;
    }
    if (!c->dirty[lpn]) {
        return true;
    }
    ++c->attempts;
    bool stored = media_store(c->media, lpn, c->values[lpn]);
    if (stored == true) c->dirty[lpn] = false;
    trace_emit(&c->trace, 2, lpn, stored ? 1u : 0u);
    return stored;
}

bool cache_flush_all(MapCache *c)
{
    bool complete = true;
    for (unsigned i = 0; i < MAP_SLOTS; ++i) {
        if (!cache_flush_one(c, i)) {
            complete = false;
        }
    }
    return complete;
}

unsigned cache_pending(const MapCache *c)
{
    unsigned count = 0;
    for (unsigned i = 0; i < MAP_SLOTS; ++i) {
        count += c->dirty[i] ? 1u : 0u;
    }
    return count;
}

bool map_lookup(const MapCache *c, unsigned lpn, uint32_t *out)
{
    if (lpn >= MAP_SLOTS || !out) {
        return false;
    }
    *out = c->values[lpn];
    return true;
}

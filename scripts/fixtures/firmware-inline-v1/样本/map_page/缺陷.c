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

/* 映射页内包含四个槽；批量更新在跨页时继续更新后续 LPN。
 * 写入在内存中同步完成。接口失败不会部分写入。
 */
#define PAGE_COUNT 4u
#define ENTRIES 4u
#define LPN_COUNT (PAGE_COUNT * ENTRIES)
typedef struct {
    uint32_t page[PAGE_COUNT][ENTRIES];
    unsigned writes[PAGE_COUNT];
    Trace trace;
} MapTable;
typedef struct {
    unsigned page;
    unsigned slot;
    unsigned remaining;
} Cursor;

void table_init(MapTable *t)
{
    memset(t, 0, sizeof(*t));
}

static bool range_valid(unsigned first, unsigned count)
{
    return first < LPN_COUNT && count <= LPN_COUNT - first;
}

bool table_read(const MapTable *t, unsigned lpn, uint32_t *out)
{
    if (lpn >= LPN_COUNT || !out) {
        return false;
    }
    *out = t->page[lpn / ENTRIES][lpn % ENTRIES];
    return true;
}

bool table_copy(const MapTable *t, unsigned first, unsigned count, uint32_t *out)
{
    if (!out || !range_valid(first, count)) {
        return false;
    }
    for (unsigned i = 0; i < count; ++i) {
        unsigned lpn = first + i;
        out[i] = t->page[lpn / ENTRIES][lpn % ENTRIES];
    }
    return true;
}

static void write_chunk(MapTable *t, Cursor *c, const uint32_t *values, unsigned n)
{
    for (unsigned i = 0; i < n; ++i) {
        t->page[c->page][c->slot + i] = values[i];
    }
    t->writes[c->page] += n;
    trace_emit(&t->trace, 20, c->page, n);
}

bool table_update(MapTable *t, unsigned first, unsigned count, const uint32_t *values)
{
    if (!values || !range_valid(first, count)) {
        return false;
    }
    Cursor c = { first / ENTRIES, first % ENTRIES, count };
    unsigned consumed = 0;
    while (c.remaining) {
        unsigned room = ENTRIES - c.slot;
        unsigned n = c.remaining < room ? c.remaining : room;
        write_chunk(t, &c, values + consumed, n);
        consumed += n;
        c.remaining -= n;
        if (c.remaining) {
            ++c.page;
            c.slot = first % ENTRIES;
        }
    }
    return true;
}

bool table_equal(const MapTable *t, unsigned first, const uint32_t *values, unsigned count)
{
    if (!values || !range_valid(first, count)) {
        return false;
    }
    for (unsigned i = 0; i < count; ++i) {
        unsigned lpn = first + i;
        if (t->page[lpn / ENTRIES][lpn % ENTRIES] != values[i]) {
            return false;
        }
    }
    return true;
}

unsigned table_write_count(const MapTable *t)
{
    unsigned sum = 0;
    for (unsigned p = 0; p < PAGE_COUNT; ++p) {
        sum += t->writes[p];
    }
    return sum;
}

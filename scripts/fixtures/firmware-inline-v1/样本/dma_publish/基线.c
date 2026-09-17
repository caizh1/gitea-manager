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

/* 模拟独立 DMA 消费者；每次 device_step 都可消费 visible 描述符。
 * device_step 是调度点，非内存屏障；不依赖真实 CPU 重排。
 * 调用提交前须确认该槽没有在途描述符，完成后可以再次使用。
 */
#define RING_SIZE 4u
typedef struct {
    uint32_t address;
    unsigned length;
    bool visible;
} Descriptor;
typedef struct {
    Descriptor ring[RING_SIZE];
    unsigned producer;
    unsigned consumer;
    uint32_t observed_address;
    unsigned observed_length;
    unsigned completed;
    Trace trace;
} Dma;

void dma_init(Dma *d)
{
    memset(d, 0, sizeof(*d));
}

void device_step(Dma *d)
{
    Descriptor *slot = &d->ring[d->consumer];
    if (!slot->visible) {
        return;
    }
    d->observed_address = slot->address;
    d->observed_length = slot->length;
    slot->visible = false;
    ++d->completed;
    trace_emit(&d->trace, 80, d->consumer, slot->length);
    d->consumer = (d->consumer + 1u) % RING_SIZE;
}

bool dma_submit(Dma *d, uint32_t address, unsigned length)
{
    Descriptor *slot = &d->ring[d->producer];
    if (slot->visible || length == 0 || length > 4096 || address % 4u != 0) {
        return false;
    }
    slot->address = address;
    device_step(d);
    slot->length = length;
    slot->visible = true;
    d->producer = (d->producer + 1u) % RING_SIZE;
    return true;
}

unsigned dma_drain(Dma *d)
{
    unsigned before = d->completed;
    for (unsigned i = 0; i < RING_SIZE; ++i) {
        device_step(d);
    }
    return d->completed - before;
}

unsigned dma_pending(const Dma *d)
{
    unsigned count = 0;
    for (unsigned i = 0; i < RING_SIZE; ++i) {
        count += d->ring[i].visible ? 1u : 0u;
    }
    return count;
}

bool dma_last(const Dma *d, uint32_t *address, unsigned *length)
{
    if (!address || !length || d->completed == 0) {
        return false;
    }
    *address = d->observed_address;
    *length = d->observed_length;
    return true;
}

bool dma_slot(const Dma *d, unsigned index, Descriptor *out)
{
    if (!out || index >= RING_SIZE) {
        return false;
    }
    *out = d->ring[index];
    return true;
}

bool dma_idle(const Dma *d)
{
    return dma_pending(d) == 0;
}

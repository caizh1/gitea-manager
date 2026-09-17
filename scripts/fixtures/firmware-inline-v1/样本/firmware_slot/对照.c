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

/* 双槽镜像；active 是掉电保留的启动选择。running 在重启时更新。
 * 镜像校验由受控介质模型返回，失败槽不允许成为启动目标。
 * stage 写入非活动槽；activate 允许在校验结束后调用并报告成功与否。
 */
typedef struct {
    uint32_t version;
    bool written;
    bool digest_ok;
    bool checked;
} Image;
typedef struct {
    Image images[2];
    unsigned active;
    unsigned running;
    unsigned boot_failures;
    Trace trace;
} Firmware;

void firmware_format(Firmware *f)
{
    memset(f, 0, sizeof(*f));
    f->images[0] = (Image){1, true, true, true};
}

bool firmware_boot(Firmware *f)
{
    const Image *image = &f->images[f->active];
    if (!image->written || !image->checked || !image->digest_ok) {
        ++f->boot_failures;
        return false;
    }
    f->running = f->active;
    return true;
}

bool firmware_stage(Firmware *f, unsigned slot, uint32_t version, bool digest_ok)
{
    if (slot >= 2 || slot == f->active || version == 0) {
        return false;
    }
    f->images[slot] = (Image){version, true, digest_ok, false};
    trace_emit(&f->trace, 90, slot, version);
    return true;
}

bool firmware_verify(Firmware *f, unsigned slot)
{
    if (slot >= 2 || !f->images[slot].written) {
        return false;
    }
    f->images[slot].checked = true;
    trace_emit(&f->trace, 91, slot, f->images[slot].digest_ok ? 1u : 0u);
    return f->images[slot].digest_ok;
}

bool firmware_query(const Firmware *f, unsigned slot, Image *out)
{
    if (slot >= 2 || !out) {
        return false;
    }
    *out = f->images[slot];
    return true;
}

bool firmware_discard(Firmware *f, unsigned slot)
{
    if (slot >= 2 || slot == f->active || slot == f->running) {
        return false;
    }
    memset(&f->images[slot], 0, sizeof(f->images[slot]));
    return true;
}

uint32_t firmware_running_version(const Firmware *f)
{
    return f->images[f->running].version;
}

unsigned firmware_ready_slots(const Firmware *f)
{
    unsigned count = 0;
    for (unsigned i = 0; i < 2; ++i) {
        const Image *image = &f->images[i];
        if (image->written && image->checked && image->digest_ok) {
            ++count;
        }
    }
    return count;
}

bool firmware_activate(Firmware *f, unsigned slot)
{
    if (slot >= 2 || slot == f->active) {
        return false;
    }
    const Image *image = &f->images[slot];
    bool ready = image->written &&
                 image->checked &&
                 (image->digest_ok == true);
    if (!ready) {
        return false;
    }
    f->active = slot;
    trace_emit(&f->trace, 92, slot, image->version);
    return true;
}
"""50 个固定合成 PR 样本：25 类缺陷及对应正常对照，不包含公司源码。"""
COMMON = '#include <stdlib.h>\n#include <stdint.h>\n#include <stddef.h>\n#include <stdio.h>\n#include <string.h>\n#include <limits.h>\n#include <pthread.h>\n#include <errno.h>\n#include <unistd.h>\n#include <fcntl.h>\n\n'

# 每项给出独立函数、错误表达式、正确表达式和允许讨论的真实问题行。
SPECS = [
 ('数组边界', 'c', 'int lookup(const int *p, size_t n, size_t i) {\n    if (!p || i > n) return -1;\n    return p[i];\n}\n', 'i > n', 'i >= n', ['i > n', 'return p[i]']),
 ('空指针检查顺序', 'c', 'int read_value(const int *p) {\n    int value = *p;\n    if (!p) return -1;\n    return value;\n}\n', '    int value = *p;\n    if (!p) return -1;', '    if (!p) return -1;\n    int value = *p;', ['int value = *p']),
 ('分配失败', 'c', 'int allocate_value(void) {\n    int *p = malloc(sizeof(*p));\n    *p = 7;\n    int value = *p;\n    free(p);\n    return value;\n}\n', '    *p = 7;', '    if (!p) return -1;\n    *p = 7;', ['int *p = malloc', '*p = 7']),
 ('释放后读取', 'c', 'int read_after_release(void) {\n    int *p = malloc(sizeof(*p));\n    if (!p) return -1;\n    *p = 7;\n    free(p);\n    return *p;\n}\n', '    free(p);\n    return *p;', '    int value = *p;\n    free(p);\n    return value;', ['return *p', 'free(p)']),
 ('重复释放', 'c', 'void release_twice(void) {\n    void *p = malloc(8);\n    if (!p) return;\n    free(p);\n    free(p);\n}\n', '    free(p);\n    free(p);', '    free(p);', ['free(p)']),
 ('错误路径泄漏', 'c', 'int process_buffer(int ready) {\n    void *p = malloc(64);\n    if (!p) return -1;\n    if (!ready) return -2;\n    memset(p, 0, 64);\n    free(p);\n    return 0;\n}\n', '    if (!ready) return -2;', '    if (!ready) { free(p); return -2; }', ['if (!ready) return']),
 ('realloc 丢失原指针', 'c', 'void *resize_owned(void *p, size_t n) {\n    if (!n) { free(p); return NULL; }\n    p = realloc(p, n);\n    if (!p) return NULL;\n    return p;\n}\n', '    p = realloc(p, n);\n    if (!p) return NULL;\n    return p;', '    void *next = realloc(p, n);\n    if (!next) { free(p); return NULL; }\n    return next;', ['p = realloc']),
 ('复制容量单位', 'c', '/* dst 和 src 均至少有 8 个有效 uint32_t 元素。 */\nint copy_words(uint32_t *dst, const uint32_t *src, size_t bytes) {\n    if (bytes > 8 * sizeof(uint32_t)) return -1;\n    memcpy(dst, src, bytes * sizeof(uint32_t));\n    return 0;\n}\n', 'bytes * sizeof(uint32_t)', 'bytes', ['memcpy']),
 ('字符串缺少终止符', 'c', '/* input 指向以零终止的字符串，长度不受限制。 */\nvoid print_name(const char *input) {\n    char name[16];\n    strncpy(name, input, sizeof(name));\n    printf("%s", name);\n}\n', '    printf("%s", name);', '    name[sizeof(name)-1] = 0;\n    printf("%s", name);', ['strncpy', 'printf']),
 ('snprintf 返回值', 'c', '/* name 为有效字符串，fd 为调用方提供的文件描述符。 */\nint send_name(int fd, const char *name) {\n    char output[16];\n    int n = snprintf(output, sizeof(output), "%s", name);\n    if (n < 0) return -1;\n    return (int)write(fd, output, (size_t)n);\n}\n', '    if (n < 0) return -1;', '    if (n < 0 || (size_t)n >= sizeof(output)) return -1;', ['if (n < 0)', 'write(fd']),
 ('格式字符串', 'c', '/* text 为外部输入的有效零终止字符串。 */\nvoid print_input(const char *text) {\n    printf(text);\n}\n', 'printf(text)', 'printf("%s", text)', ['printf(text)']),
 ('负长度转换', 'c', '/* dst 和 src 至少各有 64 个有效字节。 */\nint copy_count(char *dst, const char *src, int count) {\n    if (count > 64) return -1;\n    memcpy(dst, src, (size_t)count);\n    return 0;\n}\n', 'count > 64', 'count < 0 || count > 64', ['if (count > 64)', 'memcpy']),
 ('分配大小截断', 'c', 'uint32_t *allocate_words(uint32_t count) {\n    if ((uint64_t)count > SIZE_MAX / sizeof(uint32_t)) return NULL;\n    uint32_t bytes = (size_t)count * sizeof(uint32_t);\n    uint32_t *p = malloc(bytes);\n    if (!p) return NULL;\n    for (uint32_t i = 0; i < count; ++i) p[i] = 0;\n    return p;\n}\n', 'uint32_t bytes =', 'size_t bytes =', ['uint32_t bytes =', 'p[i] = 0']),
 ('无符号下溢', 'c', '/* p 在 n>0 时指向至少 n 个元素。 */\nvoid clear_prefix(int *p, size_t n) {\n    for (size_t i = 0; i < n - 1; ++i) p[i] = 0;\n}\n', 'i < n - 1', 'n > 0 && i < n - 1', ['n - 1']),
 ('移位宽度', 'c', 'uint32_t bit_mask(unsigned bit) {\n    return UINT32_C(1) << bit;\n}\n', '    return UINT32_C(1) << bit;', '    if (bit >= 32) return 0;\n    return UINT32_C(1) << bit;', ['<< bit']),
 ('负数左移', 'c', 'int double_value(int value) {\n    if (value > INT_MAX / 2 || value < INT_MIN / 2) return 0;\n    return value << 1;\n}\n', 'value << 1', 'value * 2', ['value << 1']),
 ('错误路径未解锁', 'c', 'int locked_work(pthread_mutex_t *lock, int ready) {\n    int rc = pthread_mutex_lock(lock);\n    if (rc) return rc;\n    if (!ready) return EAGAIN;\n    pthread_mutex_unlock(lock);\n    return 0;\n}\n', 'if (!ready) return EAGAIN;', 'if (!ready) { pthread_mutex_unlock(lock); return EAGAIN; }', ['if (!ready) return']),
 ('线程 API 错误码', 'c', 'int start_thread(pthread_t *thread, void *(*entry)(void *), void *arg) {\n    int rc = pthread_create(thread, NULL, entry, arg);\n    return rc < 0 ? -1 : 0;\n}\n', 'rc < 0', 'rc != 0', ['rc < 0']),
 ('文件描述符判断', 'c', 'int open_ok(const char *path) {\n    int fd = open(path, O_RDONLY);\n    if (fd) { close(fd); return 1; }\n    return 0;\n}\n', 'if (fd)', 'if (fd >= 0)', ['if (fd)']),
 ('read 错误值转换', 'c', 'int read_block(int fd) {\n    char buf[65];\n    size_t n = read(fd, buf, 64);\n    if (n < 0) return -1;\n    buf[n] = 0;\n    return 0;\n}\n', 'size_t n = read', 'ssize_t n = read', ['size_t n', 'if (n < 0)', 'buf[n]']),
 ('未对齐嵌入式读取', 'c', '/* 目标为严格对齐的 ARMv7；data 至少有 5 字节，但只保证字节对齐。返回本机字节序值。 */\nuint32_t load_word(const unsigned char *data) {\n    return *(const uint32_t *)(data + 1);\n}\n', '    return *(const uint32_t *)(data + 1);', '    uint32_t value;\n    memcpy(&value, data + 1, sizeof(value));\n    return value;', ['uint32_t *)(data + 1)']),
 ('硬件轮询可见性', 'c', '/* 单核裸机：status 指向合法 MMIO 寄存器，由硬件异步更新；不是普通共享内存。 */\nvoid wait_ready(uint32_t *status) {\n    while ((*status & 1u) == 0) { }\n}\n', 'wait_ready(uint32_t *status)', 'wait_ready(volatile uint32_t *status)', ['wait_ready(uint32_t', 'while']),
 ('返回悬空引用', 'cpp', '#include <string>\nconst std::string &make_name() {\n    std::string name = "device";\n    return name;\n}\n', 'const std::string &make_name()', 'std::string make_name()', ['return name', '&make_name']),
 ('迭代器失效', 'cpp', '#include <vector>\nvoid remove_zero(std::vector<int> &values) {\n    for (auto it = values.begin(); it != values.end(); ++it) {\n        if (*it == 0) values.erase(it);\n    }\n}\n', '    for (auto it = values.begin(); it != values.end(); ++it) {\n        if (*it == 0) values.erase(it);', '    for (auto it = values.begin(); it != values.end();) {\n        if (*it == 0) it = values.erase(it);\n        else ++it;', ['values.erase', '++it']),
 ('数组释放方式', 'cpp', 'void release_array() {\n    int *p = new int[8];\n    p[0] = 1;\n    delete p;\n}\n', 'delete p', 'delete[] p', ['delete p']),
]


def cases():
    result=[]
    for index,(title,language,source,bad,good,positions) in enumerate(SPECS,1):
        for faulty in (True,False):
            contract = '/* 函数接管 p 的所有权，无论成功或失败，调用者均不再释放旧指针。 */\n' if title == 'realloc 丢失原指针' else ''
            code=COMMON+contract+(source if faulty else source.replace(bad,good,1))
            number=len(result)+1
            allowed=[i for i,line in enumerate(code.splitlines(),1) if any(p in line for p in positions)] if faulty else []
            result.append({'id':f'case-{number:02d}','名称':title+('缺陷' if faulty else '正常对照'),
                           'path':f'case_{number:02d}.{language}','code':code,'存在缺陷':faulty,'允许问题行':allowed})
    return result

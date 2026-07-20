/*
 * fsdb_wrapper.cpp
 * C++ wrapper for Verdi ffrAPI → exposes C interface for Python ctypes
 *
 * Build:
 *   g++ -shared -fPIC -o libfsdb_wrapper.so fsdb_wrapper.cpp \
 *       -I$VERDI_HOME/share/FsdbReader \
 *       -L$VERDI_HOME/share/FsdbReader/linux64 \
 *       -lnffr -lnsys -lz \
 *       -Wl,-rpath,$VERDI_HOME/share/FsdbReader/linux64
 */

#ifdef NOVAS_FSDB
#undef NOVAS_FSDB
#endif

#include "ffrAPI.h"
#include <chrono>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef FALSE
#define FALSE 0
#endif
#ifndef TRUE
#define TRUE 1
#endif
#include <map>
#include <set>
#include <string>
#include <vector>

/* ─── 内部数据结构 ─────────────────────────────────────────────────── */

struct SigInfo {
    fsdbVarIdcode idcode;
    uint_T        bit_size;
    uint_T        bytes_per_bit;
    uint_T        direction;   /* fsdbVarDir: 0=IMPLICIT 1=INPUT 2=OUTPUT 3=INOUT 4=BUFFER 5=LINKAGE */
    uint_T        var_type;    /* fsdbVarType: 2=PARAMETER 3=REAL 4=REG 15=WIRE 17=MEMORY ... */
    std::string   full_path;   /* top_tb.dut.s_bits */
};

struct FsdbCtx {
    ffrObject                        *obj;
    std::map<std::string, SigInfo>    path_to_sig;   /* full_path → SigInfo */
    std::map<fsdbVarIdcode, SigInfo*> id_to_sig;
    std::string                       scope_stack;   /* 当前遍历路径 */
    std::vector<std::string>          scope_parts;
    bool                              tree_done;
    /* FSDB 时间刻度。FSDB tag 存的是 tick 计数而非 ps：
     *   真实时间 = tick × scale
     * scale 来自文件头 ffrGetScaleUnit()（如 "100fs"/"1ps"/"1ns"）。
     * 换算系数以 fs 整数存（fs/tick），sub-ps 刻度（100fs = 0.1ps）不会像
     * 整数 ps 那样被截成 0。
     * 0 = 刻度读不到/无法解析——所有时间型入口拒绝服务（返回
     * FSDB_ERR_SCALE_UNKNOWN），绝不静默假设 1 tick == 1 ps。 */
    unsigned long long                scale_fs;
    char                              scale_unit[32]; /* 原始刻度字符串，"" = 未知 */
    bool                              transition_group_active;
    std::set<fsdbVarIdcode>           transition_group_ids;
};

/* Optional profiling receipt for transition reads. All fields are numeric and
 * contain no path/value identity, so Python may safely aggregate them into
 * operation telemetry. Durations use steady-clock nanoseconds. Keep this ABI
 * append-only; Python checks symbol presence and falls back to the legacy API
 * when an older wrapper is installed. */
struct FsdbTransitionProfileV1 {
    unsigned long long lookup_ns;
    unsigned long long add_signal_ns;
    unsigned long long load_ns;
    unsigned long long create_handle_ns;
    unsigned long long seek_ns;
    unsigned long long traverse_format_ns;
    unsigned long long free_handle_ns;
    unsigned long long unload_ns;
    unsigned long long transition_count;
    unsigned long long output_bytes;
    int                truncated;
};

struct FsdbTransitionGroupProfileV1 {
    unsigned long long lookup_ns;
    unsigned long long add_signal_ns;
    unsigned long long load_ns;
    unsigned long long unload_ns;
    unsigned long long signal_count;
};

typedef std::chrono::steady_clock _ProfileClock;

static unsigned long long
_ElapsedNs(_ProfileClock::time_point begin, _ProfileClock::time_point end)
{
    return (unsigned long long)
        std::chrono::duration_cast<std::chrono::nanoseconds>(end - begin).count();
}

/* 时间刻度未知时所有时间型接口的错误码（-1 参数、-2 信号未找到、-3 句柄失败已占用） */
#define FSDB_ERR_SCALE_UNKNOWN (-4)
#define FS_PER_PS 1000ULL

/* 解析 ffrGetScaleUnit() 返回的刻度字符串（"100fs"/"1ps"/"1ns"…）为
 * fs/tick。无数字前缀按 1 处理（"ps" == "1ps"）；单位不认识或结果非正
 * 返回 0（未知）。 */
static unsigned long long
_ParseScaleFs(const char *scale_unit)
{
    if (!scale_unit || !*scale_unit) return 0;

    char *endp = NULL;
    double num = strtod(scale_unit, &endp);
    if (endp == scale_unit) num = 1.0;        /* 无数字前缀 */
    while (*endp == ' ' || *endp == '\t') endp++;

    char unit[8];
    size_t n = 0;
    for (; endp[n] && n + 1 < sizeof(unit); n++)
        unit[n] = (char)tolower((unsigned char)endp[n]);
    unit[n] = '\0';

    double mult;
    if      (0 == strcmp(unit, "fs")) mult = 1.0;
    else if (0 == strcmp(unit, "ps")) mult = 1e3;
    else if (0 == strcmp(unit, "ns")) mult = 1e6;
    else if (0 == strcmp(unit, "us")) mult = 1e9;
    else if (0 == strcmp(unit, "ms")) mult = 1e12;
    else if (0 == strcmp(unit, "s"))  mult = 1e15;
    else return 0;

    double fs = num * mult;
    if (fs < 1.0 || fs > 9e18) return 0;
    return (unsigned long long)(fs + 0.5);
}

/* ─── 树回调：建立信号路径索引 ────────────────────────────────────── */

static bool_T
_TreeCB(fsdbTreeCBType cb_type, void *client_data, void *tree_cb_data)
{
    FsdbCtx *ctx = (FsdbCtx*)client_data;

    switch (cb_type) {
    case FSDB_TREE_CBT_SCOPE: {
        fsdbTreeCBDataScope *s = (fsdbTreeCBDataScope*)tree_cb_data;
        ctx->scope_parts.push_back(std::string(s->name));
        break;
    }
    case FSDB_TREE_CBT_UPSCOPE:
        if (!ctx->scope_parts.empty())
            ctx->scope_parts.pop_back();
        break;

    case FSDB_TREE_CBT_VAR: {
        fsdbTreeCBDataVar *v = (fsdbTreeCBDataVar*)tree_cb_data;

        /* 拼完整路径 */
        std::string full_path;
        for (size_t i = 0; i < ctx->scope_parts.size(); i++) {
            if (i) full_path += ".";
            full_path += ctx->scope_parts[i];
        }
        if (!full_path.empty()) full_path += ".";
        full_path += std::string(v->name);

        SigInfo info;
        info.idcode        = v->u.idcode;
        info.bit_size      = (v->lbitnum >= v->rbitnum)
                             ? (v->lbitnum - v->rbitnum + 1)
                             : (v->rbitnum - v->lbitnum + 1);
        info.bytes_per_bit = v->bytes_per_bit;
        info.direction     = (uint_T)v->direction;
        info.var_type      = (uint_T)v->type;
        info.full_path     = full_path;

        ctx->path_to_sig[full_path] = info;
        break;
    }
    default:
        break;
    }
    return TRUE;
}

/* ─── 辅助：把 VC bytes 转成可打印字符串 ─────────────────────────── */

static std::string
_VCToStr(byte_T *vc_ptr, uint_T bit_size, uint_T bpb)
{
    if (!vc_ptr) return "?";
    if (bit_size > 65536) {
        char msg[128];
        snprintf(msg, sizeof(msg),
                 "ERROR:bit_size=%u_exceeds_limit", bit_size);
        return std::string(msg);
    }

    if (bpb == FSDB_BYTES_PER_BIT_1B) {
        /* 0/1/x/z 编码 */
        std::string s(bit_size, '?');
        for (uint_T i = 0; i < bit_size; i++) {
            switch (vc_ptr[i]) {
            case FSDB_BT_VCD_0: s[i] = '0'; break;
            case FSDB_BT_VCD_1: s[i] = '1'; break;
            case FSDB_BT_VCD_X: s[i] = 'x'; break;
            case FSDB_BT_VCD_Z: s[i] = 'z'; break;
            default:            s[i] = 'u'; break;
            }
        }
        return s;
    }
    else if (bpb == FSDB_BYTES_PER_BIT_4B) {
        /* real/float */
        char buf[64];
        snprintf(buf, sizeof(buf), "%f", *((float*)vc_ptr));
        return std::string(buf);
    }
    else if (bpb == FSDB_BYTES_PER_BIT_8B) {
        char buf[64];
        snprintf(buf, sizeof(buf), "%e", *((double*)vc_ptr));
        return std::string(buf);
    }
    return "?";
}

/* ── tick↔ps 换算的唯一收口 ──────────────────────────────────────────
 * 所有时间转换必须走这两个 helper；任何绕过它们的手写 <<32| 都是回归。
 * 调用前必须保证 ctx->scale_fs != 0（各入口先检查并返回
 * FSDB_ERR_SCALE_UNKNOWN）。
 *
 * 取整方向是一对契约：
 *   输入 ps→tick 用 floor —— ffrGotoXTag 是 at-or-before 语义，floor 保持
 *   "取 T 时刻及之前的值" 精确；
 *   输出 tick→ps 用 ceil  —— sub-ps 刻度下跳变可能落在非整数 ps（如
 *   ...897.9 ps），向上取整保证「拿工具报告的跳变时间戳回查，必落在跳变
 *   之后、取到新值」；floor 会落在跳变之前取到旧值（隐蔽 off-by-one）。
 *   对 1ps 及更粗的刻度两个方向都精确无损。 */

static fsdbTag64
_ToTag(const FsdbCtx *ctx, unsigned long long time_ps)
{
    unsigned __int128 fs = (unsigned __int128)time_ps * FS_PER_PS;
    unsigned long long tick = (unsigned long long)(fs / ctx->scale_fs);
    fsdbTag64 tag;
    tag.H = (uint_T)(tick >> 32);
    tag.L = (uint_T)(tick & 0xFFFFFFFF);
    return tag;
}

static unsigned long long
_TagToPs(const FsdbCtx *ctx, const fsdbTag64 &tag)
{
    unsigned long long tick = ((unsigned long long)tag.H << 32) | tag.L;
    unsigned __int128 fs = (unsigned __int128)tick * ctx->scale_fs;
    return (unsigned long long)((fs + FS_PER_PS - 1) / FS_PER_PS);
}

static bool
_AppendText(char *out_buf, int buf_size, int &pos,
            const std::string &text, bool &truncated)
{
    if (truncated) return false;
    const char *marker = "@TRUNCATED\n";
    int marker_len = (int)strlen(marker);
    int len = (int)text.size();
    /* Always reserve enough tail room for a parseable truncation receipt. This
     * avoids the old failure mode where the marker was written near the end of
     * the allocation, after stale bytes beyond `pos`, so ctypes stopped at the
     * earlier NUL and Python never saw it. */
    if (pos + len + marker_len + 1 > buf_size) {
        if (pos + marker_len + 1 <= buf_size) {
            memcpy(out_buf + pos, marker, marker_len);
            pos += marker_len;
            out_buf[pos] = '\0';
        } else if (buf_size > 0) {
            out_buf[buf_size - 1] = '\0';
        }
        truncated = true;
        return false;
    }
    memcpy(out_buf + pos, text.c_str(), len);
    pos += len;
    out_buf[pos] = '\0';
    return true;
}

static bool
_AppendTransitionLine(
    char *out_buf,
    int buf_size,
    int &pos,
    unsigned long long time_ps,
    const std::string &value,
    bool &truncated
)
{
    /* std::string, not a fixed buffer: a wide bus value can exceed any fixed
     * size and would otherwise be truncated (dropping the '\n'). */
    std::string line = std::to_string(time_ps) + "\t" + value + "\n";
    return _AppendText(out_buf, buf_size, pos, line, truncated);
}

/* ═══════════════════════════════════════════════════════════════════
 * C 接口（extern "C" 保证符号不被 mangle，Python ctypes 可直接调用）
 * ═══════════════════════════════════════════════════════════════════ */
extern "C" {

/* ── 打开 FSDB，建立信号索引，返回 ctx 指针（失败返回 NULL）──────── */
void*
fsdb_open(const char *fname)
{
    if (!ffrObject::ffrIsFSDB((str_T)fname))
        return NULL;

    ffrObject *obj = ffrObject::ffrOpen3((str_T)fname);
    if (!obj) return NULL;

    FsdbCtx *ctx = new FsdbCtx();
    ctx->obj       = obj;
    ctx->tree_done = false;
    ctx->transition_group_active = false;

    /* 从文件头读时间刻度（运行时真值，绝不写死 1ps）。读不到 → scale_fs=0，
     * 时间型接口一律拒绝服务而不是返回错位的数值。 */
    ctx->scale_unit[0] = '\0';
    str_T su = obj->ffrGetScaleUnit();
    if (su && su[0]) {
        strncpy(ctx->scale_unit, su, sizeof(ctx->scale_unit) - 1);
        ctx->scale_unit[sizeof(ctx->scale_unit) - 1] = '\0';
    }
    ctx->scale_fs = _ParseScaleFs(ctx->scale_unit);

    obj->ffrSetTreeCBFunc(_TreeCB, ctx);
    obj->ffrReadScopeVarTree();
    ctx->tree_done = true;

    /* 建立 idcode 反向索引 */
    for (auto &kv : ctx->path_to_sig)
        ctx->id_to_sig[kv.second.idcode] = &kv.second;

    return (void*)ctx;
}

/* ── 关闭 ────────────────────────────────────────────────────────── */
void
fsdb_close(void *handle)
{
    if (!handle) return;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    if (ctx->transition_group_active) {
        ctx->obj->ffrUnloadSignals();
        ctx->transition_group_active = false;
        ctx->transition_group_ids.clear();
    }
    ctx->obj->ffrClose();
    delete ctx;
}

/* ── 搜索信号（关键字匹配，结果写入 out_buf，换行分隔）────────────
 * 返回匹配数量，out_buf 内容格式：
 *   <full_path>\t<bit_size>\t<direction>\t<var_type>\n
 *   direction: fsdbVarDir (0=IMPLICIT 1=INPUT 2=OUTPUT 3=INOUT 4=BUFFER 5=LINKAGE)
 *   var_type:  fsdbVarType (2=PARAMETER 3=REAL 4=REG 15=WIRE 17=MEMORY ...)
 * ---------------------------------------------------------------- */
int
fsdb_search_signals(void *handle, const char *keyword,
                    char *out_buf, int buf_size)
{
    if (!handle || !keyword || !out_buf) return -1;
    FsdbCtx *ctx = (FsdbCtx*)handle;

    std::string kw(keyword);
    /* 转小写 */
    for (auto &c : kw) c = tolower(c);

    int count = 0;
    int pos   = 0;
    for (auto &kv : ctx->path_to_sig) {
        std::string lpath = kv.first;
        for (auto &c : lpath) c = tolower(c);
        if (lpath.find(kw) == std::string::npos) continue;

        char line[512];
        snprintf(line, sizeof(line), "%s\t%u\t%u\t%u\n",
                 kv.first.c_str(),
                 kv.second.bit_size,
                 kv.second.direction,
                 kv.second.var_type);
        int len = strlen(line);
        if (pos + len + 1 >= buf_size) break;
        memcpy(out_buf + pos, line, len);
        pos  += len;
        count++;
    }
    out_buf[pos] = '\0';
    return count;
}

/* ── 获取信号在指定时刻的值（time_ps = ps 精度）─────────────────────
 * 返回 0 成功，-1 失败
 * out_val 写入字符串如 "01xz" 或 "1" 等
 * ---------------------------------------------------------------- */
int
fsdb_get_value_at_time(void *handle, const char *signal_path,
                       unsigned long long time_ps,
                       char *out_val, int val_buf_size)
{
    if (!handle || !signal_path || !out_val) return -1;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    if (ctx->scale_fs == 0) return FSDB_ERR_SCALE_UNKNOWN;

    auto it = ctx->path_to_sig.find(std::string(signal_path));
    if (it == ctx->path_to_sig.end()) return -2;   /* 信号未找到 */

    fsdbVarIdcode idcode = it->second.idcode;
    uint_T        bpb    = it->second.bytes_per_bit;
    uint_T        bsize  = it->second.bit_size;

    /* 加载该信号的 VC */
    ctx->obj->ffrAddToSignalList(idcode);
    ctx->obj->ffrLoadSignals();

    ffrVCTrvsHdl hdl = ctx->obj->ffrCreateVCTraverseHandle(idcode);
    if (!hdl) {
        ctx->obj->ffrUnloadSignals();
        return -3;
    }

    fsdbTag64 tag = _ToTag(ctx, time_ps);

    std::string result = "x";

    if (hdl->ffrHasIncoreVC()) {
        /* 跳到最近的时间点（向前对齐） */
        if (FSDB_RC_SUCCESS == hdl->ffrGotoXTag((void*)&tag)) {
            byte_T *vc_ptr = NULL;
            if (FSDB_RC_SUCCESS == hdl->ffrGetVC(&vc_ptr) && vc_ptr)
                result = _VCToStr(vc_ptr, bsize, bpb);
        }
    }

    hdl->ffrFree();
    ctx->obj->ffrUnloadSignals();

    strncpy(out_val, result.c_str(), val_buf_size - 1);
    out_val[val_buf_size - 1] = '\0';
    return 0;
}

static int
_GetTransitionsImpl(
    FsdbCtx *ctx,
    SigInfo *sig,
    unsigned long long start_ps,
    unsigned long long end_ps,
    char *out_buf,
    int buf_size,
    bool already_loaded,
    FsdbTransitionProfileV1 *profile
)
{
    if (!ctx || !sig || !out_buf || buf_size <= 0) return -1;

    fsdbVarIdcode idcode = sig->idcode;
    if (already_loaded) {
        if (!ctx->transition_group_active ||
            ctx->transition_group_ids.find(idcode) == ctx->transition_group_ids.end())
            return -5;
    } else {
        _ProfileClock::time_point begin = _ProfileClock::now();
        ctx->obj->ffrAddToSignalList(idcode);
        _ProfileClock::time_point added = _ProfileClock::now();
        ctx->obj->ffrLoadSignals();
        _ProfileClock::time_point loaded = _ProfileClock::now();
        if (profile) {
            profile->add_signal_ns = _ElapsedNs(begin, added);
            profile->load_ns = _ElapsedNs(added, loaded);
        }
    }

    _ProfileClock::time_point create_begin = _ProfileClock::now();
    ffrVCTrvsHdl hdl = ctx->obj->ffrCreateVCTraverseHandle(idcode);
    _ProfileClock::time_point create_end = _ProfileClock::now();
    if (profile) profile->create_handle_ns = _ElapsedNs(create_begin, create_end);
    if (!hdl) {
        if (!already_loaded) {
            _ProfileClock::time_point unload_begin = _ProfileClock::now();
            ctx->obj->ffrUnloadSignals();
            if (profile)
                profile->unload_ns = _ElapsedNs(unload_begin, _ProfileClock::now());
        }
        return -3;
    }

    int   count     = 0;
    int   pos       = 0;
    bool  truncated = false;

    if (hdl->ffrHasIncoreVC()) {
        fsdbTag64 start_tag = _ToTag(ctx, start_ps);
        _ProfileClock::time_point seek_begin = _ProfileClock::now();
        hdl->ffrGotoXTag((void*)&start_tag);
        _ProfileClock::time_point seek_end = _ProfileClock::now();
        if (profile) profile->seek_ns = _ElapsedNs(seek_begin, seek_end);

        _ProfileClock::time_point traverse_begin = _ProfileClock::now();
        do {
            fsdbTag64  time;
            byte_T    *vc_ptr = NULL;
            hdl->ffrGetXTag(&time);
            hdl->ffrGetVC(&vc_ptr);

            unsigned long long t_ps = _TagToPs(ctx, time);
            if (end_ps != (unsigned long long)-1 && t_ps > end_ps)
                break;

            std::string val = _VCToStr(
                vc_ptr, sig->bit_size, sig->bytes_per_bit);
            if (!_AppendTransitionLine(
                    out_buf, buf_size, pos, t_ps, val, truncated)) break;
            count++;
        } while (FSDB_RC_SUCCESS == hdl->ffrGotoNextVC());
        if (profile)
            profile->traverse_format_ns =
                _ElapsedNs(traverse_begin, _ProfileClock::now());
    }

    out_buf[pos] = '\0';
    _ProfileClock::time_point free_begin = _ProfileClock::now();
    hdl->ffrFree();
    _ProfileClock::time_point free_end = _ProfileClock::now();
    if (profile) profile->free_handle_ns = _ElapsedNs(free_begin, free_end);
    if (!already_loaded) {
        _ProfileClock::time_point unload_begin = _ProfileClock::now();
        ctx->obj->ffrUnloadSignals();
        if (profile)
            profile->unload_ns = _ElapsedNs(unload_begin, _ProfileClock::now());
    }
    if (profile) {
        profile->transition_count = (unsigned long long)count;
        profile->output_bytes = (unsigned long long)pos;
        profile->truncated = truncated ? 1 : 0;
    }
    return count;
}

/* ── 获取信号所有跳变（start_ps ~ end_ps，-1 表示到结尾）────────────
 * 结果写入 out_buf，格式：
 *   <time_ps>\t<value>\n
 * 返回跳变条数，-1 失败，-2 信号未找到
 * ---------------------------------------------------------------- */
int
fsdb_get_transitions_profiled(void *handle, const char *signal_path,
                              unsigned long long start_ps,
                              unsigned long long end_ps,
                              char *out_buf, int buf_size,
                              FsdbTransitionProfileV1 *profile)
{
    if (profile) memset(profile, 0, sizeof(*profile));
    if (!handle || !signal_path || !out_buf || buf_size <= 0) return -1;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    if (ctx->scale_fs == 0) return FSDB_ERR_SCALE_UNKNOWN;

    _ProfileClock::time_point lookup_begin = _ProfileClock::now();
    auto it = ctx->path_to_sig.find(std::string(signal_path));
    _ProfileClock::time_point lookup_end = _ProfileClock::now();
    if (profile) profile->lookup_ns = _ElapsedNs(lookup_begin, lookup_end);
    if (it == ctx->path_to_sig.end()) return -2;
    return _GetTransitionsImpl(
        ctx, &it->second, start_ps, end_ps, out_buf, buf_size, false, profile);
}

int
fsdb_get_transitions(void *handle, const char *signal_path,
                     unsigned long long start_ps,
                     unsigned long long end_ps,
                     char *out_buf, int buf_size)
{
    return fsdb_get_transitions_profiled(
        handle, signal_path, start_ps, end_ps, out_buf, buf_size, NULL);
}

/* Load a bounded group once, then let Python request each signal independently
 * through its existing reusable 64 MiB per-call buffer. This avoids per-signal
 * Load/Unload while preserving the legacy output/truncation contract. */
int
fsdb_begin_transition_group(void *handle, const char **signal_paths,
                            int signal_count,
                            FsdbTransitionGroupProfileV1 *profile)
{
    if (profile) memset(profile, 0, sizeof(*profile));
    if (!handle || !signal_paths || signal_count <= 0) return -1;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    if (ctx->scale_fs == 0) return FSDB_ERR_SCALE_UNKNOWN;
    if (ctx->transition_group_active) return -5;

    std::vector<fsdbVarIdcode> ids;
    ids.reserve((size_t)signal_count);
    _ProfileClock::time_point lookup_begin = _ProfileClock::now();
    for (int i = 0; i < signal_count; i++) {
        if (!signal_paths[i]) return -1;
        auto it = ctx->path_to_sig.find(std::string(signal_paths[i]));
        if (it == ctx->path_to_sig.end()) return -2;
        ids.push_back(it->second.idcode);
    }
    _ProfileClock::time_point lookup_end = _ProfileClock::now();

    _ProfileClock::time_point add_begin = _ProfileClock::now();
    for (size_t i = 0; i < ids.size(); i++)
        ctx->obj->ffrAddToSignalList(ids[i]);
    _ProfileClock::time_point add_end = _ProfileClock::now();
    ctx->obj->ffrLoadSignals();
    _ProfileClock::time_point load_end = _ProfileClock::now();

    ctx->transition_group_ids.clear();
    ctx->transition_group_ids.insert(ids.begin(), ids.end());
    ctx->transition_group_active = true;
    if (profile) {
        profile->lookup_ns = _ElapsedNs(lookup_begin, lookup_end);
        profile->add_signal_ns = _ElapsedNs(add_begin, add_end);
        profile->load_ns = _ElapsedNs(add_end, load_end);
        profile->signal_count = (unsigned long long)ids.size();
    }
    return signal_count;
}

int
fsdb_get_loaded_transitions(void *handle, const char *signal_path,
                            unsigned long long start_ps,
                            unsigned long long end_ps,
                            char *out_buf, int buf_size,
                            FsdbTransitionProfileV1 *profile)
{
    if (profile) memset(profile, 0, sizeof(*profile));
    if (!handle || !signal_path || !out_buf || buf_size <= 0) return -1;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    if (ctx->scale_fs == 0) return FSDB_ERR_SCALE_UNKNOWN;

    _ProfileClock::time_point lookup_begin = _ProfileClock::now();
    auto it = ctx->path_to_sig.find(std::string(signal_path));
    _ProfileClock::time_point lookup_end = _ProfileClock::now();
    if (profile) profile->lookup_ns = _ElapsedNs(lookup_begin, lookup_end);
    if (it == ctx->path_to_sig.end()) return -2;
    return _GetTransitionsImpl(
        ctx, &it->second, start_ps, end_ps, out_buf, buf_size, true, profile);
}

int
fsdb_end_transition_group(void *handle,
                          FsdbTransitionGroupProfileV1 *profile)
{
    if (profile) memset(profile, 0, sizeof(*profile));
    if (!handle) return -1;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    if (!ctx->transition_group_active) return 0;
    _ProfileClock::time_point unload_begin = _ProfileClock::now();
    ctx->obj->ffrUnloadSignals();
    _ProfileClock::time_point unload_end = _ProfileClock::now();
    ctx->transition_group_active = false;
    ctx->transition_group_ids.clear();
    if (profile)
        profile->unload_ns = _ElapsedNs(unload_begin, unload_end);
    return 0;
}

int
fsdb_get_multi_signals_around_time(
    void *handle,
    const char **signal_paths,
    int signal_count,
    unsigned long long center_ps,
    unsigned long long window_ps,
    int extra_transitions,
    char *out_buf,
    int buf_size
)
{
    if (!handle || !signal_paths || signal_count < 0 || !out_buf) return -1;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    if (ctx->scale_fs == 0) return FSDB_ERR_SCALE_UNKNOWN;
    out_buf[0] = '\0';

    std::vector<SigInfo*> valid_sigs;
    valid_sigs.reserve(signal_count);

    int pos = 0;
    bool truncated = false;
    int success_count = 0;

    for (int i = 0; i < signal_count; i++) {
        const char *path = signal_paths[i];
        if (!path) continue;
        auto it = ctx->path_to_sig.find(std::string(path));
        if (it == ctx->path_to_sig.end()) {
            std::string err_line = std::string("@ERROR\t") + path + "\tsignal_not_found\n";
            _AppendText(out_buf, buf_size, pos, err_line, truncated);
            continue;
        }
        valid_sigs.push_back(&it->second);
        ctx->obj->ffrAddToSignalList(it->second.idcode);
    }

    if (valid_sigs.empty() || truncated) {
        return success_count;
    }

    ctx->obj->ffrLoadSignals();

    unsigned long long start_ps = (center_ps > window_ps) ? (center_ps - window_ps) : 0;
    unsigned long long end_ps = center_ps + window_ps;

    for (size_t i = 0; i < valid_sigs.size(); i++) {
        SigInfo *sig = valid_sigs[i];
        std::string header = std::string("@SIGNAL\t") + sig->full_path + "\t" +
                             std::to_string(sig->bit_size) + "\n";
        if (!_AppendText(out_buf, buf_size, pos, header, truncated)) break;

        ffrVCTrvsHdl hdl = ctx->obj->ffrCreateVCTraverseHandle(sig->idcode);
        if (!hdl) {
            std::string err_line = std::string("@ERROR\t") + sig->full_path +
                                   "\tcreate_traverse_handle_failed\n";
            _AppendText(out_buf, buf_size, pos, err_line, truncated);
            continue;
        }

        std::string value_at_center = "?";
        if (hdl->ffrHasIncoreVC()) {
            fsdbTag64 center_tag = _ToTag(ctx, center_ps);
            if (FSDB_RC_SUCCESS == hdl->ffrGotoXTag((void*)&center_tag)) {
                byte_T *vc_ptr = NULL;
                if (FSDB_RC_SUCCESS == hdl->ffrGetVC(&vc_ptr) && vc_ptr) {
                    value_at_center = _VCToStr(vc_ptr, sig->bit_size, sig->bytes_per_bit);
                }
            }
        }

        std::string value_line = std::string("#VALUE_AT_CENTER\t") + value_at_center + "\n";
        if (!_AppendText(out_buf, buf_size, pos, value_line, truncated)) {
            hdl->ffrFree();
            break;
        }
        if (!_AppendText(out_buf, buf_size, pos, "#WINDOW_TRANSITIONS\n", truncated)) {
            hdl->ffrFree();
            break;
        }

        if (hdl->ffrHasIncoreVC()) {
            fsdbTag64 start_tag = _ToTag(ctx, start_ps);
            if (FSDB_RC_SUCCESS == hdl->ffrGotoXTag((void*)&start_tag)) {
                do {
                    fsdbTag64 time;
                    byte_T *vc_ptr = NULL;
                    hdl->ffrGetXTag(&time);
                    hdl->ffrGetVC(&vc_ptr);
                    unsigned long long t_ps = _TagToPs(ctx, time);
                    if (t_ps > end_ps) break;
                    std::string value = _VCToStr(vc_ptr, sig->bit_size, sig->bytes_per_bit);
                    if (!_AppendTransitionLine(out_buf, buf_size, pos, t_ps, value, truncated))
                        break;
                } while (!truncated && FSDB_RC_SUCCESS == hdl->ffrGotoNextVC());
            }
        }

        if (truncated) {
            hdl->ffrFree();
            break;
        }

        if (!_AppendText(out_buf, buf_size, pos, "#PRE_WINDOW_TRANSITIONS\n", truncated)) {
            hdl->ffrFree();
            break;
        }

        if (hdl->ffrHasIncoreVC() && extra_transitions > 0) {
            fsdbTag64 start_tag = _ToTag(ctx, start_ps);
            if (FSDB_RC_SUCCESS == hdl->ffrGotoXTag((void*)&start_tag)) {
                for (int n = 0; n < extra_transitions; n++) {
                    if (FSDB_RC_SUCCESS != hdl->ffrGotoPrevVC()) break;
                    fsdbTag64 time;
                    byte_T *vc_ptr = NULL;
                    hdl->ffrGetXTag(&time);
                    hdl->ffrGetVC(&vc_ptr);
                    unsigned long long t_ps = _TagToPs(ctx, time);
                    std::string value = _VCToStr(vc_ptr, sig->bit_size, sig->bytes_per_bit);
                    if (!_AppendTransitionLine(out_buf, buf_size, pos, t_ps, value, truncated))
                        break;
                }
            }
        }

        hdl->ffrFree();
        success_count++;
        if (truncated) break;
    }

    ctx->obj->ffrUnloadSignals();
    return success_count;
}

/*
 * fsdb_batch_window_transitions
 *
 * Single-pass time-based traversal over the union of N signals.
 * Output format (one transition per line, time-sorted):
 *
 *   <time_ps>\t<full_path>\t<value>\n
 *
 * Header lines (one per resolved signal, before the transition stream):
 *
 *   @SIGNAL\t<full_path>\t<bit_size>\n
 *
 * Errors for unresolved paths:
 *
 *   @ERROR\t<path>\tsignal_not_found\n
 *
 * Returns the number of transitions written (>=0) on success, or -1 on
 * argument error. Truncation is signalled by stopping at buf_size and
 * returning the count emitted so far; caller decides if it needs to
 * widen the window.
 */
int
fsdb_batch_window_transitions(
    void *handle,
    const char **signal_paths,
    int signal_count,
    unsigned long long start_ps,
    unsigned long long end_ps,
    char *out_buf,
    int buf_size
)
{
    if (!handle || !signal_paths || signal_count < 0 || !out_buf || buf_size <= 0) return -1;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    if (ctx->scale_fs == 0) return FSDB_ERR_SCALE_UNKNOWN;
    out_buf[0] = '\0';
    int pos = 0;
    bool truncated = false;
    int emitted = 0;

    std::vector<fsdbVarIdcode> idcodes;
    idcodes.reserve(signal_count);
    /* idcode → SigInfo* for value formatting during walk */
    std::map<fsdbVarIdcode, SigInfo*> id_to_info;

    for (int i = 0; i < signal_count; i++) {
        const char *path = signal_paths[i];
        if (!path) continue;
        auto it = ctx->path_to_sig.find(std::string(path));
        if (it == ctx->path_to_sig.end()) {
            std::string err = std::string("@ERROR\t") + path + "\tsignal_not_found\n";
            _AppendText(out_buf, buf_size, pos, err, truncated);
            if (truncated) return emitted;
            continue;
        }
        SigInfo *sig = &it->second;
        idcodes.push_back(sig->idcode);
        id_to_info[sig->idcode] = sig;
        ctx->obj->ffrAddToSignalList(sig->idcode);

        std::string header = std::string("@SIGNAL\t") + sig->full_path + "\t" +
                             std::to_string(sig->bit_size) + "\n";
        _AppendText(out_buf, buf_size, pos, header, truncated);
        if (truncated) return emitted;
    }

    if (idcodes.empty()) return emitted;

    ctx->obj->ffrLoadSignals();

    ffrTimeBasedVCTrvsHdl thdl =
        ctx->obj->ffrCreateTimeBasedVCTrvsHdl((uint_T)idcodes.size(), idcodes.data());
    if (!thdl) {
        ctx->obj->ffrUnloadSignals();
        std::string err = "@ERROR\t*\tcreate_time_based_handle_failed\n";
        _AppendText(out_buf, buf_size, pos, err, truncated);
        return emitted;
    }

    /*
     * Time-based handle has no GotoXTag; we walk from the beginning
     * via ffrGotoNextVC and skip transitions strictly before start_ps.
     */
    while (FSDB_RC_SUCCESS == thdl->ffrGotoNextVC()) {
        fsdbVarIdcode idc;
        fsdbXTag      xtag;
        byte_T       *vc_ptr = NULL;
        fsdbSeqNum    seq;
        if (FSDB_RC_SUCCESS != thdl->ffrGetVarIdcodeXTagVCSeqNum(&idc, &xtag, &vc_ptr, &seq)) {
            continue;
        }
        if (!vc_ptr) continue;

        fsdbTag64 *tag64 = (fsdbTag64*)&xtag;
        unsigned long long t_ps = _TagToPs(ctx, *tag64);
        if (t_ps < start_ps) continue;
        if (t_ps > end_ps) break;

        auto it = id_to_info.find(idc);
        if (it == id_to_info.end()) continue;
        SigInfo *sig = it->second;

        std::string value = _VCToStr(vc_ptr, sig->bit_size, sig->bytes_per_bit);
        std::string line = std::to_string(t_ps) + "\t" + sig->full_path + "\t" + value + "\n";
        if (!_AppendText(out_buf, buf_size, pos, line, truncated)) break;
        emitted++;
    }

    thdl->ffrFree();
    ctx->obj->ffrUnloadSignals();
    return emitted;
}

/* ── 获取仿真结束时间（ps）─────────────────────────────────────────
 * 首选 reader 的文件级全局时间 API ffrGetMaxFsdbTag64()，它直接返回整个
 * FSDB 的最大仿真时间，与具体信号无关。旧实现从“最大 idcode 的单个信号”
 * 推导结束时间——该信号是任意挑选的，若它恰好是 static/config/testbench
 * 记账信号（无值变化），就会让一个完全有效的 FSDB 报出 end_ps==0。只有在
 * 全局 API 不可用（返回 FAILURE）时才退回旧的单信号遍历作为最后兜底。
 */
unsigned long long
fsdb_get_end_time(void *handle)
{
    if (!handle) return 0;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    /* 刻度未知无法换算；返回 0，Python 层依据 scale_unit=unknown 报警 */
    if (ctx->scale_fs == 0) return 0;

    /* 首选：文件级全局最大时间 */
    fsdbTag64 gmax;
    if (FSDB_RC_SUCCESS == ctx->obj->ffrGetMaxFsdbTag64(&gmax))
        return _TagToPs(ctx, gmax);

    /* 兜底：旧的单信号遍历（全局 API 不可用时） */
    fsdbVarIdcode max_id = ctx->obj->ffrGetMaxVarIdcode();
    if (max_id < FSDB_MIN_VAR_IDCODE) return 0;

    ctx->obj->ffrAddToSignalList(max_id);
    ctx->obj->ffrLoadSignals();

    ffrVCTrvsHdl hdl = ctx->obj->ffrCreateVCTraverseHandle(max_id);
    unsigned long long end_ps = 0;
    if (hdl && hdl->ffrHasIncoreVC()) {
        fsdbTag64 time;
        if (FSDB_RC_SUCCESS == hdl->ffrGetMaxXTag((void*)&time))
            end_ps = _TagToPs(ctx, time);
        hdl->ffrFree();
    }
    ctx->obj->ffrUnloadSignals();
    return end_ps;
}

/* ── 获取信号总数 ───────────────────────────────────────────────── */
int
fsdb_get_signal_count(void *handle)
{
    if (!handle) return 0;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    return (int)ctx->path_to_sig.size();
}

/* ── 读取时间刻度 ────────────────────────────────────────────────────
 * 返回 fs/tick 换算系数（0 = 刻度读不到/无法解析）；out_buf 写入文件头
 * 原始刻度字符串（如 "100fs"），未知时写 "unknown"。
 * Python 层用它把刻度暴露在 get_waveform_summary 中，任何人可一眼核对
 * 工具认成的单位。 */
unsigned long long
fsdb_get_scale_info(void *handle, char *out_buf, int buf_size)
{
    if (out_buf && buf_size > 0) out_buf[0] = '\0';
    if (!handle) return 0;
    FsdbCtx *ctx = (FsdbCtx*)handle;
    if (out_buf && buf_size > 0) {
        const char *s = ctx->scale_unit[0] ? ctx->scale_unit : "unknown";
        strncpy(out_buf, s, buf_size - 1);
        out_buf[buf_size - 1] = '\0';
    }
    return ctx->scale_fs;
}

} /* extern "C" */

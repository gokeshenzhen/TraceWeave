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
#include <string>
#include <vector>

/* ─── 内部数据结构 ─────────────────────────────────────────────────── */

struct SigInfo {
    fsdbVarIdcode idcode;
    uint_T        bit_size;
    uint_T        bytes_per_bit;
    std::string   full_path;   /* top_tb.dut.s_bits */
};

struct FsdbCtx {
    ffrObject                        *obj;
    std::map<std::string, SigInfo>    path_to_sig;   /* full_path → SigInfo */
    std::map<fsdbVarIdcode, SigInfo*> id_to_sig;
    std::string                       scope_stack;   /* 当前遍历路径 */
    std::vector<std::string>          scope_parts;
    bool                              tree_done;
};

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

static fsdbTag64
_ToTag(unsigned long long time_ps)
{
    fsdbTag64 tag;
    tag.H = (uint_T)(time_ps >> 32);
    tag.L = (uint_T)(time_ps & 0xFFFFFFFF);
    return tag;
}

static unsigned long long
_TagToPs(const fsdbTag64 &tag)
{
    return ((unsigned long long)tag.H << 32) | tag.L;
}

static bool
_AppendText(char *out_buf, int buf_size, int &pos,
            const std::string &text, bool &truncated)
{
    if (truncated) return false;
    int len = (int)text.size();
    if (pos + len + 1 >= buf_size) {
        const char *marker = "@TRUNCATED\n";
        int marker_len = (int)strlen(marker);
        if (buf_size > marker_len) {
            int marker_pos = buf_size - marker_len - 1;
            if (marker_pos < 0) marker_pos = 0;
            memcpy(out_buf + marker_pos, marker, marker_len);
            out_buf[marker_pos + marker_len] = '\0';
            pos = marker_pos + marker_len;
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
    char line[1024];
    snprintf(line, sizeof(line), "%llu\t%s\n", time_ps, value.c_str());
    return _AppendText(out_buf, buf_size, pos, std::string(line), truncated);
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
    ctx->obj->ffrClose();
    delete ctx;
}

/* ── 搜索信号（关键字匹配，结果写入 out_buf，换行分隔）────────────
 * 返回匹配数量，out_buf 内容格式：
 *   <full_path>\t<bit_size>\n
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
        snprintf(line, sizeof(line), "%s\t%u\n",
                 kv.first.c_str(), kv.second.bit_size);
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

    /* 构造 fsdbTag64 时间戳（time_ps 直接作为 L，H=0，适合 < 2^32 ps） */
    fsdbTag64 tag;
    tag.H = (uint_T)(time_ps >> 32);
    tag.L = (uint_T)(time_ps & 0xFFFFFFFF);

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

/* ── 获取信号所有跳变（start_ps ~ end_ps，-1 表示到结尾）────────────
 * 结果写入 out_buf，格式：
 *   <time_ps>\t<value>\n
 * 返回跳变条数，-1 失败，-2 信号未找到
 * ---------------------------------------------------------------- */
int
fsdb_get_transitions(void *handle, const char *signal_path,
                     unsigned long long start_ps,
                     unsigned long long end_ps,
                     char *out_buf, int buf_size)
{
    if (!handle || !signal_path || !out_buf) return -1;
    FsdbCtx *ctx = (FsdbCtx*)handle;

    auto it = ctx->path_to_sig.find(std::string(signal_path));
    if (it == ctx->path_to_sig.end()) return -2;

    fsdbVarIdcode idcode = it->second.idcode;
    uint_T        bpb    = it->second.bytes_per_bit;
    uint_T        bsize  = it->second.bit_size;

    ctx->obj->ffrAddToSignalList(idcode);
    ctx->obj->ffrLoadSignals();

    ffrVCTrvsHdl hdl = ctx->obj->ffrCreateVCTraverseHandle(idcode);
    if (!hdl) {
        ctx->obj->ffrUnloadSignals();
        return -3;
    }

    int   count = 0;
    int   pos   = 0;

    if (hdl->ffrHasIncoreVC()) {
        /* 跳到 start_ps */
        fsdbTag64 start_tag;
        start_tag.H = (uint_T)(start_ps >> 32);
        start_tag.L = (uint_T)(start_ps & 0xFFFFFFFF);
        hdl->ffrGotoXTag((void*)&start_tag);

        do {
            fsdbTag64  time;
            byte_T    *vc_ptr = NULL;
            hdl->ffrGetXTag(&time);
            hdl->ffrGetVC(&vc_ptr);

            unsigned long long t_ps =
                ((unsigned long long)time.H << 32) | time.L;
            if (end_ps != (unsigned long long)-1 && t_ps > end_ps)
                break;

            std::string val = _VCToStr(vc_ptr, bsize, bpb);
            char line[512];
            snprintf(line, sizeof(line), "%llu\t%s\n", t_ps, val.c_str());
            int len = strlen(line);
            if (pos + len + 1 >= buf_size) break;
            memcpy(out_buf + pos, line, len);
            pos += len;
            count++;
        } while (FSDB_RC_SUCCESS == hdl->ffrGotoNextVC());
    }

    out_buf[pos] = '\0';
    hdl->ffrFree();
    ctx->obj->ffrUnloadSignals();
    return count;
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
            fsdbTag64 center_tag = _ToTag(center_ps);
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
            fsdbTag64 start_tag = _ToTag(start_ps);
            if (FSDB_RC_SUCCESS == hdl->ffrGotoXTag((void*)&start_tag)) {
                do {
                    fsdbTag64 time;
                    byte_T *vc_ptr = NULL;
                    hdl->ffrGetXTag(&time);
                    hdl->ffrGetVC(&vc_ptr);
                    unsigned long long t_ps = _TagToPs(time);
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
            fsdbTag64 start_tag = _ToTag(start_ps);
            if (FSDB_RC_SUCCESS == hdl->ffrGotoXTag((void*)&start_tag)) {
                for (int n = 0; n < extra_transitions; n++) {
                    if (FSDB_RC_SUCCESS != hdl->ffrGotoPrevVC()) break;
                    fsdbTag64 time;
                    byte_T *vc_ptr = NULL;
                    hdl->ffrGetXTag(&time);
                    hdl->ffrGetVC(&vc_ptr);
                    unsigned long long t_ps = _TagToPs(time);
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

/* ── 获取仿真结束时间（ps）─────────────────────────────────────── */
unsigned long long
fsdb_get_end_time(void *handle)
{
    if (!handle) return 0;
    FsdbCtx *ctx = (FsdbCtx*)handle;

    fsdbVarIdcode max_id = ctx->obj->ffrGetMaxVarIdcode();
    if (max_id < FSDB_MIN_VAR_IDCODE) return 0;

    ctx->obj->ffrAddToSignalList(max_id);
    ctx->obj->ffrLoadSignals();

    ffrVCTrvsHdl hdl = ctx->obj->ffrCreateVCTraverseHandle(max_id);
    unsigned long long end_ps = 0;
    if (hdl && hdl->ffrHasIncoreVC()) {
        fsdbTag64 time;
        if (FSDB_RC_SUCCESS == hdl->ffrGetMaxXTag((void*)&time))
            end_ps = ((unsigned long long)time.H << 32) | time.L;
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

} /* extern "C" */

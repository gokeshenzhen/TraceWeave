#include <cassert>
#include <stdexcept>
#include <string>
#include <vector>

struct fsdbTag64 { unsigned H, L; };
struct fsdbXTag { fsdbTag64 hltag; };
struct Session { Session *next; fsdbXTag start_xtag, close_xtag; };
const int FSDB_RC_SUCCESS = 0, FSDB_FT_VERILOG = 0;

#include "fsdb_session_reclaim.h"

struct Reader {
    std::vector<Session> rows;
    fsdbTag64 minimum = {0, 0}, maximum = {0, 20};
    bool throws = false, bounds_fail = false;
    int type = FSDB_FT_VERILOG;
    unsigned calls = 0;
    Reader() { Set({{0, 0}, {0, 10}, {0, 20}}); }
    void Set(std::vector<fsdbTag64> boundaries) {
        rows.resize(boundaries.size() - 1);
        for (std::size_t i = 0; i < rows.size(); ++i)
            rows[i] = {i + 1 < rows.size() ? &rows[i + 1] : nullptr,
                       {boundaries[i]}, {boundaries[i + 1]}};
    }
    int ffrGetFileType() { return type; }
    int ffrGetMinFsdbTag64(fsdbTag64 *out) { *out = minimum; return bounds_fail ? 1 : 0; }
    int ffrGetMaxFsdbTag64(fsdbTag64 *out) { *out = maximum; return bounds_fail ? 1 : 0; }
    Session *ffrGetSessionListInfo() {
        ++calls;
        if (throws) throw std::runtime_error("metadata");
        return rows.empty() ? nullptr : &rows[0];
    }
};

int main(int argc, char **argv) {
    assert(argc == 2);
    const std::string mode = argv[1];
    Reader reader;
    FsdbSessionReclaimPlan plan;
    if (mode == "valid" || mode == "zero_prefix" || mode == "carry") {
        if (mode == "zero_prefix") reader.Set({{0, 0}, {0, 0}, {0, 10}, {0, 20}});
        if (mode == "carry") {
            reader.minimum = {0, 0xfffffff0}; reader.maximum = {1, 10};
            reader.Set({reader.minimum, {1, 0}, reader.maximum});
        }
        assert(plan.Load(&reader));
        reader.throws = true;
        assert(plan.Load(&reader) && reader.calls == 1); // One metadata walk.
        FsdbSessionReclaimPosition p;
        p.anchor = reader.minimum;
        if (mode == "carry") {
            assert(plan.Defer(p, {0, 0xffffffff}));
            assert(plan.Defer(p, {1, 0}));
            assert(!plan.Defer(p, {1, 1}));
            p.anchor = {1, 1};
            assert(plan.Defer(p, {1, 10}) && p.session == 1);
        } else {
            if (mode == "zero_prefix") {
                assert(plan.Defer(p, {0, 0}));
                assert(!plan.Defer(p, {0, 1}));
                p.anchor = {0, 1};
            }
            assert(plan.Defer(p, {0, 9}));
            assert(plan.Defer(p, {0, 10})); // All ordinals at the close stay.
            assert(!plan.Defer(p, {0, 11}));
            p.anchor = {0, 11};
            assert(plan.Defer(p, {0, 20}));
            assert(p.session == (mode == "zero_prefix" ? 2U : 1U));
            assert(!plan.Defer(p, {0, 21}));
            assert(!plan.Defer(p, {0, 10})); // Backward or unmodeled progress.
        }
        return 0;
    }
    if (mode == "missing") reader.rows.clear();
    else if (mode == "single") reader.Set({{0, 0}, {0, 20}});
    else if (mode == "single_zero") reader.Set({{0, 0}, {0, 0}, {0, 20}});
    else if (mode == "gap") reader.rows[1].start_xtag.hltag = {0, 11};
    else if (mode == "overlap") reader.rows[1].start_xtag.hltag = {0, 9};
    else if (mode == "reverse") reader.rows[1].close_xtag.hltag = {0, 9};
    else if (mode == "first") reader.rows[0].start_xtag.hltag = {0, 1};
    else if (mode == "last") reader.rows[1].close_xtag.hltag = {0, 19};
    else if (mode == "beyond") reader.rows[1].close_xtag.hltag = {0, 21};
    else if (mode == "zero_middle") reader.Set({{0, 0}, {0, 10}, {0, 10}, {0, 20}});
    else if (mode == "bounds") reader.bounds_fail = true;
    else if (mode == "throws") reader.throws = true;
    else if (mode == "type") reader.type = 1;
    else if (mode == "cycle") reader.rows.back().next = &reader.rows.front();
    else if (mode == "limit") {
        std::vector<fsdbTag64> tags;
        for (unsigned i = 0; i <= FsdbSessionReclaimPlan::MaxSessions + 1; ++i)
            tags.push_back({0, i});
        reader.maximum = tags.back(); reader.Set(tags);
    } else assert(false);
    assert(!plan.Load(&reader));
    const auto calls = reader.calls;
    assert(!plan.Load(&reader) && reader.calls == calls);
    FsdbSessionReclaimPosition p;
    assert(!plan.Defer(p, {0, 1}));
}

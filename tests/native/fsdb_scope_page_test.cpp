/* Independent scope/page oracle against the real C ABI, without simulation. */
#include "fsdb_wrapper.cpp"
#include <cassert>

static void add(FsdbCtx &ctx, std::vector<std::string> scope, std::string name)
{
    SigInfo info = {};
    std::string path;
    auto ends = std::make_shared<std::vector<size_t>>();
    for (const auto &part : scope) {
        if (!path.empty()) path += ".";
        path += part;
        ends->push_back(path.size());
    }
    if (!path.empty()) path += ".";
    path += name;
    info.full_path = path;
    info.scope_ends = ends;
    info.bit_size = 8; info.direction = 2; info.var_type = 15;
    ctx.path_to_sig[path] = info;
}

int main()
{
    FsdbCtx ctx = {};
    add(ctx, {"tb", "a"}, "ready_o");
    add(ctx, {"tb", "a"}, "valid_i");
    add(ctx, {"tb", "a", "gen[3]"}, "bits[1]");
    add(ctx, {"tb", "a0"}, "outside");
    add(ctx, {"tb", "A"}, "upper");
    add(ctx, {"tb", "a.b"}, "escaped_scope");
    add(ctx, {"tb"}, "a.fake_var");
    add(ctx, {"tb", "a"}, "\\odd.name\tline\n ");
    add(ctx, {"tb", "long"}, std::string(3000, 'x'));
    for (int n = 0; n < 10000; ++n)
        add(ctx, {"unrelated", std::to_string(n)}, "s");
    char out[8192], next[8192];
    FsdbScopePageV1 receipt;
    auto page = [&](std::string scope, std::string start, bool direct, unsigned int items,
                    unsigned int bytes, unsigned int scan) {
        assert(fsdb_scope_page_v1(&ctx, scope.c_str(), start.c_str(), direct, items, scan,
                                 out, bytes, next, sizeof(next), &receipt, sizeof(receipt)) == 0);
        std::vector<std::string> paths;
        size_t pos = 0;
        for (unsigned int n = 0; n < receipt.returned; ++n) {
            unsigned int fields[5]; memcpy(fields, out + pos, sizeof(fields)); pos += sizeof(fields);
            assert(fields[1] == 8 && fields[2] == 2 && fields[3] == 15);
            paths.push_back(std::string(out + pos, fields[0])); pos += fields[0];
        }
        assert(pos == receipt.output_bytes && pos <= bytes);
        return paths;
    };
    std::vector<std::string> got;
    std::string cursor;
    int pages = 0;
    do {
        auto rows = page("tb.a", cursor, false, 1, sizeof(out), 4096);
        got.insert(got.end(), rows.begin(), rows.end());
        cursor = next;
        assert(++pages < 10);
    } while (!receipt.complete);
    assert((got == std::vector<std::string>{"tb.a.\\odd.name\tline\n ", "tb.a.gen[3].bits[1]",
                                           "tb.a.ready_o", "tb.a.valid_i"}));
    got = page("tb.a", "", true, 20, sizeof(out), 4096);
    assert(got.size() == 3 && receipt.complete);
    assert(receipt.visited <= 6); // neither unrelated scope nor child declarations walked
    assert(page("tb.A", "", false, 1, sizeof(out), 4096)[0] == "tb.A.upper");
    assert(receipt.complete); // exact item cap is not automatically truncation
    assert(page("tb.missing", "", false, 1, sizeof(out), 4096).empty() && receipt.complete);
    assert(page("tb.long", "", false, 1, 20, 4096).empty());
    assert(!receipt.complete && receipt.stop_reason == 2 && std::string(next).size() > 3000);
    assert(page("tb.long", next, false, 1, sizeof(out), 4096)[0].size() > 3000 && receipt.complete);
    page("tb.a", "", false, 20, sizeof(out), 1);
    assert(receipt.visited == 1 && !receipt.complete && next[0]);
    assert(fsdb_scope_page_v1(&ctx, "tb.a", "elsewhere", 0, 1, 1, out, sizeof(out),
                             next, sizeof(next), &receipt, sizeof(receipt)) == -1);
    assert(fsdb_scope_page_v1(&ctx, "tb.a", "", 0, 1, 1, out, 0,
                             next, sizeof(next), &receipt, sizeof(receipt)) == -1);
    assert(fsdb_scope_page_v1(&ctx, "tb.a", "", 0, 1, 1, out, sizeof(out),
                             next, 2, &receipt, sizeof(receipt)) == 0);
    assert(!receipt.complete && receipt.stop_reason == 4);
}

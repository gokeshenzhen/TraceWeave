#include "fsdb_wrapper.cpp"
#include <assert.h>

int main() {
    FsdbCtx ctx{};
    const char *names[] = {"top.left.same", "top.right.same", "top.gen[3].bus[7:0]",
                           "top.\\escaped.with.dot ", "top.a[0:7]", "top.alias"};
    const unsigned widths[] = {1, 3, 8, 17, 8, 8};
    for (unsigned i = 0; i < 6; ++i) {
        SigInfo info{};
        info.bit_size = widths[i];
        info.direction = 2;
        info.var_type = 15;
        info.idcode = i == 5 ? 2 : i; // alias preserves declaration metadata
        ctx.path_to_sig[names[i]] = info;
    }
    assert(fsdb_metadata_version() == 1);
    FsdbSignalMetadataV1 metadata{};
    for (unsigned i = 0; i < 6; ++i) {
        assert(fsdb_get_signal_metadata_v1(&ctx, names[i], &metadata, sizeof(metadata)) == 0);
        assert(metadata.width == widths[i] && metadata.direction == 2 && metadata.var_type == 15);
    }
    assert(fsdb_get_signal_metadata_v1(&ctx, "same", &metadata, sizeof(metadata)) == -2);
    assert(fsdb_get_signal_metadata_v1(&ctx, "top.missing", &metadata, sizeof(metadata)) == -2);
    assert(fsdb_get_signal_metadata_v1(&ctx, names[0], &metadata, 1) == -1);
    // Listing is lexical, stops at a complete string, and never infers roots
    // from the first signal sample. The last root is intentionally distant.
    ctx.top_modules.insert("a");
    ctx.top_modules.insert("z");
    char buffer[256] = {};
    int truncated = 0;
    assert(fsdb_get_summary_paths_v1(&ctx, 0, 1, buffer, sizeof(buffer), &truncated) == 1);
    assert(truncated && std::string(buffer) == ctx.path_to_sig.begin()->first);
    assert(fsdb_get_summary_paths_v1(&ctx, 0, 20, buffer, 2, &truncated) == 0);
    assert(truncated && buffer[0] == '\0');
    assert(fsdb_get_summary_paths_v1(&ctx, 1, 20, buffer, sizeof(buffer), &truncated) == 2);
    assert(!truncated && std::string(buffer) == "a" && std::string(buffer + 2) == "z");
    assert(fsdb_get_summary_paths_v1(&ctx, 1, 1, buffer, sizeof(buffer), &truncated) == 1);
    assert(truncated);
    return 0;
}

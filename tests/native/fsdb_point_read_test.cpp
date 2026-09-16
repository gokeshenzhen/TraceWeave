// Exercise the production point-read lifecycle without a Verdi installation.
#include <cassert>
#include <stdexcept>
#include <string>
#include <vector>

struct fsdbTag64 { unsigned H, L; };
typedef fsdbTag64 fsdbXTag;
typedef unsigned fsdbVarIdcode;
const int FSDB_RC_SUCCESS = 0;

#include "fsdb_point_read.h"

struct Handle {
    std::vector<std::string> *calls;
    void ffrFree() { calls->push_back("free"); }
};

struct Reader {
    fsdbTag64 start = {7, 10}, end = {8, 10};
    std::vector<std::string> calls;
    Handle handle = {&calls};
    std::string failure;
    int resets = 0;

    int ffrGetMinFsdbTag64(fsdbTag64 *tag) {
        *tag = start;
        return failure == "bounds" ? 1 : 0;
    }
    int ffrGetMaxFsdbTag64(fsdbTag64 *tag) { *tag = end; return 0; }
    int ffrResetViewWindow(fsdbXTag *a, fsdbXTag *b) {
        calls.push_back("window");
        if (failure == "unsupported") return 1;
        start = *a; end = *b; ++resets;
        // Mutate before failing to model a partially applied request.
        return (failure == "set" && resets == 1) ||
               (failure == "restore" && resets == 2) ? 1 : 0;
    }
    int ffrAddToSignalList(fsdbVarIdcode) {
        calls.push_back("add");
        return failure == "add" ? 1 : 0;
    }
    int ffrLoadSignals() {
        calls.push_back("load");
        if (failure == "throw_load") throw std::runtime_error("load");
        return failure == "load" ? 1 : 0;
    }
    Handle *ffrCreateVCTraverseHandle(fsdbVarIdcode) {
        calls.push_back("create");
        return failure == "create" ? nullptr : &handle;
    }
    int ffrUnloadSignals() {
        calls.push_back("unload");
        return failure == "unload" ? 1 : 0;
    }
};

int main(int argc, char **argv) {
    assert(argc == 2);
    Reader reader;
    reader.failure = argv[1];
    const std::string &test = reader.failure;
    bool resident = test == "resident";
    bool allow = test != "disabled";
    fsdbTag64 point = {7, 50};
    if (test == "before") point = {6, 50};
    if (test == "after") point = {9, 50};
    bool bypass = resident || !allow || test == "bounds" || test == "before" || test == "after";
    bool threw = false;
    try {
        FsdbPointRead<Reader, Handle*> read(&reader);
        int rc = read.Open(1, point, resident, allow);
        bool early = test == "add" || test == "load" || test == "create";
        assert(rc == (early ? -3 : 0));
        assert(read.Narrowed() == (!bypass && test != "set" && test != "unsupported"));
        if (test == "throw_body") throw std::runtime_error("body");
        bool expected_cleanup = test != "restore" && test != "unload";
        assert(read.Finish() == expected_cleanup);
        assert(read.Finish() == expected_cleanup);  // idempotent before destructor
    } catch (const std::runtime_error&) {
        threw = true;
    }
    assert(threw == (test == "throw_load" || test == "throw_body"));
    assert(reader.start.H == 7 && reader.start.L == 10);
    assert(reader.end.H == 8 && reader.end.L == 10);

    std::vector<std::string> expected;
    if (!bypass) expected.push_back("window");
    if (test == "set") expected.push_back("window");
    if (!resident) {
        expected.push_back("add");
        if (test != "add") expected.push_back("load");
    }
    if (test != "add" && test != "load" && test != "throw_load") {
        expected.push_back("create");
        if (test != "create") expected.push_back("free");
    }
    if (!resident) expected.push_back("unload");
    if (!bypass && test != "set" && test != "unsupported") expected.push_back("window");
    assert(reader.calls == expected);
}

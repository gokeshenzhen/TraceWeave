#ifndef TRACEWEAVE_FSDB_POINT_READ_H
#define TRACEWEAVE_FSDB_POINT_READ_H

/* Include after ffrAPI.h. Parameterizing the reader and handle lets the same
 * resource lifecycle run against a fault-injecting reader in portable tests.
 * This guard owns only standalone point loads; resident groups own their load.
 */
template <typename Reader, typename TraverseHandle>
class FsdbPointRead {
    Reader *reader_;
    TraverseHandle handle_;
    fsdbTag64 saved_start_, saved_end_;
    bool loaded_, restore_, narrowed_, cleanup_ok_;

    static bool Less(const fsdbTag64 &a, const fsdbTag64 &b) {
        return a.H < b.H || (a.H == b.H && a.L < b.L);
    }

    bool Restore() {
        if (restore_) {
            restore_ = false;
            if (reader_->ffrResetViewWindow(
                    (fsdbXTag*)&saved_start_, (fsdbXTag*)&saved_end_)
                != FSDB_RC_SUCCESS)
                cleanup_ok_ = false;
        }
        return cleanup_ok_;
    }

    FsdbPointRead(const FsdbPointRead&) = delete;
    FsdbPointRead& operator=(const FsdbPointRead&) = delete;

public:
    explicit FsdbPointRead(Reader *reader)
        : reader_(reader), handle_(nullptr), loaded_(false), restore_(false),
          narrowed_(false), cleanup_ok_(true) {}

    ~FsdbPointRead() { Finish(); }

    int Open(fsdbVarIdcode id, fsdbTag64 tag, bool resident, bool allow_window) {
        if (!resident) {
            /* Query bounds BEFORE restricting: FFR reports the current view's
             * bounds once a view is set. Saving both also preserves an existing
             * non-default view. Out-of-bounds points retain the legacy seek. */
            if (allow_window &&
                reader_->ffrGetMinFsdbTag64(&saved_start_) == FSDB_RC_SUCCESS &&
                reader_->ffrGetMaxFsdbTag64(&saved_end_) == FSDB_RC_SUCCESS &&
                !Less(tag, saved_start_) && !Less(saved_end_, tag)) {
                restore_ = true;  // Also restore a partially failed Set call.
                if (reader_->ffrResetViewWindow((fsdbXTag*)&tag, (fsdbXTag*)&tag)
                    == FSDB_RC_SUCCESS)
                    narrowed_ = true;
                else {
                    fsdbTag64 current_start, current_end;
                    if (reader_->ffrGetMinFsdbTag64(&current_start) == FSDB_RC_SUCCESS &&
                        reader_->ffrGetMaxFsdbTag64(&current_end) == FSDB_RC_SUCCESS &&
                        !Less(current_start, saved_start_) && !Less(saved_start_, current_start) &&
                        !Less(current_end, saved_end_) && !Less(saved_end_, current_end))
                        restore_ = false;  // Unsupported API left the view intact.
                    else if (!Restore())
                        return -6;
                }
            }
            loaded_ = true;  // Unload even if Add/Load partially fails or throws.
            if (reader_->ffrAddToSignalList(id) != FSDB_RC_SUCCESS ||
                reader_->ffrLoadSignals() != FSDB_RC_SUCCESS)
                return -3;
        }
        handle_ = reader_->ffrCreateVCTraverseHandle(id);
        return handle_ ? 0 : -3;
    }

    TraverseHandle Handle() const { return handle_; }
    bool Narrowed() const { return narrowed_; }

    bool Finish() {
        if (handle_) {
            handle_->ffrFree();
            handle_ = nullptr;
        }
        if (loaded_) {
            loaded_ = false;
            if (reader_->ffrUnloadSignals() != FSDB_RC_SUCCESS)
                cleanup_ok_ = false;
        }
        return Restore();
    }
};

#endif

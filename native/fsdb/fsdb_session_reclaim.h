#ifndef TRACEWEAVE_FSDB_SESSION_RECLAIM_H
#define TRACEWEAVE_FSDB_SESSION_RECLAIM_H

#include <cstddef>
#include <vector>

/* Include after ffrAPI.h. Metadata only changes WHEN the existing cursor
 * reclamation runs; it never supplies an event, count or resume position.
 * A closed session boundary includes all records at that raw tag. */
struct FsdbSessionReclaimPosition {
    fsdbTag64 anchor = {};
    std::size_t session = 0;
};

class FsdbSessionReclaimPlan {
    bool attempted_ = false, valid_ = false;
    fsdbTag64 minimum_ = {};
    std::vector<fsdbTag64> closes_;

    static bool Less(const fsdbTag64 &a, const fsdbTag64 &b) {
        return a.H < b.H || (a.H == b.H && a.L < b.L);
    }
    static bool Equal(const fsdbTag64 &a, const fsdbTag64 &b) {
        return a.H == b.H && a.L == b.L;
    }

public:
    // Bound allocation and cyclic/oversized metadata walks. A rejected plan
    // is an optimization bypass; the original record/byte budget still runs.
    static const std::size_t MaxSessions = 65536;

    template <typename Reader>
    bool Load(Reader *reader) {
        if (attempted_) return valid_;
        attempted_ = true;
        try {
            fsdbTag64 maximum;
            if (reader->ffrGetFileType() != FSDB_FT_VERILOG ||
                reader->ffrGetMinFsdbTag64(&minimum_) != FSDB_RC_SUCCESS ||
                reader->ffrGetMaxFsdbTag64(&maximum) != FSDB_RC_SUCCESS)
                return false;
            std::vector<fsdbTag64> closes;
            fsdbTag64 previous = minimum_;
            std::size_t positive = 0;
            for (auto item = reader->ffrGetSessionListInfo(); item; item = item->next) {
                if (closes.size() == MaxSessions) return false;
                const fsdbTag64 start = {item->start_xtag.hltag.H, item->start_xtag.hltag.L};
                const fsdbTag64 close = {item->close_xtag.hltag.H, item->close_xtag.hltag.L};
                if (!Equal(start, previous) || Less(close, start) || Less(maximum, close))
                    return false;
                if (Less(start, close)) ++positive;
                // A leading zero-span entry is legal and may contain initial
                // records. Repeated/interior zero spans are ambiguous here.
                else if (!closes.empty()) return false;
                closes.push_back(close);
                previous = close;
            }
            // A single positive session retains the old budget, even when
            // preceded by a zero-span entry. It is not a multi-session gain.
            if (positive < 2 || !Equal(previous, maximum)) return false;
            closes_.swap(closes);
            valid_ = true;
        } catch (...) {
            // Failure to inspect optional metadata must not poison a reader.
            valid_ = false;
        }
        return valid_;
    }

    bool Defer(FsdbSessionReclaimPosition &position, const fsdbTag64 &next,
               std::size_t max_sessions = 1) const {
        // Coalesce at most two metadata spans after the original progress
        // budget fires. This bounds deferral, not FFR's physical allocation:
        // a page can demand-load beyond a close before this check runs.
        if (!valid_ || !max_sessions || max_sessions > 2 ||
            Less(position.anchor, minimum_) ||
            Less(next, position.anchor) || Less(closes_.back(), next)) return false;
        // The anchor is the cursor's initial or most recently restored tag.
        // Across pages/reclaims this index only advances: no full-table scan.
        while (position.session < closes_.size() &&
               Less(closes_[position.session], position.anchor)) ++position.session;
        if (position.session >= closes_.size()) return false;
        const std::size_t remaining = closes_.size() - position.session;
        const std::size_t span = max_sessions < remaining ? max_sessions : remaining;
        return !Less(closes_[position.session + span - 1], next);
    }
};

#endif

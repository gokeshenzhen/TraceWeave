// The build harness supplies an immutable, instrumented wrapper copy.
#include TRACEWEAVE_BENCH_WRAPPER
#include <sys/resource.h>

static double elapsed(_ProfileClock::time_point start) {
    return std::chrono::duration<double,std::milli>(_ProfileClock::now()-start).count();
}
static void require(bool value,const char *label) {
    if (!value) { fprintf(stderr,"probe failure: %s\n",label); exit(20); }
}
static unsigned long long rss_kib() {
    FILE *f=fopen("/proc/self/status","r"); char line[256]; unsigned long long value=0;
    if (f) { while (fgets(line,sizeof(line),f)) if (sscanf(line,"VmRSS: %llu",&value)==1) break; fclose(f); }
    return value;
}
static void libraries() {
    FILE *f=fopen("/proc/self/maps","r"); char line[8192];
    if (!f) return;
    std::set<std::string> paths;
    while (fgets(line,sizeof(line),f)) {
        if (!strstr(line,"libnffr") && !strstr(line,"libnsys") && !strstr(line,"libz.so")) continue;
        char *path=strchr(line,'/'); if (!path) continue;
        path[strcspn(path,"\n")]=0; paths.insert(path);
    }
    fclose(f);
    for (auto &path:paths) printf("{\"phase\":\"library\",\"path\":\"%s\"}\n",path.c_str());
}
int main(int argc,char **argv) {
    if (argc!=8) return 2;
    auto started=_ProfileClock::now();
    auto ctx=(FsdbCtx*)fsdb_open(argv[1]); require(ctx,"open");
    double open_ms=elapsed(started);
    libraries();
    auto found=ctx->path_to_sig.find(argv[2]); require(found!=ctx->path_to_sig.end(),"signal");
    auto sig=&found->second;
    unsigned long long start=strtoull(argv[3],nullptr,10),end=strtoull(argv[4],nullptr,10);
    unsigned long long expected=strtoull(argv[5],nullptr,10);
    unsigned repeats=strtoul(argv[6],nullptr,10); std::string mode=argv[7];
    struct Session { unsigned long long start_tick,close_tick,start_ps,close_ps; };
    std::vector<Session> layout;
    unsigned sessions=0; unsigned long long max_span=0,min_span=~0ULL;
    for (auto item=ctx->obj->ffrGetSessionListInfo();item;item=item->next) {
        require(sessions<1048576,"bounded session metadata");
        ++sessions;
        Session row={};
        unsigned long long a=_TagToPs(ctx,*(fsdbTag64*)&item->start_xtag,&row.start_tick);
        unsigned long long b=_TagToPs(ctx,*(fsdbTag64*)&item->close_xtag,&row.close_tick);
        require(row.close_tick>=row.start_tick,"ordered session boundary");
        row.start_ps=a;row.close_ps=b;layout.push_back(row);
        max_span=std::max(max_span,b-a); min_span=std::min(min_span,b-a);
    }
    printf("{\"phase\":\"metadata\",\"open_ms\":%.6f,\"sessions\":%u,\"largest_session_ps\":%llu,\"smallest_session_ps\":%llu,\"width\":%u,\"scale_fs\":%llu,\"duration_ps\":%llu,\"session_layout\":[",open_ms,sessions,max_span,min_span,sig->bit_size,ctx->scale_fs,fsdb_get_end_time(ctx));
    for (unsigned i=0;i<layout.size();++i) {
        const auto &row=layout[i];
        printf("%s{\"start_tick\":%llu,\"close_tick\":%llu,\"start_ps\":%llu,\"close_ps\":%llu}",i ? "," : "",row.start_tick,row.close_tick,row.start_ps,row.close_ps);
    }
    printf("]}\n"); fflush(stdout);
    if (mode=="info") { fsdb_close(ctx); return 0; }
    for (unsigned repeat=0;repeat<repeats;++repeat) {
        bench_reclaims=bench_reclaim_ns=0;
        started=_ProfileClock::now(); const char *paths[]={argv[2]};
        require(fsdb_begin_lazy_transition_group_v1(ctx,paths,1,nullptr)==1,"group");
        unsigned long long cursor=0;
        require(fsdb_event_open_v1(ctx,argv[2],start,end,&cursor)==0,"cursor");
        double seek_ms=elapsed(started);
        started=_ProfileClock::now(); unsigned long long count=0,loaded_peak=0;
        while (true) {
            FsdbEventPageV1 page;
            require(fsdb_event_count_v1(ctx,cursor,65536,&page,sizeof(page))==0,"count");
            require(!page.truncated && (page.events || page.complete),"progress");
            count+=page.events; loaded_peak=std::max(loaded_peak,(unsigned long long)ctx->obj->ffrGetVCCount(sig->idcode));
            if (page.complete) break;
        }
        double scan_ms=elapsed(started); require(count==expected,"exact count oracle");
        started=_ProfileClock::now(); require(fsdb_event_close_v1(ctx,cursor)==0,"cursor close");
        require(fsdb_end_transition_group(ctx,nullptr)==0,"group close");
        double cleanup_ms=elapsed(started); struct rusage usage={}; getrusage(RUSAGE_SELF,&usage);
        printf("{\"phase\":\"count\",\"repeat\":%u,\"seek_ms\":%.6f,\"scan_ms\":%.6f,\"cleanup_ms\":%.6f,\"elapsed_ms\":%.6f,\"reclaim_calls\":%llu,\"reclaim_ms\":%.6f,\"records\":%llu,\"observed_loaded_peak\":%llu,\"peak_rss_kib\":%ld}\n",repeat,seek_ms,scan_ms,cleanup_ms,seek_ms+scan_ms+cleanup_ms,bench_reclaims,bench_reclaim_ns/1e6,count,loaded_peak,usage.ru_maxrss); fflush(stdout);
    }
    started=_ProfileClock::now(); fsdb_close(ctx);
    printf("{\"phase\":\"close\",\"elapsed_ms\":%.6f,\"rss_kib\":%llu}\n",elapsed(started),rss_kib());
}

// Deterministic local FSDB benchmark inputs; never simulate or overwrite data.
#include "ffwAPI.h"
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#include <sys/stat.h>

struct Signal {
    std::vector<byte_T> bits;
    ffwVarMapId id;
    Signal(ffwObject *writer, const std::string &name, unsigned width,
           fsdbVarDir direction = FSDB_VD_IMPLICIT) : bits(width, FSDB_BT_VCD_0) {
        id = ffw_CreateVarByHandle(writer, FSDB_VT_VCD_REG, direction,
            FSDB_DT_HANDLE_VERILOG_STANDARD, width - 1, 0, bits.data(),
            const_cast<char *>(name.c_str()), FSDB_BYTES_PER_BIT_1B);
        if (!id) std::exit(5);
    }
    void put(ffwObject *writer, uint64_t value, int unknown = -1) {
        for (unsigned i = 0; i < bits.size(); ++i)
            bits[i] = unknown >= 0 ? byte_T(unknown) :
                ((value >> ((bits.size() - i - 1) % 64)) & 1 ? FSDB_BT_VCD_1 : FSDB_BT_VCD_0);
        ffw_CreateVarValueByVarMapId(writer, id, bits.data());
    }
};

static void at(ffwObject *writer, uint64_t tick) {
    ffw_CreateXCoorByHnL(writer, tick >> 32, tick & 0xffffffff);
}
static void scope(ffwObject *writer, const std::string &name) {
    ffw_CreateScope(writer, FSDB_ST_VCD_MODULE, const_cast<char *>(name.c_str()));
}
struct Vr {
    Signal valid, ready, data;
    Vr(ffwObject *w) : valid(w,"valid_o",1,FSDB_VD_OUTPUT),
        ready(w,"ready_i",1,FSDB_VD_INPUT), data(w,"data_o[31:0]",32,FSDB_VD_OUTPUT) {}
    void put(ffwObject *w, uint64_t cycle, unsigned index) {
        unsigned phase = (cycle + 32) % 64;
        bool drop = index % 3 == 2 && phase == 40;
        int unknown = index == 4 && (phase == 4 || phase == 5)
            ? (phase == 4 ? FSDB_BT_VCD_X : FSDB_BT_VCD_Z) : -1;
        valid.put(w, !drop && !(index == 0 && phase < 4), unknown);
        ready.put(w, phase < 24 || phase >= 48);
        data.put(w, ((cycle + 32) / 64) * 2 + (index % 3 == 1 && phase >= 32));
    }
};
struct Ahb {
    Signal clock, htrans, ready, address, write, size, burst, prot, data;
    Ahb(ffwObject *w) : clock(w,"hclk",1), htrans(w,"htrans_o[1:0]",2,FSDB_VD_OUTPUT),
        ready(w,"hready_i",1,FSDB_VD_INPUT), address(w,"haddr_o[31:0]",32,FSDB_VD_OUTPUT),
        write(w,"hwrite_o",1,FSDB_VD_OUTPUT), size(w,"hsize_o[2:0]",3,FSDB_VD_OUTPUT),
        burst(w,"hburst_o[2:0]",3,FSDB_VD_OUTPUT), prot(w,"hprot_o[3:0]",4,FSDB_VD_OUTPUT),
        data(w,"hwdata_o[31:0]",32,FSDB_VD_OUTPUT) {}
    void put(ffwObject *w, uint64_t cycle, unsigned index) {
        unsigned phase = (cycle + 32) % 64;
        htrans.put(w,2); ready.put(w,phase < 24 || phase >= 48);
        address.put(w,((cycle + 32) / 64) * 8 + (index && phase >= 32 ? 4 : 0),
                    index && phase == 8 ? FSDB_BT_VCD_X : -1);
        write.put(w,1); size.put(w,2); burst.put(w,0); prot.put(w,3);
        data.put(w,index ? ((cycle + 32) / 64) * 8 + (phase >= 32 ? 4 : 0) : 0);
    }
};

int main(int argc, char **argv) {
    if (argc != 5 && argc != 6) { std::fprintf(stderr,"usage: fixture OUTPUT dense|wide|wide64|wide256|wide1024|protocol STEPS SYNC_EVERY [MEMORY_MIB]\n"); return 2; }
    struct stat info;
    if (stat(argv[1],&info) == 0) return 3;
    std::string mode=argv[2]; uint64_t steps=std::strtoull(argv[3],nullptr,10);
    uint64_t sync=std::strtoull(argv[4],nullptr,10);
    bool wide=mode=="wide" || mode=="wide64" || mode=="wide256" || mode=="wide1024";
    if (!steps || (mode!="dense" && !wide && mode!="protocol")) return 4;
    ffwObject *w=ffw_Open(argv[1],FSDB_FT_VERILOG); if (!w) return 5;
    ffw_SetVCMemLimit(w,argc == 6 ? std::strtoul(argv[5],nullptr,10) : 128);
    ffw_SetScaleUnit(w,(char*)"1ps"); ffw_SetGlitchNum(w,0);
    ffw_CreateTreeByHandleScheme(w); ffw_BeginTree(w); scope(w,"top");
    if (mode!="protocol") {
        unsigned width=mode=="wide64" ? 64 : mode=="wide1024" ? 1024 : wide ? 256 : 32;
        Signal clk(w,"clk",1), data(w,"data["+std::to_string(width-1)+":0]",width), sparse(w,"sparse",1);
        ffw_CreateUpscope(w); ffw_EndTree(w);
        at(w,0); clk.put(w,0); data.put(w,0); sparse.put(w,0);
        const uint64_t offset=wide ? 0 : 1000000000000ULL;
        const uint64_t stride=wide ? 10 : 1;
        for (uint64_t i=1;i<=steps;++i) {
            at(w,offset+i*stride); clk.put(w,i&1);
            data.put(w,uint32_t(i)*uint32_t(2654435761U),wide && (i==5 || i==6) ?
                (i==5 ? FSDB_BT_VCD_X : FSDB_BT_VCD_Z) : -1);
            if (i==steps/2) sparse.put(w,1);
            if (sync && i%sync==0) ffw_Sync(w);
        }
        at(w,2*offset+steps*stride); sparse.put(w,0);
    } else {
        scope(w,"fast"); Signal fast(w,"clk",1);
        std::vector<Vr*> vr;
        for (unsigned i=0;i<6;++i) { scope(w,"vr"+std::to_string(i)); vr.push_back(new Vr(w)); ffw_CreateUpscope(w); }
        std::vector<Ahb*> ahb;
        for (unsigned i=0;i<2;++i) { scope(w,"m_if"+std::to_string(i)); ahb.push_back(new Ahb(w)); ffw_CreateUpscope(w); }
        ffw_CreateUpscope(w); scope(w,"slow"); Signal slow(w,"clk",1);
        for (unsigned i=6;i<8;++i) { scope(w,"vr"+std::to_string(i)); vr.push_back(new Vr(w)); ffw_CreateUpscope(w); }
        ffw_CreateUpscope(w); ffw_CreateUpscope(w); ffw_EndTree(w);
        for (uint64_t i=0;i<steps;++i) {
            at(w,20*i); fast.put(w,0); slow.put(w,i&1);
            for (unsigned j=0;j<6;++j) vr[j]->put(w,i,j);
            if (!(i&1)) for (unsigned j=6;j<8;++j) vr[j]->put(w,i/2,j);
            for (unsigned j=0;j<2;++j) { ahb[j]->clock.put(w,0); ahb[j]->put(w,i,j); }
            at(w,20*i+10); fast.put(w,1);
            for (auto bus:ahb) bus->clock.put(w,1);
            if (sync && (i+1)%sync==0) ffw_Sync(w);
        }
        at(w,20*steps); fast.put(w,0); slow.put(w,0);
        for (auto bus:ahb) { bus->clock.put(w,0); delete bus; }
        for (auto bus:vr) delete bus;
    }
    ffw_Close(w); ffw_Destroy(w);
    std::printf("{\"complete\":true,\"mode\":\"%s\",\"steps\":%llu}\n",mode.c_str(),(unsigned long long)steps);
}

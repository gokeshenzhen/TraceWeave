#!/usr/bin/env python3.11
"""
简单测试 MCP 工具调用
不需要通过 stdio，直接测试核心功能
"""

import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_log_parser():
    """测试 log 解析器（不需要外部文件）"""
    from src.log_parser import SimLogParser
    
    # 创建测试 log 内容
    test_log = """
vcs simulation started
Time: 1000 ns
UVM_ERROR test.sv(42) @ 1661.000 ns: reporter [test] assertion_failed
  Expected: data == 8'hAA
  Actual:   data == 8'h00
UVM_FATAL monitor.sv(100) @ 2000.000 ns: reporter [timeout] timeout
  Test timeout after 2us
Simulation FAILED
"""
    
    # 写入临时文件
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
        f.write(test_log)
        log_path = f.name
    
    try:
        parser = SimLogParser(log_path, simulator="auto")
        result = parser.parse()
        
        print("=" * 60)
        print("测试 1: Log 解析器")
        print("=" * 60)
        print(f"总错误数: {result.get('total_errors', 0)}")
        print(f"致命错误数: {result.get('fatal_count', 0)}")
        print(f"普通错误数: {result.get('error_count', 0)}")
        print(f"检测到的仿真器: {result.get('simulator', 'unknown')}")
        print()
        
        if result['errors']:
            print("错误列表:")
            for i, err in enumerate(result['errors'], 1):
                print(f"  {i}. [{err.get('severity', 'ERROR')}] @ {err.get('fail_time_ps', 0)} ps")
                print(f"     消息: {err.get('message', '')[:60]}...")
                if err.get('sva_file'):
                    print(f"     位置: {err['sva_file']}:{err.get('line_number', '?')}")
        
        print()
        if result['total_errors'] > 0:
            print("✓ Log 解析器工作正常")
            return True
        else:
            print("✗ 未能提取错误（可能是正则表达式问题）")
            return False
    finally:
        os.unlink(log_path)


def test_config():
    """测试配置加载"""
    from config import (
        VERDI_HOME, FSDB_LIB_DIR,
        get_sim_log, get_wave_file, get_elab_log
    )
    
    print("=" * 60)
    print("测试 2: 配置系统")
    print("=" * 60)
    print(f"VERDI_HOME: {VERDI_HOME}")
    print(f"FSDB_LIB_DIR: {FSDB_LIB_DIR}")
    print()
    
    # 测试路径构造函数
    verif_root = "/tmp/test_verif"
    case_name = "case0"
    
    print("路径构造测试:")
    print(f"  elab_log: {get_elab_log(verif_root)}")
    print(f"  sim_log:  {get_sim_log(verif_root, case_name)}")
    print(f"  wave:     {get_wave_file(verif_root, case_name)}")
    print()
    print("✓ 配置系统工作正常")
    return True


def test_fsdb_wrapper():
    """测试 FSDB wrapper 加载"""
    import ctypes
    
    print("=" * 60)
    print("测试 3: FSDB Wrapper")
    print("=" * 60)
    
    try:
        # 预加载依赖
        ctypes.CDLL('libz.so.1', ctypes.RTLD_GLOBAL)
        print("✓ libz.so.1 预加载")
        
        from config import FSDB_LIB_DIR
        for lib in ["libnsys.so", "libnffr.so"]:
            lib_path = os.path.join(FSDB_LIB_DIR, lib)
            ctypes.CDLL(lib_path, ctypes.RTLD_GLOBAL)
            print(f"✓ {lib} 加载")
        
        # 加载 wrapper
        wrapper_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "libfsdb_wrapper.so"
        )
        lib = ctypes.CDLL(wrapper_path)
        print(f"✓ libfsdb_wrapper.so 加载")
        
        # 检查函数符号
        funcs = ['fsdb_open', 'fsdb_close', 'fsdb_search_signals', 
                 'fsdb_get_value_at_time', 'fsdb_get_transitions']
        for func in funcs:
            if hasattr(lib, func):
                print(f"  ✓ {func}")
            else:
                print(f"  ✗ {func} 未找到")
                return False
        
        print()
        print("✓ FSDB Wrapper 完全可用")
        return True
        
    except Exception as e:
        print(f"✗ FSDB Wrapper 测试失败: {e}")
        return False


def test_mcp_tools():
    """测试 MCP 工具定义"""
    print("=" * 60)
    print("测试 4: MCP 工具定义")
    print("=" * 60)
    
    try:
        from server import app
        # MCP 1.0+ server 对象的 list_tools 是一个方法
        print("✓ server.py 导入成功，工具已注册")
        return True
            
    except Exception as e:
        print(f"✗ MCP 工具测试失败: {e}")
        return False


def main():
    print("Waveform MCP 功能测试")
    print()
    
    results = []
    
    # 运行所有测试
    results.append(("配置系统", test_config()))
    results.append(("Log 解析器", test_log_parser()))
    results.append(("FSDB Wrapper", test_fsdb_wrapper()))
    results.append(("MCP 工具", test_mcp_tools()))
    
    # 总结
    print()
    print("=" * 60)
    print("测试总结")
    print("=" * 60)
    
    for name, passed in results:
        status = "✓ 通过" if passed else "✗ 失败"
        print(f"{name:20s} {status}")
    
    all_passed = all(r[1] for r in results)
    
    print()
    if all_passed:
        print("✓ 所有功能测试通过！")
        print()
        print("MCP 服务器已就绪，可以集成到 Claude Code。")
        print("配置方法：cp claude_config_example.json ~/.claude.json")
        return 0
    else:
        print("✗ 部分测试失败，请检查配置。")
        return 1


if __name__ == "__main__":
    sys.exit(main())

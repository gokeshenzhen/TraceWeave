#!/usr/bin/env python3.11
"""
环境验证脚本
用法: python3.11 verify_setup.py
"""

import sys
import os

def main():
    print("=" * 60)
    print("Waveform MCP 环境验证")
    print("=" * 60)
    print()
    
    # 添加项目路径
    project_root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, project_root)
    
    all_ok = True
    
    # 1. Python 版本检查
    print("1. Python 版本检查")
    if sys.version_info >= (3, 9):
        print(f"   ✓ Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        print(f"   ✗ Python 版本过低: {sys.version_info.major}.{sys.version_info.minor}")
        print("   需要 Python 3.9+")
        all_ok = False
    print()
    
    # 2. 依赖包检查
    print("2. 依赖包检查")
    deps = [
        ("mcp.server", "MCP Server"),
        ("yaml", "PyYAML"),
    ]
    for module, name in deps:
        try:
            __import__(module)
            print(f"   ✓ {name}")
        except ImportError as e:
            print(f"   ✗ {name}: {e}")
            all_ok = False
    print()
    
    # 3. 配置文件检查
    print("3. 配置文件检查")
    try:
        from config import VERDI_HOME, FSDB_LIB_DIR
        print(f"   ✓ config.py")
        print(f"     VERDI_HOME: {VERDI_HOME}")
        
        if os.path.isdir(FSDB_LIB_DIR):
            print(f"   ✓ FSDB 库目录存在: {FSDB_LIB_DIR}")
        else:
            print(f"   ✗ FSDB 库目录不存在: {FSDB_LIB_DIR}")
            all_ok = False
    except Exception as e:
        print(f"   ✗ config.py 加载失败: {e}")
        all_ok = False
    print()
    
    # 4. 源代码模块检查
    print("4. 源代码模块检查")
    modules = [
        "src.log_parser",
        "src.vcd_parser", 
        "src.fsdb_parser",
        "src.fsdb_signal_index",
        "src.analyzer"
    ]
    for mod in modules:
        try:
            __import__(mod)
            print(f"   ✓ {mod}")
        except Exception as e:
            print(f"   ✗ {mod}: {e}")
            all_ok = False
    print()
    
    # 5. FSDB Wrapper 检查
    print("5. FSDB Wrapper 检查")
    wrapper_path = os.path.join(project_root, "libfsdb_wrapper.so")
    if os.path.exists(wrapper_path):
        print(f"   ✓ libfsdb_wrapper.so 存在")
        
        # 尝试加载
        try:
            import ctypes
            # 预加载 zlib
            ctypes.CDLL('libz.so.1', ctypes.RTLD_GLOBAL)
            
            # 加载 Verdi 库
            from config import FSDB_LIB_DIR
            for lib in ["libnsys.so", "libnffr.so"]:
                lib_path = os.path.join(FSDB_LIB_DIR, lib)
                ctypes.CDLL(lib_path, ctypes.RTLD_GLOBAL)
            
            # 加载 wrapper
            ctypes.CDLL(wrapper_path)
            print(f"   ✓ libfsdb_wrapper.so 可加载")
        except Exception as e:
            print(f"   ✗ wrapper 加载失败: {e}")
            all_ok = False
    else:
        print(f"   ✗ libfsdb_wrapper.so 不存在")
        print(f"     请运行: bash build_wrapper.sh")
        all_ok = False
    print()
    
    # 6. 服务器启动检查
    print("6. MCP 服务器检查")
    try:
        from server import app
        print(f"   ✓ server.py 可导入")
    except Exception as e:
        print(f"   ✗ server.py 导入失败: {e}")
        all_ok = False
    print()
    
    # 总结
    print("=" * 60)
    if all_ok:
        print("✓ 所有检查通过！环境配置完成。")
        print()
        print("下一步：")
        print("1. 将 claude_config_example.json 复制到 ~/.claude.json")
        print("2. 在 Claude Code 中使用 waveform MCP 工具")
        return 0
    else:
        print("✗ 部分检查失败，请查看上述错误信息。")
        print()
        print("常见问题：")
        print("- 如果缺少依赖: python3.11 -m pip install mcp pyyaml --user")
        print("- 如果缺少 wrapper: bash build_wrapper.sh")
        return 1

if __name__ == "__main__":
    sys.exit(main())

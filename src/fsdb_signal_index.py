"""
fsdb_signal_index.py
信号索引管理器：利用 C++ Wrapper 内部的 Scope 树实现快速搜索。
该模块主要解决在大规模芯片设计中，快速找到信号完整路径的问题。
"""

from .fsdb_parser import FSDBParser
from config import SIGNAL_SEARCH_MAX_RESULTS

class FSDBSignalIndex:
    """
    薄封装类，主要用于在 server.py 中实现单例或缓存机制，
    避免同一个波形文件被反复索引。
    """

    def __init__(self, fsdb_path: str):
        """关联一个 FSDB 解析器实例"""
        self._parser = FSDBParser(fsdb_path)

    def search(self, keyword: str,
               max_results: int = SIGNAL_SEARCH_MAX_RESULTS) -> dict:
        """根据关键字搜索信号全名（如输入 'data' 找到 'top_tb.dut.data'）"""
        return self._parser.search_signals(keyword, max_results)

    def list_top_scopes(self) -> dict:
        """列出波形中顶层的模块实例名（Scope），帮助 AI 理解设计结构"""
        # 通过搜索空字符串获取前 10000 个信号，然后提取其最顶层前缀
        result = self._parser.search_signals("", max_results=10000)
        top_scopes = list({
            item["path"].split(".")[0]
            for item in result["results"]
            if "." in item["path"]
        })
        return {
            "top_scopes":            sorted(top_scopes),
            "total_signals_indexed": result["total_matched"],
        }

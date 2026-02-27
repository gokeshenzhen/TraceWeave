"""
fsdb_signal_index.py
信号路径搜索 — 直接复用 FSDBParser.search_signals
（wrapper 内部已通过 scope 树建索引，不读 VC，GB 级文件友好）
"""

from .fsdb_parser import FSDBParser
from config import SIGNAL_SEARCH_MAX_RESULTS


class FSDBSignalIndex:
    """薄封装，供 server.py 缓存使用"""

    def __init__(self, fsdb_path: str):
        self._parser = FSDBParser(fsdb_path)

    def search(self, keyword: str,
               max_results: int = SIGNAL_SEARCH_MAX_RESULTS) -> dict:
        return self._parser.search_signals(keyword, max_results)

    def list_top_scopes(self) -> dict:
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

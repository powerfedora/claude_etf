"""Tushare 官方 MCP HTTP 客户端 (https://api.tushare.pro/mcp/)"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "tushare_mcp.json"

MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0


class TushareMcpError(RuntimeError):
    pass


def load_mcp_url() -> str:
    url = os.environ.get("TUSHARE_MCP_URL", "").strip()
    if url:
        return url
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        url = (cfg.get("mcp_url") or cfg.get("url") or "").strip()
        if url:
            return url
    raise TushareMcpError(
        f"未配置 Tushare MCP。请设置环境变量 TUSHARE_MCP_URL，"
        f"或在 {CONFIG_FILE.name} 中写入 {{\"mcp_url\": \"https://api.tushare.pro/mcp/?token=你的token\"}}"
    )


def to_ts_code(code: str) -> str:
    code = code.zfill(6)
    suffix = "SH" if code.startswith(("5", "6")) else "SZ"
    return f"{code}.{suffix}"


class TushareMcpClient:
    def __init__(self, mcp_url: Optional[str] = None):
        self.mcp_url = mcp_url or load_mcp_url()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = False
        self._req_id = 0

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        arguments = arguments or {}
        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                return self._call_tool_once(name, arguments)
            except TushareMcpError as e:
                last_err = e
                msg = str(e)
                if "没有接口" in msg or "没有访问权限" in msg:
                    raise
                if "频率超限" not in msg and attempt + 1 >= MAX_RETRIES:
                    raise
                if attempt + 1 >= MAX_RETRIES:
                    raise
                if "频率超限" in msg:
                    wait = 65.0 + random.uniform(0, 2.0)
                else:
                    wait = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.8)
                time.sleep(wait)
        raise last_err  # pragma: no cover

    def _parse_sse_response(self, text: str) -> dict:
        chunks: List[str] = []
        in_data = False
        for line in text.splitlines():
            if line.startswith("event:"):
                continue
            if line.startswith("data:"):
                chunks.append(line[5:].lstrip())
                in_data = True
            elif in_data:
                chunks.append(line)
        if not chunks:
            raise TushareMcpError("MCP 无有效响应")
        return json.loads("".join(chunks))

    def _call_tool_once(self, name: str, arguments: Dict[str, Any]) -> Any:
        self._req_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        resp = self.session.post(
            self.mcp_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            timeout=60,
        )
        resp.raise_for_status()
        # 服务端返回 UTF-8，但 Content-Type 未声明 charset，requests 会误用 ISO-8859-1
        body = resp.content.decode("utf-8")
        msg = self._parse_sse_response(body)
        if "error" in msg:
            raise TushareMcpError(msg["error"])
        result = msg.get("result") or {}
        text = (result.get("content") or [{}])[0].get("text", "")
        if result.get("isError") or (isinstance(text, str) and text.startswith("tushare API 错误")):
            raise TushareMcpError(text or "Tushare MCP 调用失败")
        if text == "":
            return []
        return json.loads(text)

    def fetch_fund_adj(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        rows: List[dict] = self.call_tool(
            "fund_adj",
            {
                "ts_code": to_ts_code(code),
                "start_date": start_date,
                "end_date": end_date,
                "fields": ["trade_date", "adj_factor"],
            },
        )
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d")
        df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce")
        return df[["date", "adj_factor"]].sort_values("date").dropna().reset_index(drop=True)

    @staticmethod
    def apply_qfq(df: pd.DataFrame, adj: pd.DataFrame) -> pd.DataFrame:
        """前复权, 与同花顺默认一致: price * adj_factor / latest_adj_factor"""
        if adj is None or adj.empty:
            return df
        merged = df.merge(adj, on="date", how="left")
        merged["adj_factor"] = merged["adj_factor"].ffill().bfill()
        factor = merged["adj_factor"] / merged["adj_factor"].iloc[-1]
        out = df.copy()
        for col in ("open", "high", "low", "close"):
            out[col] = merged[col] * factor
        return out

    def fetch_fund_daily(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        rows: List[dict] = self.call_tool(
            "fund_daily",
            {
                "ts_code": to_ts_code(code),
                "start_date": start_date,
                "end_date": end_date,
                "fields": ["trade_date", "open", "high", "low", "close", "vol"],
            },
        )
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df = df.rename(columns={"trade_date": "date", "vol": "volume"})
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("date").dropna().reset_index(drop=True)
        if adjust == "qfq":
            adj = self.fetch_fund_adj(code, start_date, end_date)
            df = self.apply_qfq(df, adj)
        return df

    def fetch_stock_daily(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        ts_code = to_ts_code(code)
        rows: List[dict] = self.call_tool(
            "daily",
            {
                "ts_code": ts_code,
                "start_date": start_date,
                "end_date": end_date,
                "fields": ["trade_date", "open", "high", "low", "close", "vol"],
            },
        )
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df = df.rename(columns={"trade_date": "date", "vol": "volume"})
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("date").dropna().reset_index(drop=True)
        if adjust == "qfq":
            adj_rows: List[dict] = self.call_tool(
                "adj_factor",
                {
                    "ts_code": ts_code,
                    "start_date": start_date,
                    "end_date": end_date,
                    "fields": ["trade_date", "adj_factor"],
                },
            )
            if adj_rows:
                adj = pd.DataFrame(adj_rows)
                adj["date"] = pd.to_datetime(adj["trade_date"], format="%Y%m%d")
                adj["adj_factor"] = pd.to_numeric(adj["adj_factor"], errors="coerce")
                adj = adj[["date", "adj_factor"]].sort_values("date").dropna().reset_index(drop=True)
                df = self.apply_qfq(df, adj)
        return df

    def fetch_index_daily(
        self,
        ts_code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        rows: List[dict] = self.call_tool(
            "index_daily",
            {
                "ts_code": ts_code,
                "start_date": start_date,
                "end_date": end_date,
                "fields": ["trade_date", "open", "high", "low", "close", "vol"],
            },
        )
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df = df.rename(columns={"trade_date": "date", "vol": "volume"})
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.sort_values("date").dropna().reset_index(drop=True)

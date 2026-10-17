# -*- coding: utf-8 -*-
"""
Общее: пути к файлам, логгер, утилиты IO, форматтеры, табличная модель,
поток-воркер для Dexscreener с мягким анти-429.
"""

import os, json, time, random
from datetime import datetime
from typing import Dict, Any, List, Optional, Iterable
from urllib.request import getproxies
from urllib.parse import urlsplit, urlunsplit

import requests
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QAbstractTableModel
from PyQt5.QtGui import QBrush, QColor

# -------------------- Пути к данным --------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

MEMORY_FILE = os.path.join(DATA_DIR, "memory.json")
PRICE_TRACKER_FILE = os.path.join(DATA_DIR, "price_tracker.json")
ARCHIVE_FILE = os.path.join(DATA_DIR, "archive.json")
STRATEGY_STATE_FILE = os.path.join(DATA_DIR, "strategy_state.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
PRICE_HISTORY_FILE = os.path.join(DATA_DIR, "price_history.jsonl")
HISTORY_STATE_FILE = os.path.join(DATA_DIR, "history_state.json")

TRADING_LOG_S1 = os.path.join(DATA_DIR, "trading_log_s1.json")
TRADING_LOG_S2 = os.path.join(DATA_DIR, "trading_log_s2.json")
TRADING_LOG_S3 = os.path.join(DATA_DIR, "trading_log_s3.json")

# -------------------- Логгер --------------------
def log_event(evt: str, **fields):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    parts = [f"{k}={fields[k]}" for k in sorted(fields.keys())]
    print(f"[{ts}] {evt} | " + " ".join(parts))

# -------------------- IO --------------------
def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log_event("IO_READ_ERROR", file=path, error=str(e))
        return default

def save_json(data, path: str):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        log_event("IO_WRITE_ERROR", file=path, error=str(e))

def append_jsonl(obj: dict, path: str):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as e:
        log_event("IO_APPEND_ERROR", file=path, error=str(e))

# -------------------- Форматтеры --------------------
def fmt_time(iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(iso).strftime('%H:%M:%S')
    except Exception:
        return "---"

def fmt_date(iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(iso).strftime('%Y-%m-%d')
    except Exception:
        return "---"

def pct_cell(value_float: Optional[float]):
    if value_float is None:
        return {"text": "---", "color": None}
    txt = f"{value_float:+.2f}%"
    color = "#0a0" if value_float >= 0 else "#d11"
    return {"text": txt, "color": color}

# -------------------- Табличная модель с цветом --------------------
class CustomTableModel(QAbstractTableModel):
    def __init__(self, data: List[List[Any]], headers: List[str], centered_columns: Iterable[int]):
        super().__init__()
        self._data = data
        self.headers = headers
        self.centered_columns = set(centered_columns)

    def data(self, index, role):
        cell = self._data[index.row()][index.column()]
        if role == Qt.DisplayRole:
            if isinstance(cell, dict):
                return cell.get("text", "")
            return cell
        if role == Qt.TextAlignmentRole and index.column() in self.centered_columns:
            return Qt.AlignCenter
        if role == Qt.ForegroundRole and isinstance(cell, dict):
            c = cell.get("color")
            if c:
                return QBrush(QColor(c))
        return None

    def rowCount(self, _): return len(self._data)
    def columnCount(self, _): return len(self.headers)
    def headerData(self, section, orientation, role):
        if role == Qt.DisplayRole and orientation == 1:
            return self.headers[section]

# -------------------- Воркер Dexscreener --------------------
class Worker(QThread):
    """
    Универсальный воркер.
    - Если fetch_boosts=True: тянет списки бустов (top+latest), фильтрует endswith("pump").
    - Всегда тянет детали по address_to_track.
    """
    completed = pyqtSignal(dict)

    def __init__(self, addresses_to_track: Iterable[str], fetch_boosts: bool = True, parent=None):
        super().__init__(parent)
        self.addresses_to_track = set(a for a in addresses_to_track if isinstance(a, str))
        self.fetch_boosts = fetch_boosts
        self.session = requests.Session()
        # Локальный HTTP-прокси использует CONNECT для HTTPS-сайтов.
        # Windows может ошибочно обозначать сам прокси схемой https://.
        self.proxies = getproxies()
        for protocol, proxy_url in list(self.proxies.items()):
            parsed = urlsplit(proxy_url)
            if parsed.scheme == "https" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
                self.proxies[protocol] = urlunsplit(parsed._replace(scheme="http"))

    def run(self):
        boosted = {}
        if self.fetch_boosts:
            boosted = self._safe_get_boosts()

        # Детали по адресам (включая адреса из бустов)
        addrs = set(self.addresses_to_track)
        addrs.update(a for a in boosted.keys() if isinstance(a, str))
        details = self._get_many_details(addrs)

        self.completed.emit({"boosts": boosted, "details": details})

    # ---- boosts ----
    def _safe_get(self, url: str, timeout=10, retries=3, backoff=0.8):
        last_err = None
        for i in range(retries):
            try:
                r = self.session.get(url, timeout=timeout, proxies=self.proxies)
                if r.status_code == 429:
                    sleep_s = backoff * (i + 1) + random.uniform(0, 0.5)
                    time.sleep(sleep_s)
                    continue
                r.raise_for_status()
                return r
            except Exception as e:
                last_err = e
                time.sleep(backoff * (i + 1))
        if last_err:
            raise last_err

    def _safe_get_boosts(self) -> Dict[str, dict]:
        all_tokens: Dict[str, dict] = {}
        try:
            r_top = self._safe_get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10)
            for t in r_top.json():
                a = t.get("tokenAddress")
                if a: all_tokens[a] = t
        except Exception as e:
            log_event("API_ERROR", where="get_boosted_tokens_combined(top)", error=str(e))

        try:
            r_latest = self._safe_get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
            for t in r_latest.json():
                a = t.get("tokenAddress")
                if a: all_tokens[a] = t
        except Exception as e:
            log_event("API_ERROR", where="get_boosted_tokens_combined(latest)", error=str(e))

        # Только pump.fun
        result = {a: t for a, t in all_tokens.items() if isinstance(a, str) and a.endswith("pump")}
        return result

    # ---- details ----
    def _get_detail_one(self, addr: str) -> Optional[dict]:
        try:
            r = self._safe_get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=6, retries=3)
            j = r.json()
            pairs = j.get("pairs") or []
            return pairs[0] if pairs else None
        except Exception as e:
            log_event("API_ERROR", where="get_token_details", address=addr, error=str(e))
            return None

    def _get_many_details(self, addrs: Iterable[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for addr in addrs:
            d = self._get_detail_one(addr)
            if d:
                out[addr] = d
            time.sleep(0.05 + random.uniform(0, 0.05))
        return out

# -*- coding: utf-8 -*-
"""
История цен (сек) — GeckoTerminal (Standalone)
"""

import os, sys, json, time, pathlib, threading, random
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from typing import Set

# ---- Qt plugins (Windows safe) ----
try:
    import PyQt5

    os.environ.pop("QT_PLUGIN_PATH", None)
    os.environ.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    qt_plugins = pathlib.Path(PyQt5.__file__).parent / "Qt5" / "plugins" / "platforms"
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(qt_plugins)
except Exception:
    pass

import requests

from PyQt5.QtCore import Qt, QAbstractTableModel, QModelIndex, QTimer, QSize, QRectF, pyqtSignal, QItemSelectionModel, \
    QObject
from PyQt5.QtGui import QColor, QPen, QPainter, QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QTableView, QHeaderView, QMenu, QMessageBox, QTabWidget,
    QScrollArea, QGroupBox, QFormLayout, QSpinBox, QDoubleSpinBox, QProgressBar,
    QStyledItemDelegate
)

# ИМПОРТ НОВОГО ДВИЖКА
from candle_engine import CandleService, get_request_proxies

APP_NAME = "История цен (сек) — GeckoTerminal"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

PRICE_HISTORY_FILE = os.path.join(DATA_DIR, "price_history.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "price_history_state.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "price_history_settings.json")

# ---- источники ----
DEX_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{address}"

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
GECKO_POOLS = GECKO_BASE + "/networks/solana/tokens/{address}/pools?per_page=10"
GECKO_OHLCV = GECKO_BASE + "/networks/solana/pools/{pool}/ohlcv/minute"


# ---- утилиты ----
def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(evt, **fields):
    ts = now_str()
    parts = [f"{k}={fields[k]}" for k in sorted(fields.keys())]
    print(f"[{ts}] {evt} | " + " ".join(parts))


def parse_ts(x):
    if x is None: return None
    if isinstance(x, (int, float)): return int(x)
    if isinstance(x, str):
        s = x.strip().replace("T", " ")
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except Exception:
                continue
        try:
            return int(s)
        except Exception:
            return None
    return None


def iso_utc_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ensure_json_file(path, default_obj):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_obj, f, ensure_ascii=False, indent=2)
        return default_obj
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_obj, f, ensure_ascii=False, indent=2)
        return default_obj


# ---- настройки ----
DEFAULT_SETTINGS = {
    "live_pull_interval_sec": 30,
    "live_recent_limit": 5,
    "pause_live_while_batch": True,
    "gecko_rps_limit": 2.0,
    "gecko_max_retries": 6,
    "gecko_backoff_base": 1.4,
    "dex_rps_limit": 3.0,
    "dex_max_retries": 5,
    "use_dex_details_for_name_fdv": True,
    "batch_page_limit": 1000,
}
DEFAULT_STATE = {
    "follow_mode": "only_new",
    "paused": False,
    "history_days_limit": 7,
    "full_history": False,
    "column_widths": {},
    "greylist": {}
}


class WorkerSignals(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal()
    error = pyqtSignal(str)


class PaddedItemDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        option.rect = option.rect.adjusted(5, 0, -5, 0)
        super(PaddedItemDelegate, self).paint(painter, option, index)


# ---- модель таблицы ----
class SimpleTableModel(QAbstractTableModel):
    def __init__(self, headers, rows):
        super().__init__()
        self.headers = headers
        self.rows = rows

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole: return None
        if orientation == Qt.Horizontal: return self.headers[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid(): return None
        v = self.rows[index.row()][index.column()]
        if role == Qt.DisplayRole: return v
        return None


class CenteredTableModel(SimpleTableModel):
    def __init__(self, headers, rows, is_active_flags):
        super().__init__(headers, rows)
        self.is_active_flags = is_active_flags

    def data(self, index, role):
        if not index.isValid():
            return None

        # Окраска активных токенов (старая логика)
        if role == Qt.ForegroundRole and index.column() in [1, 2]:
            if self.is_active_flags[index.row()]:
                return QColor("#006400")

        # Новая логика окраски для столбцов просадки и профита
        if role == Qt.ForegroundRole:
            header_text = self.headerData(index.column(), Qt.Horizontal)
            if header_text == "Макс. профит":
                val_str = self.rows[index.row()][index.column()]
                if val_str.startswith('+'):
                    return QColor("#006400")  # Зеленый
            elif header_text == "Макс. просадка":
                val_str = self.rows[index.row()][index.column()]
                if val_str.startswith('-'):
                    return QColor("#d33c3c")  # Красный

        if role == Qt.TextAlignmentRole:
            if index.column() not in [1, 2]:
                return Qt.AlignCenter
            return Qt.AlignVCenter | Qt.AlignLeft

        return super().data(index, role)


# ---- Кастомная таблица для отлова нажатий ----
class KeypressTableView(QTableView):
    deleteKeyPressed = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.deleteKeyPressed.emit()
        else:
            super().keyPressEvent(event)


# ---- ограничитель запросов ----
class RateLimiter:
    def __init__(self, rps: float):
        self.rps = max(0.1, float(rps))
        self.win = deque()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            while self.win and now - self.win[0] > 1.0:
                self.win.popleft()
            sleep = 0.0 if len(self.win) < self.rps else 1.0 - (now - self.win[0])
            if sleep > 0: time.sleep(sleep)
            self.win.append(time.time())


def http_get(url, params=None, timeout=10, headers=None):
    r = requests.get(url, params=params, timeout=timeout, headers=headers or {},
                     proxies=get_request_proxies())
    r.raise_for_status()
    return r


# ---- воркеры ----
class DexBoostTailWorker(threading.Thread):
    on_new_boost = None

    def __init__(self, settings, state, stop_event, initial_seen):
        super().__init__(daemon=True)
        self.settings = settings
        self.state = state
        self.stop_event = stop_event
        self.rl = RateLimiter(settings.get("dex_rps_limit", 3.0))
        self.seen = initial_seen
        self.is_first_run = True

    def run(self):
        while not self.stop_event.is_set():
            try:
                if self.state.get("follow_mode") not in ["only_new", "combined"] or self.state.get("paused"):
                    time.sleep(5.0)
                    continue

                arr = []
                try:
                    self.rl.wait();
                    j1 = http_get(DEX_LATEST, timeout=10).json()
                    self.rl.wait();
                    j2 = http_get(DEX_TOP, timeout=10).json()

                    boosts1 = j1.get("boosts", []) if isinstance(j1, dict) else (j1 if isinstance(j1, list) else [])
                    boosts2 = j2.get("boosts", []) if isinstance(j2, dict) else (j2 if isinstance(j2, list) else [])
                    arr = (boosts1 or []) + (boosts2 or [])

                except Exception as e:
                    log_event("DEX_TAIL_ERR", error=str(e))
                    time.sleep(2.0);
                    continue

                if self.is_first_run:
                    initial_tokens = set()
                    for t in arr:
                        if isinstance(t, dict):
                            a = t.get("tokenAddress")
                            if a:
                                initial_tokens.add(a)
                    self.seen.update(initial_tokens)
                    self.is_first_run = False
                    log_event("DEX_TAIL_BASELINE_SET", count=len(initial_tokens))
                    time.sleep(10.0)
                    continue

                newly_found_tokens = {}
                for t in arr:
                    if not isinstance(t, dict): continue

                    a = t.get("tokenAddress")
                    if not a or not isinstance(a, str): continue

                    if a not in self.seen:
                        newly_found_tokens[a] = t

                for address, obj in newly_found_tokens.items():
                    self.seen.add(address)
                    boosts_amt = int(obj.get("totalAmount") or obj.get("amount") or 0)
                    if callable(self.on_new_boost):
                        self.on_new_boost(address, boosts_amt)
            except Exception as e:
                log_event("DEX_TAIL_LOOP_ERR", error=str(e))

            time.sleep(10.0)


# ---- График ----
class CandleChartWindow(QWidget):
    def __init__(self, title: str, candles: list):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(1000, 600)
        self.candles = candles[-1000:]
        self.margin_left = 100
        self.margin_right = 20
        self.margin_top = 40
        self.margin_bot = 60

    def minimumSizeHint(self):
        return QSize(800, 400)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#ffffff"))
        if not self.candles:
            p.end();
            return
        highs = [c[2] for c in self.candles]
        lows = [c[3] for c in self.candles]
        minp = min(lows);
        maxp = max(highs)
        if minp == maxp: minp *= 0.99; maxp *= 1.01
        L = self.margin_left;
        R = self.width() - self.margin_right
        T = self.margin_top;
        B = self.height() - self.margin_bot
        W = max(1, R - L);
        H = max(1, B - T)
        p.setPen(QPen(QColor("#dddddd")));
        p.drawRect(L, T, W, H)

        p.setFont(QFont("Arial", 9))
        grid_n = 8
        for i in range(grid_n + 1):
            y = T + int(H * i / grid_n)
            p.setPen(QPen(QColor("#eeeeee")));
            p.drawLine(L, y, R, y)
            val = maxp - (maxp - minp) * i / grid_n
            p.setPen(QPen(QColor("#555555")))
            p.drawText(0, y - 8, L - 8, 16, Qt.AlignRight | Qt.AlignVCenter, f"{val:.8f}")

        cnt = len(self.candles)
        xstep = W / max(1, cnt)
        tick_n = min(12, max(3, int(W / 90)))
        for i in range(tick_n + 1):
            idx = int(i * (cnt - 1) / max(1, tick_n))
            x = int(L + idx * xstep)
            ts = self.candles[idx][0]
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            label = dt.strftime("%m-%d\n%H:%M")
            p.setPen(QPen(QColor("#eeeeee")));
            p.drawLine(x, T, x, B)
            p.setPen(QPen(QColor("#555555")));
            p.drawText(x - 28, B + 28, 56, 28, Qt.AlignHCenter | Qt.AlignTop, label)

        for i, (ts, o, h, l, c, v) in enumerate(self.candles):
            x_center = L + int(i * xstep) + int(xstep / 2)
            body_w = max(1, int(xstep * 0.6))
            y_h = T + int((1 - (h - minp) / (maxp - minp)) * H)
            y_l = T + int((1 - (l - minp) / (maxp - minp)) * H)
            y_o = T + int((1 - (o - minp) / (maxp - minp)) * H)
            y_c = T + int((1 - (c - minp) / (maxp - minp)) * H)
            up = c >= o
            col = QColor("#2b9e3f") if up else QColor("#d33c3c")
            p.setPen(QPen(col, 1))
            p.drawLine(x_center, y_h, x_center, y_l)
            top = min(y_o, y_c);
            bottom = max(y_o, y_c)
            if bottom - top < 2: bottom = top + 2
            x_left = x_center - body_w // 2
            p.fillRect(QRectF(x_left, top, body_w, bottom - top), col)

        p.end()


# ---- История по адресу ----
class HistoryWindow(QMainWindow):
    def __init__(self, address: str, meta: dict):
        super().__init__()
        self.address = address
        self.meta = meta or {}
        title = f"История цены — {self.meta.get('name', 'N/A')} ({address[:8]}…)"
        self.setWindowTitle(title)
        self.resize(1000, 600)
        central = QWidget();
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        self.table = QTableView()
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)
        self.refresh()

    def refresh(self):
        candle_data = []
        try:
            with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("type") != "candle" or obj.get("address") != self.address: continue
                    ts = parse_ts(obj.get("ts"))
                    if ts is None: continue

                    candle_data.append(obj)
        except FileNotFoundError:
            pass

        candle_data.sort(key=lambda x: int(x.get("ts", 0)), reverse=True)

        rows = []
        for obj in candle_data:
            ts = int(obj["ts"])
            o = float(obj.get("o"));
            h = float(obj.get("h"))
            l = float(obj.get("l"));
            c = float(obj.get("c"))
            v = obj.get("v")

            utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            msk_dt = utc_dt + timedelta(hours=3)

            rows.append([
                msk_dt.strftime("%Y-%m-%d"),
                msk_dt.strftime("%H:%M:%S"),
                f"{o:.10f}".rstrip("0").rstrip("."),
                f"{h:.10f}".rstrip("0").rstrip("."),
                f"{l:.10f}".rstrip("0").rstrip("."),
                f"{c:.10f}".rstrip("0").rstrip("."),
                f"{v:.6f}" if isinstance(v, (int, float)) else str(v)
            ])

        headers = ["Дата", "Время (MSK)", "O", "H", "L", "C", "Объём"]
        model = SimpleTableModel(headers, rows)
        self.table.setModel(model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)


# ---- Главное окно ----
class PriceHistoryApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1500, 900)
        self.settings = ensure_json_file(SETTINGS_FILE, DEFAULT_SETTINGS.copy())
        self.state = ensure_json_file(STATE_FILE, DEFAULT_STATE.copy())
        self.meta = {}
        # Кэш для всех свечей в памяти для быстрых расчетов
        self.all_candles = defaultdict(list)
        self._load_jsonl()
        self.follow_mode = self.state.get("follow_mode", "only_new")
        self.paused = bool(self.state.get("paused", False))
        self.follow_set = set()
        self.last_selected_addr = None
        self.batch_in_progress = False
        self._open_history_windows = {}
        self._open_chart_windows = {}
        self._stop_all = threading.Event()
        self._dex_tail = None

        self.worker_signals = WorkerSignals()

        self.candle_service = CandleService(adapter=self, settings={**self.settings, **self.state}, logger=log_event)

        self.candle_service.set_callbacks(
            on_progress=self._on_batch_progress,
            on_finish=self._on_batch_finish,
            on_error=self._on_batch_error,
            on_live_appended=self._on_live_appended
        )

        self._setup_ui()
        self._start_threads()
        self._refresh_table()

    # ---------- IO ----------
    def _calculate_stats_for_address(self, addr: str):
        """Быстро рассчитывает статистику для одного адреса, используя кэш в памяти."""
        candles = self.all_candles.get(addr)
        if not candles:
            return

        # Свечи должны быть отсортированы. На всякий случай, если порядок нарушится.
        candles.sort(key=lambda x: x[0])

        # Цена открытия самой первой свечи
        discovery_price = candles[0][1]
        if discovery_price == 0:
            return

        max_high = max(c[2] for c in candles)
        min_low = min(c[3] for c in candles)

        max_profit = ((max_high / discovery_price) - 1) * 100
        max_drawdown = ((min_low / discovery_price) - 1) * 100

        m = self.meta.setdefault(addr, {})
        m['max_profit'] = max_profit
        m['max_drawdown'] = max_drawdown

    def _calculate_all_stats(self):
        """Рассчитывает статистику для всех токенов, используя кэш."""
        log_event("STATS_CALC_START_FAST")
        for addr in self.all_candles.keys():
            self._calculate_stats_for_address(addr)
        log_event("STATS_CALC_DONE_FAST", tokens=len(self.all_candles))

    def _load_jsonl(self):
        candles_count = defaultdict(int)
        last_ts = {}
        if not os.path.exists(PRICE_HISTORY_FILE):
            with open(PRICE_HISTORY_FILE, "w", encoding="utf-8"): pass

        with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                t = obj.get("type");
                a = obj.get("address")
                if not a: continue
                if t == "meta":
                    d = dict(obj)
                    disc_iso = d.get("discoveryTime") or d.get("discovery_time")
                    if disc_iso:
                        tsd = parse_ts(disc_iso)
                        if tsd is not None:
                            utc_dt = datetime.fromtimestamp(tsd, tz=timezone.utc)
                            msk_dt = utc_dt + timedelta(hours=3)
                            d["disc_date"] = msk_dt.strftime("%Y-%m-%d")
                            d["disc_time"] = msk_dt.strftime("%H:%M:%S")
                    self.meta.setdefault(a, {}).update(d)
                elif t == "candle":
                    candles_count[a] += 1
                    ts = parse_ts(obj.get("ts"))
                    if ts is not None:
                        last_ts[a] = max(last_ts.get(a, 0), ts)
                        # Заполняем кэш свечей в памяти
                        o = float(obj.get("o", 0))
                        h = float(obj.get("h", 0))
                        l = float(obj.get("l", 0))
                        self.all_candles[a].append((ts, o, h, l))

        all_known_addrs = set(self.meta.keys()) | set(candles_count.keys())
        for a in all_known_addrs:
            m = self.meta.setdefault(a, {})
            m["candles"] = candles_count.get(a, 0)
            if last_ts.get(a):
                m["last_candle_utc"] = iso_utc_from_ts(last_ts[a])

        # Рассчитываем статистику после загрузки всех данных
        self._calculate_all_stats()

        log_event("BASELINE_READY", size=len(self.meta))

    def _append_jsonl(self, obj):
        with open(PRICE_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def update_meta_field(self, addr, **fields):
        m = self.meta.setdefault(addr, {})
        changed = False
        for k, v in fields.items():
            if v is not None and m.get(k) != v:
                m[k] = v;
                changed = True
        if changed:
            snap = {"type": "meta", "address": addr}
            for k in ["name", "boosts", "fdv", "discoveryTime", "pool"]:
                if m.get(k) is not None: snap[k] = m[k]
            self._append_jsonl(snap)
            try:
                disc = m.get("discoveryTime") or m.get("discovery_time")
                if disc:
                    tsd = parse_ts(disc)
                    if tsd is not None:
                        utc_dt = datetime.fromtimestamp(tsd, tz=timezone.utc)
                        msk_dt = utc_dt + timedelta(hours=3)
                        m["disc_date"] = msk_dt.strftime("%Y-%m-%d")
                        m["disc_time"] = msk_dt.strftime("%H:%M:%S")
            except Exception:
                pass

    def append_candle(self, addr, item):
        ts, o, h, l, c, v = item
        # 1. Записываем в файл
        self._append_jsonl(
            {"type": "candle", "address": addr, "ts": int(ts), "o": float(o), "h": float(h), "l": float(l),
             "c": float(c), "v": v})

        # 2. Обновляем метаданные
        m = self.meta.setdefault(addr, {})
        m["candles"] = m.get("candles", 0) + 1
        m["last_candle_utc"] = iso_utc_from_ts(int(ts))

        # 3. Обновляем кэш в памяти
        self.all_candles[addr].append((int(ts), float(o), float(h), float(l)))

        # 4. Запускаем быстрый перерасчет статистики для этого токена
        self._calculate_stats_for_address(addr)

    def last_candle_ts(self, addr):
        m = self.meta.get(addr) or {}
        utc_str = m.get("last_candle_utc")
        if utc_str:
            try:
                dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S")
                return int(dt.replace(tzinfo=timezone.utc).timestamp())
            except (ValueError, TypeError):
                return None
        return None

    def erase_candles_for(self, addr):
        src = PRICE_HISTORY_FILE;
        tmp = src + ".tmp"
        try:
            with open(src, "r", encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
                for line in fin:
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "candle" and obj.get("address") == addr: continue
                    except Exception:
                        pass
                    fout.write(line)
            os.replace(tmp, src)
        except FileNotFoundError:
            pass
        m = self.meta.setdefault(addr, {})
        m["candles"] = 0
        m.pop("last_candle_utc", None)
        # Очищаем кэш и статистику для удаленного токена
        if addr in self.all_candles:
            del self.all_candles[addr]
        m.pop('max_profit', None)
        m.pop('max_drawdown', None)

    def delete_tokens_everywhere(self, addresses: Set[str]):
        for addr in addresses:
            self.meta.pop(addr, None)
            if addr in self.all_candles:
                del self.all_candles[addr]
        src = PRICE_HISTORY_FILE;
        tmp = src + ".tmp"
        try:
            with open(src, "r", encoding="utf-8") as fin:
                lines = fin.readlines()
            with open(tmp, "w", encoding="utf-8") as fout:
                for line in lines:
                    try:
                        obj = json.loads(line)
                        if obj.get("address") in addresses: continue
                    except (json.JSONDecodeError, KeyError):
                        pass
                    fout.write(line)
            os.replace(tmp, src)
        except FileNotFoundError:
            pass
        except Exception as e:
            log_event("DELETE_ERROR", error=str(e))
        log_event("TOKENS_DELETED", count=len(addresses))
        self._refresh_table()

    def ensure_pool_for(self, addr, rl, force_refresh=False):
        if not force_refresh:
            pool = self.meta.get(addr, {}).get("pool")
            if pool: return pool

        if rl: rl.wait()

        retries = int(self.settings.get("gecko_max_retries", 6))
        backoff = float(self.settings.get("gecko_backoff_base", 1.4))

        for i in range(retries):
            try:
                r = http_get(GECKO_POOLS.format(address=addr), timeout=12, headers={"accept": "application/json"})
                j = r.json()
                pools_data = j.get("data") or []

                if not pools_data:
                    return None

                def sort_key(pool):
                    try:
                        attributes = pool.get("attributes", {})
                        volume_usd = attributes.get("volume_usd", {})
                        return float(volume_usd.get("h24", 0))
                    except (ValueError, TypeError):
                        return 0.0

                sorted_pools = sorted(pools_data, key=sort_key, reverse=True)

                if len(sorted_pools) > 1:
                    active_pools = [p for p in sorted_pools if sort_key(p) > 0]
                    if active_pools:
                        sorted_pools = active_pools

                best_pool = sorted_pools[0]
                pid = best_pool.get("id") or ""

                if "_" in pid:
                    pid = pid.split("_", 1)[-1]

                if pid:
                    self.update_meta_field(addr, pool=pid)
                    return pid

                return None
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    log_event("GECKO_TOKEN_POOLS_404_NOT_FOUND", address=addr)
                    self.candle_service.add_to_greylist(addr)
                    return None
                if i == retries - 1:
                    log_event("GECKO_TOKEN_POOLS_ERR", address=addr, error=str(e))
                    return None
                time.sleep((backoff ** i) + random.uniform(0, 0.25))
            except Exception as e:
                if i == retries - 1:
                    log_event("GECKO_TOKEN_POOLS_ERR", address=addr, error=str(e))
                    return None
                time.sleep((backoff ** i) + random.uniform(0, 0.25))
        return None

    # ---------- UI ----------
    def _setup_ui(self):
        central = QWidget();
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        status_layout = QHBoxLayout()
        self.indicator_lbl = QLabel()
        self.progress_bar = QProgressBar();
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(True);
        self.progress_bar.setVisible(False)
        status_layout.addWidget(self.indicator_lbl);
        status_layout.addWidget(self.progress_bar, 1)
        root.addLayout(status_layout)
        self.progress_lbl = QLabel();
        root.addWidget(self.progress_lbl)
        self._update_indicator_text()
        kb = QHBoxLayout()
        self.btn_only_new = QPushButton("Только новые бусты")
        self.btn_all_known = QPushButton("Live для всех известных")
        self.btn_combined = QPushButton("Новые бусты + Live")
        self.btn_pause = QPushButton("Пауза");
        self.btn_resume = QPushButton("Продолжить")
        self.btn_stop = QPushButton("Стоп Live");

        self.batch_buttons = []

        for b in [self.btn_only_new, self.btn_all_known, self.btn_combined, self.btn_pause, self.btn_resume,
                  self.btn_stop]:
            kb.addWidget(b)
        root.addLayout(kb)

        kb_batch = QHBoxLayout()
        self.btn_refresh = QPushButton("Обновить все данные")
        self.btn_topup = QPushButton("Догрузить недостающее")
        for b in [self.btn_refresh, self.btn_topup]:
            kb_batch.addWidget(b)
        root.addLayout(kb_batch)

        kb2 = QHBoxLayout()
        self.btn_open_history = QPushButton("Открыть историю цены")
        self.btn_open_chart = QPushButton("Открыть график")
        self.btn_update_token = QPushButton("Обновить данные токена")
        self.btn_reset_columns = QPushButton("Сбросить столбцы")
        self.btn_reset_columns.setFixedWidth(120)

        self.btn_cleanup = QPushButton("Очистить базу")
        self.btn_cleanup.setFixedWidth(120)

        kb2.addWidget(self.btn_open_history)
        kb2.addWidget(self.btn_open_chart)
        kb2.addWidget(self.btn_update_token)
        kb2.addStretch()
        kb2.addWidget(self.btn_reset_columns)
        kb2.addWidget(self.btn_cleanup)
        root.addLayout(kb2)

        self.batch_buttons.extend([self.btn_refresh, self.btn_topup, self.btn_update_token, self.btn_cleanup])

        self.tabs = QTabWidget();
        root.addWidget(self.tabs)
        self.hist_tab = QWidget();
        self.tabs.addTab(self.hist_tab, "История цен (сек)")
        hl = QVBoxLayout(self.hist_tab)
        self.table = KeypressTableView()

        padding_delegate = PaddedItemDelegate(self.table)
        self.table.setItemDelegateForColumn(1, padding_delegate)
        self.table.setItemDelegateForColumn(2, padding_delegate)

        self.table.setSelectionBehavior(QTableView.SelectRows);
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context)
        self.table.deleteKeyPressed.connect(self._on_delete_requested)
        self.table.verticalHeader().setVisible(False);
        self.table.clicked.connect(self._remember_clicked)
        hl.addWidget(self.table)
        self.settings_scroll = QScrollArea();
        self.settings_scroll.setWidgetResizable(True)
        self.tabs.addTab(self.settings_scroll, "Настройки");
        sett_container = QWidget()
        self.settings_scroll.setWidget(sett_container);
        sLay = QVBoxLayout(sett_container)
        gb_live = QGroupBox("Live-пуллер");
        fg = QFormLayout(gb_live)
        self.sb_live_sec = QSpinBox();
        self.sb_live_sec.setRange(5, 600);
        self.sb_live_sec.setValue(int(self.settings.get("live_pull_interval_sec", 30)))
        self.sb_live_last = QSpinBox();
        self.sb_live_last.setRange(1, 1000);
        self.sb_live_last.setValue(int(self.settings.get("live_recent_limit", 5)))
        self.sb_pause_while_batch = QSpinBox();
        self.sb_pause_while_batch.setRange(0, 1);
        self.sb_pause_while_batch.setValue(1 if self.settings.get("pause_live_while_batch", True) else 0)
        fg.addRow("Интервал live, сек", self.sb_live_sec);
        fg.addRow("Сколько последних свечей", self.sb_live_last);
        fg.addRow("Останавливать live во время батча (1/0)", self.sb_pause_while_batch)
        sLay.addWidget(gb_live)
        gb_gecko = QGroupBox("GeckoTerminal");
        gg = QFormLayout(gb_gecko)
        self.sb_gecko_rps = QDoubleSpinBox();
        self.sb_gecko_rps.setRange(0.2, 10.0);
        self.sb_gecko_rps.setDecimals(1);
        self.sb_gecko_rps.setValue(float(self.settings.get("gecko_rps_limit", 2.0)))
        self.sb_gecko_retry = QSpinBox();
        self.sb_gecko_retry.setRange(1, 15);
        self.sb_gecko_retry.setValue(int(self.settings.get("gecko_max_retries", 6)))
        self.sb_gecko_back = QDoubleSpinBox();
        self.sb_gecko_back.setRange(1.0, 3.0);
        self.sb_gecko_back.setDecimals(2);
        self.sb_gecko_back.setValue(float(self.settings.get("gecko_backoff_base", 1.4)))
        self.sb_batch_page = QSpinBox();
        self.sb_batch_page.setRange(1, 1000);
        self.sb_batch_page.setValue(int(self.settings.get("batch_page_limit", 1000)))
        gg.addRow("RPS (запросов/сек)", self.sb_gecko_rps);
        gg.addRow("Повторы", self.sb_gecko_retry);
        gg.addRow("Backoff", self.sb_gecko_back);
        gg.addRow("Лимит OHLCV, шт.", self.sb_batch_page)
        self.sb_history_days = QSpinBox();
        self.sb_history_days.setRange(1, 3650);
        self.sb_history_days.setValue(int(self.state.get("history_days_limit", 7)))
        self.sb_full_history = QSpinBox();
        self.sb_full_history.setRange(0, 1);
        self.sb_full_history.setValue(1 if self.state.get("full_history", False) else 0)
        gg.addRow("Лимит истории, дней", self.sb_history_days)
        gg.addRow("Полная история (1/0)", self.sb_full_history)
        sLay.addWidget(gb_gecko)
        gb_dex = QGroupBox("Dexscreener");
        dg = QFormLayout(gb_dex)
        self.sb_dex_rps = QDoubleSpinBox();
        self.sb_dex_rps.setRange(0.2, 10.0);
        self.sb_dex_rps.setDecimals(1);
        self.sb_dex_rps.setValue(float(self.settings.get("dex_rps_limit", 3.0)))
        self.sb_dex_retry = QSpinBox();
        self.sb_dex_retry.setRange(1, 15);
        self.sb_dex_retry.setValue(int(self.settings.get("dex_max_retries", 5)))
        self.sb_use_dex_details = QSpinBox();
        self.sb_use_dex_details.setRange(0, 1);
        self.sb_use_dex_details.setValue(1 if self.settings.get("use_dex_details_for_name_fdv", True) else 0)
        dg.addRow("RPS", self.sb_dex_rps);
        dg.addRow("Повторы", self.sb_dex_retry);
        dg.addRow("Обогащать имя/FDV через Dex (1/0)", self.sb_use_dex_details)
        sLay.addWidget(gb_dex)
        sb = QHBoxLayout();
        bsave = QPushButton("Сохранить настройки");
        breset = QPushButton("Сбросить")
        sb.addWidget(bsave);
        sb.addWidget(breset);
        sLay.addLayout(sb)
        bsave.clicked.connect(self._save_settings);
        breset.clicked.connect(self._reset_settings)

        self.btn_only_new.clicked.connect(self._mode_only_new);
        self.btn_all_known.clicked.connect(self._mode_all_known)
        self.btn_combined.clicked.connect(self._mode_combined)
        self.btn_pause.clicked.connect(self._pause);
        self.btn_resume.clicked.connect(self._resume)
        self.btn_stop.clicked.connect(self._stop)
        self.btn_refresh.clicked.connect(self._run_batch_for_all)
        self.btn_topup.clicked.connect(lambda: self._run_batch("topup_all", title="Дозагрузка недостающего"))
        self.btn_open_history.clicked.connect(self._open_selected_history)
        self.btn_open_chart.clicked.connect(self._open_selected_chart)
        self.btn_update_token.clicked.connect(self._run_batch_for_selected)
        self.btn_reset_columns.clicked.connect(self._reset_column_widths)
        self.btn_cleanup.clicked.connect(self._cleanup_database)

        self.worker_signals.progress.connect(self._handle_batch_progress)
        self.worker_signals.finished.connect(self._handle_batch_finish)
        self.worker_signals.error.connect(self._handle_batch_error)
        self.ui_timer = QTimer(self);
        self.ui_timer.setInterval(2000)
        self.ui_timer.timeout.connect(self._refresh_table);

        self.ui_timer.start()

    def _remember_clicked(self, index: QModelIndex):
        r = index.row()
        if 0 <= r < len(getattr(self, "_table_addrs", [])):
            self.last_selected_addr = self._table_addrs[r]

    def _save_settings(self):
        self.settings["live_pull_interval_sec"] = int(self.sb_live_sec.value())
        self.settings["live_recent_limit"] = int(self.sb_live_last.value())
        self.settings["pause_live_while_batch"] = bool(int(self.sb_pause_while_batch.value()))
        self.settings["gecko_rps_limit"] = float(self.sb_gecko_rps.value())
        self.settings["gecko_max_retries"] = int(self.sb_gecko_retry.value())
        self.settings["gecko_backoff_base"] = float(self.sb_gecko_back.value())
        self.settings["batch_page_limit"] = int(self.sb_batch_page.value())
        self.settings["dex_rps_limit"] = float(self.sb_dex_rps.value())
        self.settings["dex_max_retries"] = int(self.sb_dex_retry.value())
        self.settings["use_dex_details_for_name_fdv"] = bool(int(self.sb_use_dex_details.value()))
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, ensure_ascii=False, indent=2)

        self.state["history_days_limit"] = int(self.sb_history_days.value())
        self.state["full_history"] = bool(self.sb_full_history.value())
        self._save_state()

        self.candle_service.update_settings(**self.settings, **self.state)
        QMessageBox.information(self, "Настройки", "Настройки сохранены и применены.")

    def _reset_settings(self):
        self.settings = DEFAULT_SETTINGS.copy()
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f: json.dump(self.settings, f, ensure_ascii=False, indent=2)

        self.state["history_days_limit"] = DEFAULT_STATE["history_days_limit"]
        self.state["full_history"] = DEFAULT_STATE["full_history"]
        self.state["column_widths"] = {}
        self._save_state()

        self.sb_live_sec.setValue(self.settings["live_pull_interval_sec"])
        self.sb_live_last.setValue(self.settings["live_recent_limit"])
        self.sb_pause_while_batch.setValue(1 if self.settings["pause_live_while_batch"] else 0)
        self.sb_gecko_rps.setValue(self.settings["gecko_rps_limit"])
        self.sb_gecko_retry.setValue(self.settings["gecko_max_retries"])
        self.sb_gecko_back.setValue(self.settings["gecko_backoff_base"])
        self.sb_batch_page.setValue(self.settings["batch_page_limit"])
        self.sb_dex_rps.setValue(self.settings["dex_rps_limit"])
        self.sb_dex_retry.setValue(self.settings["dex_max_retries"])
        self.sb_use_dex_details.setValue(1 if self.settings["use_dex_details_for_name_fdv"] else 0)
        self.sb_history_days.setValue(self.state["history_days_limit"])
        self.sb_full_history.setValue(1 if self.state["full_history"] else 0)

        self.candle_service.update_settings(**self.settings, **self.state)
        self._refresh_table()
        QMessageBox.information(self, "Настройки", "Настройки сброшены к значениям по умолчанию.")

    # ---------- режимы/состояние ----------
    def _save_state(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def _update_indicator_text(self):
        if self.batch_in_progress: return
        mode_map = {
            "only_new": "Режим: только новые",
            "all_known": "Режим: все известные",
            "combined": "Режим: Новые + Live",
            "stop": "Режим: Стоп"
        }
        mode_text = mode_map.get(self.follow_mode, "Режим: Неизвестно")

        status = " (ПАУЗА)" if self.paused else ""
        color = "#0a0" if not self.paused else "#d11"
        if self.follow_mode == 'stop':
            color = "#888"

        self.indicator_lbl.setText(f'<span style="color:{color}; font-weight:bold;">{mode_text}{status}</span>')

    def _mode_only_new(self):
        self.follow_mode = "only_new";
        self.state["follow_mode"] = self.follow_mode;
        self._save_state();
        self._update_indicator_text()

    def _mode_all_known(self):
        self.follow_mode = "all_known";
        self.state["follow_mode"] = self.follow_mode;
        self._save_state();
        self._update_indicator_text()

    def _mode_combined(self):
        self.follow_mode = "combined"
        self.state["follow_mode"] = self.follow_mode
        self._save_state()
        self._update_indicator_text()

    def _pause(self):
        self.paused = True;
        self.state["paused"] = True;
        self._save_state();
        self._update_indicator_text()

    def _resume(self):
        self.paused = False;
        self.state["paused"] = False;
        self._save_state();
        self._update_indicator_text()

    def _stop(self):
        self.follow_mode = "stop"
        self.state["follow_mode"] = self.follow_mode
        self.follow_set.clear()
        self._save_state()
        self._update_indicator_text()

    # ---------- таблица ----------
    def _current_selected_address(self):
        sel = self.table.selectionModel()
        if sel:
            rows = sel.selectedRows()
            if rows:
                r = rows[0].row()
                if 0 <= r < len(self._table_addrs): return self._table_addrs[r]
        return self.last_selected_addr

    def _fmt_boosts(self, val):
        if isinstance(val, (int, float)) and val > 0: return str(int(val))
        return "—"

    def _refresh_table(self):
        selection_model = self.table.selectionModel()
        selected_addresses = set()
        if selection_model and hasattr(self, "_table_addrs"):
            for index in selection_model.selectedRows():
                if 0 <= index.row() < len(self._table_addrs):
                    selected_addresses.add(self._table_addrs[index.row()])
        vbar = self.table.verticalScrollBar()
        keep_scroll = vbar.value() if vbar is not None else 0

        items = sorted(self.meta.items(),
                       key=lambda item: (item[1].get("discoveryTime", "0")), reverse=True)
        self._table_addrs = [addr for addr, m in items]

        is_active_flags = []
        now_ts = int(time.time())
        days_limit_sec = self.state.get("history_days_limit", 7) * 86400

        rows = []
        for i, (addr, m) in enumerate(items, start=1):
            fdv = m.get("fdv");
            fdv_s = (f"${int(fdv):,}".replace(",", " ")) if isinstance(fdv, (int, float)) else "—"

            # Форматирование для новых столбцов
            drawdown = m.get('max_drawdown')
            drawdown_s = f"{drawdown:.2f}%" if isinstance(drawdown, float) else "—"

            profit = m.get('max_profit')
            profit_s = f"+{profit:.2f}%" if isinstance(profit, float) else "—"

            last_candle_date_str = "—"
            last_candle_time_str = "—"
            utc_str = m.get("last_candle_utc")
            if utc_str:
                try:
                    utc_dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    msk_dt = utc_dt + timedelta(hours=3)
                    last_candle_date_str = msk_dt.strftime("%Y-%m-%d")
                    last_candle_time_str = msk_dt.strftime("%H:%M:%S")
                except ValueError:
                    pass

            rows.append([
                i, m.get("name") or "N/A", addr, self._fmt_boosts(m.get("boosts")), fdv_s,
                drawdown_s, profit_s,  # Новые данные
                   m.get("disc_date") or "—", m.get("disc_time") or "—",
                str(int(m.get("candles", 0))),
                last_candle_date_str,
                last_candle_time_str,
            ])

            is_active = False
            discovery_time_str = m.get("discoveryTime")
            if discovery_time_str:
                discovery_ts = parse_ts(discovery_time_str)
                if discovery_ts and (now_ts - discovery_ts) < days_limit_sec:
                    is_active = True
            is_active_flags.append(is_active)

        headers = ["#", "Название", "Адрес (CA)", "Бусты", "FDV", "Макс. просадка", "Макс. профит", "Дата", "Время",
                   "Свечей", "Последняя свеча (дата)",
                   "Последняя свеча (время)"]

        model = CenteredTableModel(headers, rows, is_active_flags)
        self.table.setModel(model)

        if not hasattr(self, "_columns_initialized"):
            header = self.table.horizontalHeader()
            saved_widths = self.state.get("column_widths", {})
            if saved_widths:
                for index_str, width in saved_widths.items():
                    try:
                        index = int(index_str)
                        if index < header.count():
                            header.setSectionResizeMode(index, QHeaderView.Interactive)
                            self.table.setColumnWidth(index, width)
                    except (ValueError, TypeError):
                        continue
            else:
                self._reset_column_widths()
            self._columns_initialized = True

        if selected_addresses:
            new_selection_model = QItemSelectionModel(model)
            for addr in selected_addresses:
                if addr in self._table_addrs:
                    row_index = self._table_addrs.index(addr)
                    new_selection_model.select(model.index(row_index, 0),
                                               QItemSelectionModel.Select | QItemSelectionModel.Rows)
            self.table.setSelectionModel(new_selection_model)
        if vbar is not None: vbar.setValue(keep_scroll)

    def _reset_column_widths(self):
        self.state["column_widths"] = {}
        self._save_state()

        header = self.table.horizontalHeader()
        if not header: return

        for i in range(header.count()):
            header.setSectionResizeMode(i, QHeaderView.Interactive)

        for i in range(header.count()):
            if i in [1, 2]: continue
            header.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)

    # ---------- контекстное меню и удаление ----------
    def _show_context(self, pos):
        idx = self.table.indexAt(pos)
        if not idx.isValid(): return
        self.last_selected_addr = self._table_addrs[idx.row()]
        menu = QMenu()
        act_open = menu.addAction("Открыть на Dexscreener")
        act_copy_link = menu.addAction("Копировать ссылку")
        act_copy_ca = menu.addAction("Копировать CA")
        act_copy_api = menu.addAction("Скопировать ссылку на API")

        menu.addSeparator()
        act_delete = menu.addAction("Удалить выбранное...")

        act = menu.exec_(self.table.viewport().mapToGlobal(pos))

        url = f"https://dexscreener.com/solana/{self.last_selected_addr}"

        if act == act_open:
            import webbrowser;
            webbrowser.open(url)
        elif act == act_copy_link:
            QApplication.clipboard().setText(url)
        elif act == act_copy_ca:
            QApplication.clipboard().setText(self.last_selected_addr)
        elif act == act_delete:
            self._on_delete_requested()
        elif act == act_copy_api:
            meta_info = self.meta.get(self.last_selected_addr, {})
            pool_id = meta_info.get("pool")
            if pool_id:
                api_url = GECKO_OHLCV.format(pool=pool_id)
                QApplication.clipboard().setText(api_url)
                QMessageBox.information(self, "Скопировано", f"Ссылка на API скопирована:\n{api_url}")
            else:
                QMessageBox.warning(self, "Ошибка",
                                    "Pool ID для этого токена не найден. Обновите данные токена, чтобы он появился.")

    def _on_delete_requested(self):
        selection = self.table.selectionModel().selectedRows()
        if not selection: return
        addresses_to_delete = {self._table_addrs[index.row()] for index in selection}
        count = len(addresses_to_delete)
        msg = f"Вы уверены, что хотите удалить данные для {count} токена(ов)?"
        reply = QMessageBox.question(self, "Подтверждение удаления", msg, QMessageBox.Yes | QMessageBox.No,
                                     QMessageBox.No)
        if reply == QMessageBox.Yes: self.delete_tokens_everywhere(addresses_to_delete)

    # ---------- действия по кнопкам ----------
    def _open_selected_history(self):
        addr = self._current_selected_address()
        if not addr: return
        w = HistoryWindow(addr, self.meta.get(addr, {}))
        self._open_history_windows[addr] = w
        w.show();
        w.raise_();
        w.activateWindow()

    def _open_selected_chart(self):
        addr = self._current_selected_address()
        if not addr: return

        # Используем быстрый кэш в памяти вместо чтения файла
        candles_tuples = self.all_candles.get(addr, [])
        if not candles_tuples:
            QMessageBox.information(self, "График", "Нет свечных данных для выбранного токена.")
            return

        # Преобразуем кортежи в нужный формат для графика (добавляя фиктивные c и v)
        candles = [(t, o, h, l, o, 0) for t, o, h, l in candles_tuples]

        title = f"График — {self.meta.get(addr, {}).get('name', 'N/A')} ({addr[:8]}…)"
        w = CandleChartWindow(title, sorted(candles, key=lambda x: x[0]))
        self._open_chart_windows[addr] = w
        w.show();
        w.raise_();
        w.activateWindow()

    def _cleanup_database(self):
        tokens_to_delete = set()
        for addr, meta in self.meta.items():
            is_pump = addr.endswith("pump")
            fdv = meta.get("fdv")

            if not is_pump:
                tokens_to_delete.add(addr)
                continue

            if fdv is not None and fdv < 30000:
                tokens_to_delete.add(addr)

        if not tokens_to_delete:
            QMessageBox.information(self, "Очистка", "Не найдено токенов для удаления.")
            return

        msg = f"Найдено {len(tokens_to_delete)} токена(ов), не соответствующих критериям (не .pump или FDV < 30k).\n\nВы уверены, что хотите их удалить?"
        reply = QMessageBox.question(self, "Подтверждение очистки", msg, QMessageBox.Yes | QMessageBox.No,
                                     QMessageBox.No)

        if reply == QMessageBox.Yes:
            self.delete_tokens_everywhere(tokens_to_delete)
            QMessageBox.information(self, "Очистка завершена", f"Удалено {len(tokens_to_delete)} токена(ов).")

    # ---------- потоки ----------
    def _start_threads(self):
        self._dex_tail = DexBoostTailWorker(self.settings, self.state, self._stop_all,
                                            initial_seen=set(self.meta.keys()))
        self._dex_tail.on_new_boost = self._on_new_boost
        self._dex_tail.start()
        self.candle_service.start_live()

    def _on_new_boost(self, address, boosts):
        threading.Thread(target=self._try_enrich_and_filter, args=(address, boosts), daemon=True).start()

    def _on_live_appended(self, active_count, appended_count):
        log_event("LIVE_APPENDED", active=active_count, count=appended_count)

    def active_addresses(self):
        if self.paused or self.follow_mode == "stop":
            return []

        if self.follow_mode == "only_new":
            return list(self.follow_set)

        if self.follow_mode in ["all_known", "combined"]:
            active_tokens = []
            now_ts = int(time.time())
            days_limit_sec = self.state.get("history_days_limit", 7) * 86400

            for addr, meta_data in self.meta.items():
                discovery_time_str = meta_data.get("discoveryTime")
                if not discovery_time_str:
                    continue

                discovery_ts = parse_ts(discovery_time_str)
                if not discovery_ts:
                    continue

                if (now_ts - discovery_ts) < days_limit_sec:
                    active_tokens.append(addr)

            return list(set(active_tokens) | self.follow_set)

        return []

    def _set_batch_status(self, is_running: bool, mode_str: str = ""):
        self.batch_in_progress = is_running
        for btn in self.batch_buttons: btn.setEnabled(not is_running)
        self.progress_bar.setVisible(is_running)
        if is_running:
            self.indicator_lbl.setText(f'<span style="color:green; font-weight:bold;">Идёт загрузка данных...</span>')
            self.progress_lbl.setText(f"Подготовка к операции: {mode_str}")
            self.progress_bar.setValue(0)
        else:
            self.progress_lbl.setText("Пакетная обработка завершена.")
            self._update_indicator_text()

    def _run_batch(self, mode: str, addrs: list = None, title: str = ""):
        if self.batch_in_progress:
            return

        target_addrs = addrs if addrs is not None else list(self.meta.keys())
        if not target_addrs:
            if addrs is not None:
                QMessageBox.information(self, "Нет цели", "Нет токенов для выполнения операции.")
            return

        if self.settings.get("pause_live_while_batch", True):
            self.paused = True
            self.state["paused"] = True
            self._save_state()

        self._set_batch_status(True, title)

        if mode == "update_all":
            for addr in target_addrs:
                if addr in self.meta:
                    self.meta[addr].pop("pool", None)

        self.candle_service.start_batch(mode=mode, addresses=target_addrs)

    def _run_batch_for_all(self):
        self._run_batch(mode="update_all", title="Полное обновление всех данных")

    def _run_batch_for_selected(self):
        selection = self.table.selectionModel().selectedRows()
        if not selection:
            QMessageBox.information(self, "Нет выбора",
                                    "Пожалуйста, выберите один или несколько токенов для обновления.", QMessageBox.Ok)
            return

        addresses_to_update = [self._table_addrs[index.row()] for index in selection]
        self._run_batch(mode="update_all", addrs=addresses_to_update,
                        title=f"Обновление {len(addresses_to_update)} токена(ов)")

    def _on_batch_progress(self, done, total, msg):
        self.worker_signals.progress.emit(done, total, msg)

    def _on_batch_finish(self):
        self.worker_signals.finished.emit()

    def _on_batch_error(self, message):
        self.worker_signals.error.emit(message)

    def _handle_batch_progress(self, done, total, msg):
        if total > 0 and done >= 0:
            percent = int(done * 100 / total) if total > 0 else 0
            self.progress_bar.setValue(percent)
            s = f"[{done}/{total}] {msg}"
            self.progress_lbl.setText(s)
            log_event("BATCH_PROGRESS", done=done, total=total, msg=msg)
        else:
            self.progress_lbl.setText(msg)
            log_event("BATCH_PROGRESS", msg=msg)

    def _handle_batch_finish(self):
        log_event("BATCH_DONE")
        # Пересчитываем статистику после завершения пакетной загрузки
        self._calculate_all_stats()

        if self.settings.get("pause_live_while_batch", True):
            self.paused = False;
            self.state["paused"] = False;
            self._save_state()
        self._set_batch_status(False)
        self._refresh_table()

    def _handle_batch_error(self, message):
        log_event("BATCH_ERR", error=message)

    def _try_enrich_and_filter(self, address, boosts):
        try:
            if not address.endswith("pump"):
                log_event("TOKEN_REJECTED", address=address, reason="Not a pump token")
                return

            j = http_get(DEX_TOKEN.format(address=address), timeout=8).json()
            pairs = j.get("pairs") or []

            if not pairs:
                log_event("TOKEN_REJECTED", address=address, reason="No pairs found on Dexscreener")
                return

            p = pairs[0]
            fdv = p.get("fdv")

            if not isinstance(fdv, (int, float)) or fdv < 30000:
                log_event("TOKEN_REJECTED", address=address, reason=f"FDV is {fdv}, which is less than 30k")
                return

            name = (p.get("baseToken") or {}).get("name")

            if address not in self.meta:
                disc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                self.update_meta_field(address, name=name, fdv=fdv, boosts=int(boosts), discoveryTime=disc, candles=0)
            elif int(self.meta[address].get("boosts") or 0) != int(boosts):
                self.update_meta_field(address, boosts=int(boosts), fdv=fdv)

            self.follow_set.add(address)
            log_event("NEW_BOOST_ACCEPTED", address=address, boosts=int(boosts), fdv=fdv)

        except Exception as e:
            log_event("ENRICH_FILTER_ERR", address=address, error=str(e))

    def closeEvent(self, e):
        header = self.table.horizontalHeader()
        if header and self.table.model() and header.count() > 0:
            widths = {}
            for i in range(header.count()):
                widths[str(i)] = header.sectionSize(i)
            self.state["column_widths"] = widths
            self._save_state()

        try:
            self._stop_all.set()
            self.candle_service.stop_all()
        except Exception:
            pass
        super().closeEvent(e)


# ---- main ----
if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = PriceHistoryApp()
    w.show()
    sys.exit(app.exec_())

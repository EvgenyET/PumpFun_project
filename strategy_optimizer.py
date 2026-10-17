# strategy_optimizer.py
# PyQt5 GUI для оптимизации стратегии на основе сохранённой БД price_history.jsonl
# Требования: Python 3.9+, PyQt5, pyqtgraph. Без pandas.
# pip install pyqtgraph
#
# Обновление от 2025-10-21 (v22.4, UI/UX):
# - Добавлено сохранение размеров окна и ширины колонок для "Детализации сделок".
# - Настроено центрирование колонок в "Детализации сделок".
#
import json
import os
import sys
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Set

import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QMenu


class NumericTableWidgetItem(QtWidgets.QTableWidgetItem):
    def __lt__(self, other):
        try:
            self_text = self.text().replace('$', '').replace('%', '').replace(',', '').strip()
            other_text = other.text().replace('$', '').replace('%', '').replace(',', '').strip()
            return float(self_text) < float(other_text)
        except (ValueError, TypeError):
            return super().__lt__(other)


def parse_iso(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.strptime(ts.split('.')[0], "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)


class DataLoader:
    def __init__(self, path: str):
        self.path = path
        self.tokens: Dict[str, Dict] = {}

    def load(self) -> None:
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"Файл не найден: {self.path}")
        tokens: Dict[str, Dict] = {}
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s: continue
                try:
                    obj = json.loads(s)
                except Exception:
                    continue
                t = obj.get("type");
                addr = obj.get("address")
                if not addr: continue
                if t == "meta":
                    rec = tokens.get(addr) or {"name": addr, "discovery": None, "ticks": [], "boosts": 0, "fdv": 0}
                    rec["name"] = obj.get("name") or rec.get("name") or addr
                    disc = obj.get("discoveryTime")
                    rec["discovery"] = parse_iso(disc) if disc else rec.get("discovery")
                    rec["boosts"] = obj.get("boosts") or rec.get("boosts")
                    rec["fdv"] = obj.get("fdv") or rec.get("fdv")
                    tokens[addr] = rec
                elif t in ("tick", "candle"):
                    ts_val = obj.get("ts")
                    price = obj.get("priceUsd") if t == "tick" else obj.get("c")
                    if ts_val is None or price is None: continue
                    dt = parse_iso(ts_val) if isinstance(ts_val, str) else datetime.fromtimestamp(int(ts_val),
                                                                                                  tz=timezone.utc)
                    rec = tokens.get(addr) or {"name": addr, "discovery": None, "ticks": [], "boosts": 0, "fdv": 0}
                    rec["ticks"].append((dt, float(price)))
                    tokens[addr] = rec

        tokens_with_ticks = {addr: rec for addr, rec in tokens.items() if rec.get("ticks")}

        for addr, rec in tokens_with_ticks.items():
            rec["ticks"].sort(key=lambda x: x[0])
            disc = rec.get("discovery")
            if disc:
                after = [(ts, p) for (ts, p) in rec["ticks"] if ts >= disc]
                if after: rec["ticks"] = after
        self.tokens = tokens_with_ticks

    def get_filtered_stream(self, filtered_tokens: Dict) -> List[Tuple[datetime, str, float]]:
        events = []
        for addr, rec in filtered_tokens.items():
            for dt, price in rec["ticks"]:
                events.append((dt, addr, price))
        events.sort(key=lambda x: x[0])
        return events

    def diagnostics(self) -> Tuple[int, int, int]:
        total = len(self.tokens)
        with_ticks = sum(1 for r in self.tokens.values() if r.get("ticks"))
        with_after_disc = 0
        for r in self.tokens.values():
            if not r.get("ticks"): continue
            disc = r.get("discovery")
            if not disc or any(ts >= disc for ts, _ in r["ticks"]):
                with_after_disc += 1
        return total, with_ticks, with_after_disc


class Backtester:
    def __init__(self, tokens: Dict, deposit_usd: float, per_trade_usd: float, tp_pct: float,
                 sl_pct: float, hold_days: int, fee_pct: float, slip_pct: float, entry_mode: str,
                 high_window_minutes: int):
        self.initial_tokens = tokens
        self.deposit_usd = deposit_usd
        self.per_trade_usd = per_trade_usd
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.hold_td = timedelta(days=hold_days)
        self.fee_pct = fee_pct
        self.slip_pct = slip_pct
        self.entry_mode = entry_mode
        self.high_window = timedelta(minutes=max(1, int(high_window_minutes)))
        self.current_tokens = {}
        self.events = []

    def set_active_data(self, tokens: Dict, events: List):
        self.current_tokens = tokens
        self.events = events

    def run_for_threshold(self, dd_threshold: Optional[float]) -> Tuple[float, List[Dict], Set[str]]:
        capital = self.deposit_usd;
        open_pos = {};
        processed_tokens = set();
        trades = []
        seen_first_tick = set();
        local_windows = {};
        boost_price = {};
        entered_tokens = set()

        for ts, addr, price in self.events:
            name = self.current_tokens.get(addr, {}).get("name", addr)
            pos = open_pos.get(addr)
            if pos:
                entry_raw = pos["entry_raw"]
                exit_reason, target_price = None, None
                if price <= entry_raw * (1.0 - self.sl_pct):
                    exit_reason, target_price = "STOP", entry_raw * (1.0 - self.sl_pct)
                elif price >= entry_raw * (1.0 + self.tp_pct):
                    exit_reason, target_price = "TP", entry_raw * (1.0 + self.tp_pct)
                elif ts - pos["entry_time"] >= self.hold_td:
                    exit_reason, target_price = "TIMEOUT", price
                if exit_reason:
                    px_raw = target_price if target_price is not None else price
                    px_eff = px_raw * (1.0 - self.slip_pct)
                    net = (pos["qty"] * px_eff) * (1.0 - self.fee_pct)
                    capital += net
                    pnl = net - pos["cash_out"]
                    pnl_pct = (pnl / pos["cash_out"]) * 100 if pos["cash_out"] != 0 else 0
                    trade_record = {
                        "token": name, "address": addr, "entry_dt": pos["entry_time"], "exit_dt": ts,
                        "threshold": int(dd_threshold * 100) if dd_threshold is not None else 0,
                        "entry_price": entry_raw, "exit_price": px_raw, "qty": pos["qty"],
                        "slippage": self.slip_pct, "fee_pct": self.fee_pct, "pnl": pnl, "pnl_pct": pnl_pct,
                        "balance": capital, "reason": exit_reason, "tp_pct": self.tp_pct, "sl_pct": self.sl_pct,
                        "cash_out": self.per_trade_usd,
                        "boosts": self.current_tokens.get(addr, {}).get("boosts", 0),
                        "fdv": self.current_tokens.get(addr, {}).get("fdv", 0),
                        "entry_dd_pct": pos.get("entry_dd_pct", 0)
                    }
                    trades.append(trade_record)
                    del open_pos[addr];
                    processed_tokens.add(addr);
                    continue

            if addr not in seen_first_tick:
                seen_first_tick.add(addr);
                boost_price[addr] = price

            if self.entry_mode == "drawdown_local":
                win = local_windows.get(addr, []);
                local_windows[addr] = win;
                win.append((ts, price))
                while win and win[0][0] < ts - self.high_window: win.pop(0)
                local_max = max(p for _, p in win) if win else price
            else:
                local_max = 0

            if (addr not in open_pos) and (addr not in processed_tokens) and (capital >= self.per_trade_usd):
                should_enter = False
                entry_dd_pct = 0
                if self.entry_mode == "instant_boost":
                    should_enter = True
                elif self.entry_mode == "drawdown_boost" and dd_threshold is not None:
                    if price <= boost_price.get(addr, price) * (1.0 - dd_threshold):
                        should_enter = True
                        if boost_price.get(addr, 0) > 0:
                            entry_dd_pct = (1 - price / boost_price.get(addr, price)) * 100
                elif self.entry_mode == "drawdown_local" and dd_threshold is not None:
                    if local_max > 0 and price <= local_max * (1.0 - dd_threshold):
                        should_enter = True
                        entry_dd_pct = (1 - price / local_max) * 100

                if should_enter:
                    invest_cash = self.per_trade_usd * (1.0 - self.fee_pct)
                    if invest_cash > 0:
                        qty = invest_cash / (price * (1.0 + self.slip_pct))
                        capital -= self.per_trade_usd
                        open_pos[addr] = {"entry_time": ts, "entry_raw": price, "qty": qty,
                                          "cash_out": self.per_trade_usd, "entry_dd_pct": entry_dd_pct}
                        entered_tokens.add(addr)

        if open_pos:
            capital += len(open_pos) * self.per_trade_usd

        return capital, trades, entered_tokens


class TradesTable(QtWidgets.QTableWidget):
    HEADERS = ["Токен", "Адрес", "Вход", "Выход", "Порог(%)", "TP(%)", "SL(%)", "Сумма($)", "PnL $", "PnL %",
               "Баланс $", "Причина"]
    progress_updated = QtCore.pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent);
        self.setColumnCount(len(self.HEADERS));
        self.setHorizontalHeaderLabels(self.HEADERS);
        self.setSortingEnabled(True)
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents);
        self.verticalHeader().setVisible(False);
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)

    def load_rows_with_progress(self, trades: List[Dict]):
        self.setSortingEnabled(False)
        self.setRowCount(0)
        total = len(trades)
        for i, tr in enumerate(trades):
            r = self.rowCount()
            self.insertRow(r)
            cells = [
                tr["token"], tr["address"], tr["entry_dt"].strftime("%Y-%m-%d %H:%M"),
                tr["exit_dt"].strftime("%Y-%m-%d %H:%M"),
                ("-" if tr["threshold"] == 0 else f'-{tr["threshold"]}'), f'{tr.get("tp_pct", 0) * 100:.0f}',
                f'{tr.get("sl_pct", 0) * 100:.0f}', f'{tr["cash_out"]:.2f}', f'{tr["pnl"]:.2f}',
                f'{tr.get("pnl_pct", 0):.2f}%', f'{tr["balance"]:.2f}', tr["reason"]
            ]
            for c, val in enumerate(cells):
                item = NumericTableWidgetItem(val) if c >= 7 else QtWidgets.QTableWidgetItem(val)
                if c >= 7: item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if c in [8, 9]:
                    pnl_val = tr.get("pnl", 0.0)
                    color = QtGui.QColor("green") if pnl_val > 0 else (
                        QtGui.QColor("red") if pnl_val < 0 else QtGui.QColor("black"))
                    item.setForeground(color)
                self.setItem(r, c, item)

            if i % 500 == 0 and total > 0:
                self.progress_updated.emit(int((i / total) * 100))
                QtWidgets.QApplication.processEvents()

        self.setSortingEnabled(True)


class DateAxis(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M") for value in values]


class ChartWindow(QtWidgets.QDialog):
    def __init__(self, token_name, ticks, entry_trade, exit_trade, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"График: {token_name}")
        self.resize(1000, 600)

        self.ticks = ticks
        self.entry_trade = entry_trade
        self.exit_trade = exit_trade

        layout = QtWidgets.QVBoxLayout(self)
        self.plot_widget = pg.PlotWidget(axisItems={'bottom': DateAxis(orientation='bottom')})
        layout.addWidget(self.plot_widget)

        self._plot_data()

    def _plot_data(self):
        self.plot_widget.setBackground('w')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.setTitle(self.windowTitle(), color='k', size='12pt')

        x = [dt.timestamp() for dt, price in self.ticks]
        y = [price for dt, price in self.ticks]

        self.plot_widget.plot(x, y, pen=pg.mkPen('b', width=2))

        # Entry Point
        entry_x = self.entry_trade['entry_dt'].timestamp()
        entry_y = self.entry_trade['entry_price']
        entry_scatter = pg.ScatterPlotItem(x=[entry_x], y=[entry_y], symbol='o', size=25,
                                           pen=pg.mkPen('k', width=2), brush=pg.mkBrush(0, 255, 0, 200))
        entry_text = pg.TextItem(text='Вход', color=(0, 100, 0), anchor=(0.5, 1.5))
        entry_text.setPos(entry_x, entry_y)
        self.plot_widget.addItem(entry_scatter)
        self.plot_widget.addItem(entry_text)

        # Exit Point
        exit_x = self.exit_trade['exit_dt'].timestamp()
        exit_y = self.exit_trade['exit_price']
        exit_scatter = pg.ScatterPlotItem(x=[exit_x], y=[exit_y], symbol='o', size=25,
                                          pen=pg.mkPen('k', width=2), brush=pg.mkBrush(255, 0, 0, 200))
        exit_text = pg.TextItem(text='Выход', color=(150, 0, 0), anchor=(0.5, 1.5))
        exit_text.setPos(exit_x, exit_y)
        self.plot_widget.addItem(exit_scatter)
        self.plot_widget.addItem(exit_text)


class DetailedTradesWindow(QtWidgets.QDialog):
    HEADERS = ["Токен", "CA", "FDV ($)", "Бусты", "Вход (MSK)", "Выход (MSK)", "Просадка входа (%)", "Цена входа $",
               "Цена выхода $", "PnL $", "PnL %",
               "Причина"]

    def __init__(self, trades: List[Dict], all_tokens: Dict, params_str: str, parent=None):
        super().__init__(parent)
        self.trades = trades
        self.all_tokens = all_tokens
        self.setWindowTitle(f"Детализация сделок: {params_str}")
        self.settings = QtCore.QSettings(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "optimizer_settings.ini"), QtCore.QSettings.IniFormat)

        layout = QtWidgets.QVBoxLayout(self)
        self.table = QtWidgets.QTableWidget()
        layout.addWidget(self.table)

        self.table.setColumnCount(len(self.HEADERS));
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSortingEnabled(True);
        self.table.verticalHeader().setVisible(False);
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.load_rows(trades)

        self.table.cellDoubleClicked.connect(self._show_chart)
        self._load_geometry()

    def _load_geometry(self):
        try:
            geometry = self.settings.value("DetailedTradesGeometry")
            if geometry:
                self.restoreGeometry(geometry)
            else:
                self.resize(1200, 600)

            header_state = self.settings.value("DetailedTradesHeader")
            if header_state:
                self.table.horizontalHeader().restoreState(header_state)
            else:
                self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        except Exception as e:
            print(f"Не удалось загрузить геометрию окна детализации: {e}")
            self.resize(1200, 600)

    def _save_geometry(self):
        self.settings.setValue("DetailedTradesGeometry", self.saveGeometry())
        self.settings.setValue("DetailedTradesHeader", self.table.horizontalHeader().saveState())

    def closeEvent(self, event):
        self._save_geometry()
        super().closeEvent(event)

    def _show_chart(self, row, column):
        item = self.table.item(row, 0)
        if not item: return
        trade_data = item.data(QtCore.Qt.UserRole)

        token_address = trade_data["address"]
        token_name = trade_data["token"]

        if token_address in self.all_tokens:
            ticks = self.all_tokens[token_address]["ticks"]
            if not ticks:
                QtWidgets.QMessageBox.warning(self, "Ошибка", f"Нет данных о ценах (тиков) для токена {token_address}")
                return
            chart_dialog = ChartWindow(token_name, ticks, trade_data, trade_data, self)
            chart_dialog.exec_()
        else:
            QtWidgets.QMessageBox.warning(self, "Ошибка", f"Не найдены данные для токена {token_address}")

    def contextMenuEvent(self, event):
        item = self.table.itemAt(event.pos())
        if not item: return

        trade_data = self.table.item(item.row(), 0).data(QtCore.Qt.UserRole)
        if not trade_data: return

        address = trade_data["address"]
        url = f"https://dexscreener.com/solana/{address}"

        menu = QMenu(self)
        copy_action = menu.addAction("Копировать ссылку на DexScreener")
        open_action = menu.addAction("Открыть ссылку на DexScreener")

        action = menu.exec_(self.table.mapToGlobal(event.pos()))

        if action == copy_action:
            clipboard = QtWidgets.QApplication.clipboard()
            clipboard.setText(url)
        elif action == open_action:
            webbrowser.open(url)

    def load_rows(self, trades: List[Dict]):
        self.table.setSortingEnabled(False);
        self.table.setRowCount(0)

        NUMERIC_COLS = {2, 3, 7, 8, 9, 10}
        CENTER_COLS = {4, 5, 6, 11}

        for tr in trades:
            r = self.table.rowCount();
            self.table.insertRow(r)
            msk_offset = timedelta(hours=3)
            entry_msk = tr["entry_dt"] + msk_offset;
            exit_msk = tr["exit_dt"] + msk_offset
            dd_pct = tr.get("entry_dd_pct", 0)
            dd_str = f"{dd_pct:.2f}%" if dd_pct > 0 else "-"
            cells = [tr["token"], tr["address"], f'{tr.get("fdv", 0):,}', str(tr.get("boosts", "N/A")),
                     entry_msk.strftime("%Y-%m-%d %H:%M:%S"), exit_msk.strftime("%Y-%m-%d %H:%M:%S"),
                     dd_str, f'{tr["entry_price"]:.8f}', f'{tr["exit_price"]:.8f}', f'{tr["pnl"]:.2f}',
                     f'{tr.get("pnl_pct", 0):.2f}%', tr["reason"]]
            for c, val in enumerate(cells):
                item = NumericTableWidgetItem(val) if c in NUMERIC_COLS else QtWidgets.QTableWidgetItem(val)

                if c in NUMERIC_COLS:
                    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                elif c in CENTER_COLS:
                    item.setTextAlignment(QtCore.Qt.AlignCenter)

                if c in [9, 10]:
                    pnl_val = tr.get("pnl", 0.0)
                    color = QtGui.QColor("green") if pnl_val > 0 else (
                        QtGui.QColor("red") if pnl_val < 0 else QtGui.QColor("black"))
                    item.setForeground(color)
                self.table.setItem(r, c, item)
            self.table.item(r, 0).setData(QtCore.Qt.UserRole, tr)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(4, QtCore.Qt.AscendingOrder)


class SummaryTable(QtWidgets.QTableWidget):
    HEADERS = ["FDV мин ($)", "Бусты мин", "Порог (%)", "TP (%)", "SL (%)", "Проскальз. (%)", "Сумма ($)",
               "Итоговый баланс $", "Сделок", "Побед", "Убытков", "Стопов",
               "Суммарный PnL $", "Средний PnL/сделку $"]
    progress_updated = QtCore.pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent);
        self.setColumnCount(len(self.HEADERS));
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setSortingEnabled(True);
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.verticalHeader().setVisible(False);
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)

    def load_summary_with_progress(self, summary: Dict, best_key: str, worst_key: str):
        self.setSortingEnabled(False);
        self.setRowCount(0)

        def fmt(x: float):
            return f"{x:.2f}"

        summary_items = list(summary.items())
        total = len(summary_items)

        for i, (key, rec) in enumerate(summary_items):
            r = self.rowCount();
            self.insertRow(r)
            th_txt = "-" if rec["threshold"] is None else f'-{rec["threshold"]}'
            pnl_sum = rec.get("pnl_sum", 0.0);
            count = rec.get("count", 0)
            avg_pnl = (pnl_sum / count) if count > 0 else 0.0

            cells = [
                f'{rec.get("fdv", 0):,}', str(rec.get("boosts", 0)), th_txt, str(rec["tp"]), str(rec["sl"]),
                str(rec["slip"]), str(rec["trade_size"]),
                fmt(rec["capital"]), str(count), str(rec.get("wins", 0)), str(rec.get("losses", 0)),
                str(rec.get("stops", 0)),
                fmt(pnl_sum), fmt(avg_pnl)
            ]
            for c, val in enumerate(cells):
                item = NumericTableWidgetItem(val) if c not in [2] else QtWidgets.QTableWidgetItem(val)
                if c >= 7: item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if c in [12, 13]:
                    try:
                        pnl_value = float(val.replace(',', '').replace('$', '').strip())
                        color = QtGui.QColor("green") if pnl_value > 0 else (
                            QtGui.QColor("red") if pnl_value < 0 else QtGui.QColor("black"))
                        item.setForeground(color)
                    except ValueError:
                        pass
                self.setItem(r, c, item)

            self.item(r, 0).setData(QtCore.Qt.UserRole, rec)
            if key == best_key:
                self.row_highlight(r, QtGui.QColor(220, 255, 220))
            elif key == worst_key:
                self.row_highlight(r, QtGui.QColor(255, 230, 230))

            if i % 100 == 0 and total > 0:
                self.progress_updated.emit(int((i / total) * 100))
                QtWidgets.QApplication.processEvents()

        self.setSortingEnabled(True)
        self.sortByColumn(7, QtCore.Qt.DescendingOrder)  # Sort by final balance by default

    def row_highlight(self, row: int, color: QtGui.QColor):
        for col in range(self.columnCount()):
            it = self.item(row, col);
            if it: it.setBackground(color)


class SavedStrategiesTable(QtWidgets.QTableWidget):
    HEADERS = ["FDV мин ($)", "Бусты мин", "Порог (%)", "TP (%)", "SL (%)", "Проскальз. (%)", "Сумма ($)",
               "Итоговый баланс $", "Сделок", "Побед", "Убытков", "Стопов",
               "Суммарный PnL $"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(self.HEADERS))
        self.setHorizontalHeaderLabels(self.HEADERS)
        self.setSortingEnabled(True)
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)

    def load_strategies(self, strategies: List[Dict]):
        self.setSortingEnabled(False)
        self.setRowCount(0)

        def fmt(x: float):
            return f"{x:.2f}"

        for strategy_data in strategies:
            r = self.rowCount()
            self.insertRow(r)

            th_txt = "-" if strategy_data["threshold"] is None else f'-{strategy_data["threshold"]}'

            cells = [
                f'{strategy_data.get("fdv", 0):,}', str(strategy_data.get("boosts", 0)), th_txt,
                str(strategy_data["tp"]), str(strategy_data["sl"]),
                str(strategy_data["slip"]), str(strategy_data["trade_size"]),
                fmt(strategy_data["capital"]), str(strategy_data["count"]), str(strategy_data.get("wins", 0)),
                str(strategy_data.get("losses", 0)),
                str(strategy_data.get("stops", 0)), fmt(strategy_data["pnl_sum"])
            ]

            for c, val in enumerate(cells):
                item = NumericTableWidgetItem(val) if c not in [2] else QtWidgets.QTableWidgetItem(val)
                self.setItem(r, c, item)

            self.item(r, 0).setData(QtCore.Qt.UserRole, strategy_data)

        self.setSortingEnabled(True)


class OptimizerWorker(QtCore.QThread):
    progressed = QtCore.pyqtSignal(str)
    progress_updated = QtCore.pyqtSignal(int)
    finishedOK = QtCore.pyqtSignal(dict, list, tuple, dict)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, params: Dict, parent=None):
        super().__init__(parent)
        self.params = params
        self.mutex = QtCore.QMutex()
        self.is_paused = False
        self.is_stopped = False

    def stop(self):
        self.mutex.lock()
        self.is_stopped = True
        self.mutex.unlock()

    def pause(self):
        self.mutex.lock()
        self.is_paused = True
        self.mutex.unlock()

    def resume(self):
        self.mutex.lock()
        self.is_paused = False
        self.mutex.unlock()

    def run(self):
        try:
            self.progressed.emit("Загрузка БД...")
            loader = DataLoader(self.params["path"]);
            loader.load()

            if self.params.get("date_filter_enabled"):
                start_date = self.params["start_date"];
                end_date = self.params["end_date"]
                for addr, rec in loader.tokens.items():
                    rec['ticks'] = [(ts, price) for ts, price in rec['ticks'] if start_date <= ts <= end_date]
                loader.tokens = {addr: rec for addr, rec in loader.tokens.items() if rec['ticks']}

            diag_total, diag_with_ticks, _ = loader.diagnostics()
            if not loader.tokens: self.failed.emit("Нет токенов с данными за указанный период."); return

            initial_tokens = loader.tokens

            bt = Backtester(tokens=initial_tokens,
                            deposit_usd=self.params["deposit"], per_trade_usd=self.params["per_trade"],
                            tp_pct=self.params["tp"], sl_pct=self.params["sl"], hold_days=self.params["hold_days"],
                            fee_pct=self.params["fee"], slip_pct=self.params["slip"],
                            entry_mode=self.params["entry_mode"],
                            high_window_minutes=self.params["high_window"])

            thresholds = list(range(10, 91, 10));
            slip_values = list(range(5, 10)) if self.params.get("slip_sweep") else [int(self.params["slip"] * 100)]
            tp_values = list(range(10, 51, 10)) if self.params.get("tp_sweep") else [int(self.params["tp"] * 100)]

            sl_values = [int(self.params["sl"] * 100)]
            if self.params.get("sl_sweep_tight"):
                sl_values = list(range(10, 31, 10))
            elif self.params.get("sl_sweep"):
                sl_values = list(range(30, 81, 10))

            trade_size_values = list(range(5, 51, 5)) if self.params.get("trade_size_sweep") else [
                self.params["per_trade"]]
            fdv_values = list(range(30000, 200001, 10000)) if self.params.get("fdv_sweep") else [
                self.params.get("min_fdv") if self.params.get("fdv_filter_enabled") else 0]

            boost_values = [self.params.get("min_boosts") if self.params.get("boost_filter_enabled") else 0]
            if self.params.get("boost_sweep"):
                max_boost_in_data = max((rec.get("boosts", 0) or 0 for rec in initial_tokens.values()), default=0)
                boost_range_limit = min(500, max_boost_in_data)
                boost_values = list(range(10, int(boost_range_limit) + 1, 10))

            th_list = [None] if self.params["entry_mode"] == "instant_boost" else [t / 100.0 for t in thresholds]

            total_iterations = len(boost_values) * len(fdv_values) * len(trade_size_values) * len(tp_values) * len(
                sl_values) * len(th_list) * len(slip_values)
            current_iteration = 0

            best = (-1e18,) + (None,) * 7
            worst = (1e18,) + (None,) * 7
            all_trades, summary, all_entered = [], {}, set()

            self.progressed.emit("Запуск оптимизации...")

            for min_boosts in boost_values:
                tokens_after_boost_filter = {k: v for k, v in initial_tokens.items() if
                                             (v.get("boosts", 0) or 0) >= min_boosts}

                for min_fdv in fdv_values:
                    current_tokens = {k: v for k, v in tokens_after_boost_filter.items() if
                                      (v.get("fdv", 0) or 0) >= min_fdv}

                    if not current_tokens:
                        current_iteration += len(trade_size_values) * len(tp_values) * len(sl_values) * len(
                            th_list) * len(slip_values)
                        if total_iterations > 0:
                            progress = int((current_iteration / total_iterations) * 100)
                            self.progress_updated.emit(progress)
                        continue

                    events = loader.get_filtered_stream(current_tokens)
                    bt.set_active_data(current_tokens, events)

                    for trade_size in trade_size_values:
                        bt.per_trade_usd = float(trade_size)
                        for tp in tp_values:
                            bt.tp_pct = tp / 100.0
                            for sl in sl_values:
                                bt.sl_pct = sl / 100.0
                                for dd in th_list:
                                    for slip in slip_values:
                                        self.mutex.lock()
                                        if self.is_stopped:
                                            self.mutex.unlock()
                                            return
                                        while self.is_paused:
                                            self.mutex.unlock()
                                            self.msleep(100)
                                            self.mutex.lock()
                                        self.mutex.unlock()

                                        bt.slip_pct = slip / 100.0

                                        cap, trades, entered = bt.run_for_threshold(dd)
                                        all_entered |= entered

                                        key_tuple = (
                                        int(dd * 100) if dd else None, int(slip), int(tp), int(sl), int(trade_size),
                                        int(min_fdv), int(min_boosts))
                                        for t in trades: t["_key"] = key_tuple
                                        all_trades.extend(trades)

                                        th_k = "-" if dd is None else f"{int(dd * 100)}%"
                                        summary_key = f"fdv>={int(min_fdv)}|b>={int(min_boosts)}|{th_k}|tp={int(tp)}%|sl={int(sl)}%|slip={int(slip)}%|trade=${int(trade_size)}"

                                        wins = sum(1 for t in trades if t['pnl'] > 0)
                                        losses = len(trades) - wins
                                        stops = sum(1 for t in trades if t['reason'] == 'STOP')

                                        summary[summary_key] = {
                                            "capital": cap, "threshold": key_tuple[0], "slip": key_tuple[1],
                                            "tp": key_tuple[2], "sl": key_tuple[3], "trade_size": key_tuple[4],
                                            "fdv": key_tuple[5], "boosts": key_tuple[6],
                                            "count": len(trades), "pnl_sum": sum(t["pnl"] for t in trades),
                                            "wins": wins, "losses": losses, "stops": stops, "_key": key_tuple
                                        }

                                        if cap > best[0]: best = (
                                        cap, key_tuple[0], slip / 100, tp / 100, sl / 100, trade_size, min_fdv,
                                        min_boosts)
                                        if cap < worst[0]: worst = (
                                        cap, key_tuple[0], slip / 100, tp / 100, sl / 100, trade_size, min_fdv,
                                        min_boosts)

                                        current_iteration += 1
                                        if total_iterations > 0:
                                            progress = int((current_iteration / total_iterations) * 100)
                                            self.progress_updated.emit(progress)

            info = {"best": {"capital": best[0], "threshold": best[1], "slip": best[2], "tp": best[3], "sl": best[4],
                             "trade_size": best[5], "fdv": best[6], "boosts": best[7]},
                    "worst": {"capital": worst[0], "threshold": worst[1], "slip": worst[2], "tp": worst[3],
                              "sl": worst[4], "trade_size": worst[5], "fdv": worst[6], "boosts": worst[7]},
                    "summary": summary}

            self.finishedOK.emit(info, all_trades, (diag_total, diag_with_ticks, 0, len(all_entered)), initial_tokens)
        except Exception as e:
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


class SingleTestWorker(QtCore.QThread):
    finished = QtCore.pyqtSignal(list, dict, str)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, params: Dict, strategy_params: Dict, parent=None):
        super().__init__(parent)
        self.params = params
        self.strategy_params = strategy_params

    def run(self):
        try:
            loader = DataLoader(self.params["path"])
            loader.load()

            if self.params.get("date_filter_enabled"):
                start_date = self.params["start_date"]
                end_date = self.params["end_date"]
                for addr, rec in loader.tokens.items():
                    rec['ticks'] = [(ts, price) for ts, price in rec['ticks'] if start_date <= ts <= end_date]
                loader.tokens = {addr: rec for addr, rec in loader.tokens.items() if rec['ticks']}

            if not loader.tokens: self.failed.emit("Нет токенов с данными за указанный период."); return

            initial_tokens = loader.tokens

            min_fdv = self.strategy_params.get('fdv', 0)
            min_boosts = self.strategy_params.get('boosts', 0)

            tokens_after_boost_filter = {k: v for k, v in initial_tokens.items() if
                                         (v.get("boosts", 0) or 0) >= min_boosts}
            current_tokens = {k: v for k, v in tokens_after_boost_filter.items() if (v.get("fdv", 0) or 0) >= min_fdv}

            if not current_tokens:
                self.finished.emit([], initial_tokens, "Нет токенов, соответствующих критериям стратегии.")
                return

            events = loader.get_filtered_stream(current_tokens)

            bt = Backtester(tokens=initial_tokens,
                            deposit_usd=self.params["deposit"], per_trade_usd=self.strategy_params["trade_size"],
                            tp_pct=self.strategy_params["tp"] / 100.0, sl_pct=self.strategy_params["sl"] / 100.0,
                            hold_days=self.params["hold_days"], fee_pct=self.params["fee"],
                            slip_pct=self.strategy_params["slip"] / 100.0,
                            entry_mode=self.params["entry_mode"],
                            high_window_minutes=self.params["high_window"])

            bt.set_active_data(current_tokens, events)

            dd_threshold = self.strategy_params["threshold"]
            if dd_threshold is not None:
                dd_threshold /= 100.0

            _, trades, _ = bt.run_for_threshold(dd_threshold)

            self.finished.emit(trades, initial_tokens, "Тест завершен.")

        except Exception as e:
            self.failed.emit(f"{e}\n{traceback.format_exc()}")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__();
        self.setWindowTitle("Оптимизатор стратегий (PyQt5) v22.4");
        self.resize(1320, 840)
        self.all_trades = [];
        self.all_tokens_data = {}
        self.detail_window = None
        self.saved_strategies = []
        self.last_test_results = None

        # --- UI Elements ---
        self.path_edit = QtWidgets.QLineEdit()
        self.btn_browse = QtWidgets.QPushButton("…")
        path_layout = QtWidgets.QHBoxLayout();
        path_layout.addWidget(QtWidgets.QLabel("БД JSONL:"));
        path_layout.addWidget(self.path_edit, 1);
        path_layout.addWidget(self.btn_browse)

        self.dep_spin = QtWidgets.QDoubleSpinBox();
        self.dep_spin.setRange(0, 1e9);
        self.dep_spin.setGroupSeparatorShown(True)
        self.per_trade_spin = QtWidgets.QDoubleSpinBox();
        self.per_trade_spin.setRange(5, 1e6);
        self.per_trade_spin.setGroupSeparatorShown(True)
        self.tp_spin = QtWidgets.QDoubleSpinBox();
        self.tp_spin.setRange(0.0, 100.0);
        self.tp_spin.setSuffix(" %")
        self.sl_spin = QtWidgets.QDoubleSpinBox();
        self.sl_spin.setRange(0.0, 100.0);
        self.sl_spin.setSuffix(" %")
        self.hold_days_spin = QtWidgets.QSpinBox();
        self.hold_days_spin.setRange(1, 3650);
        self.fee_spin = QtWidgets.QDoubleSpinBox();
        self.fee_spin.setRange(0.0, 10.0);
        self.fee_spin.setSuffix(" %");
        self.fee_spin.setDecimals(2)
        self.slip_spin = QtWidgets.QDoubleSpinBox();
        self.slip_spin.setRange(0.0, 50.0);
        self.slip_spin.setSuffix(" %")
        self.cb_trade_size_sweep = QtWidgets.QCheckBox("Прогнать сумму на сделку ($5-$50)")
        self.cb_tp_sweep = QtWidgets.QCheckBox("Прогнать Take-Profit (10-50%)")
        self.cb_sl_sweep_tight = QtWidgets.QCheckBox("Прогнать Stop-Loss (10-30%)")
        self.cb_sl_sweep = QtWidgets.QCheckBox("Прогнать Stop-Loss (30-80%)")
        self.cb_slip_sweep = QtWidgets.QCheckBox("Прогнать проскальзывание 5–9%")
        self.cb_fdv_sweep = QtWidgets.QCheckBox("Прогнать FDV (от $30k)")
        self.cb_boost_sweep = QtWidgets.QCheckBox("Прогнать по бустам (10-500, шаг 10)")
        self.entry_mode = QtWidgets.QComboBox();
        self.entry_mode.addItems(["Сразу при бусте", "Просадка от локального хая (окно)", "Просадка от цены буста"])
        self.high_window_spin = QtWidgets.QSpinBox();
        self.high_window_spin.setRange(1, 1440);
        self.high_window_spin.setSuffix(" мин")

        params_layout = QtWidgets.QVBoxLayout()
        form_layout = QtWidgets.QFormLayout()
        form_layout.addRow("Депозит $:", self.dep_spin);
        form_layout.addRow("Сумма на сделку $:", self.per_trade_spin)
        form_layout.addRow("Тейк-профит:", self.tp_spin);
        form_layout.addRow("Стоп-лосс:", self.sl_spin)
        form_layout.addRow("Макс. удержание (дней):", self.hold_days_spin);
        form_layout.addRow("Комиссия за сторону:", self.fee_spin)
        form_layout.addRow("Проскальзывание:", self.slip_spin);
        form_layout.addRow("Триггер входа:", self.entry_mode)
        form_layout.addRow("Окно локального хая:", self.high_window_spin)

        params_layout.addLayout(form_layout)

        sweep_group = QtWidgets.QGroupBox("Опции перебора (грид-серч)")
        sweep_grid_layout = QtWidgets.QGridLayout(sweep_group)
        sweep_grid_layout.addWidget(self.cb_trade_size_sweep, 0, 0)
        sweep_grid_layout.addWidget(self.cb_tp_sweep, 1, 0)
        sweep_grid_layout.addWidget(self.cb_sl_sweep_tight, 2, 0)
        sweep_grid_layout.addWidget(self.cb_sl_sweep, 3, 0)
        sweep_grid_layout.addWidget(self.cb_slip_sweep, 0, 1)
        sweep_grid_layout.addWidget(self.cb_fdv_sweep, 1, 1)
        sweep_grid_layout.addWidget(self.cb_boost_sweep, 2, 1)

        params_layout.addWidget(sweep_group)
        params_group = QtWidgets.QGroupBox("Параметры");
        params_group.setLayout(params_layout)

        filters_layout = QtWidgets.QVBoxLayout()
        date_group = QtWidgets.QGroupBox("Фильтр по дате")
        date_form_layout = QtWidgets.QFormLayout(date_group)
        self.cb_date_filter = QtWidgets.QCheckBox("Активировать фильтр по дате")
        self.start_date_edit = QtWidgets.QDateEdit(calendarPopup=True);
        self.start_date_edit.setDisplayFormat("yyyy-MM-dd");
        self.end_date_edit = QtWidgets.QDateEdit(calendarPopup=True);
        self.end_date_edit.setDisplayFormat("yyyy-MM-dd");
        date_form_layout.addRow(self.cb_date_filter);
        date_form_layout.addRow("С:", self.start_date_edit);
        date_form_layout.addRow("По:", self.end_date_edit)

        boost_group = QtWidgets.QGroupBox("Фильтр по бустам")
        boost_layout = QtWidgets.QFormLayout(boost_group)
        self.cb_boost_filter = QtWidgets.QCheckBox("Активировать фильтр")
        self.min_boost_spin = QtWidgets.QSpinBox();
        self.min_boost_spin.setRange(0, 10000)
        self.max_boost_spin = QtWidgets.QSpinBox();
        self.max_boost_spin.setRange(0, 10000)
        boost_layout.addRow(self.cb_boost_filter);
        boost_layout.addRow("Мин. бустов:", self.min_boost_spin);
        boost_layout.addRow("Макс. бустов (0 = без лимита):", self.max_boost_spin)

        fdv_group = QtWidgets.QGroupBox("Фильтр по FDV")
        fdv_layout = QtWidgets.QFormLayout(fdv_group)
        self.cb_fdv_filter = QtWidgets.QCheckBox("Активировать фильтр")
        self.min_fdv_spin = QtWidgets.QDoubleSpinBox()
        self.min_fdv_spin.setRange(0, 1e12)
        self.min_fdv_spin.setSuffix(" $")
        self.min_fdv_spin.setDecimals(0)
        self.min_fdv_spin.setGroupSeparatorShown(True)
        fdv_layout.addRow(self.cb_fdv_filter)
        fdv_layout.addRow("Мин. FDV:", self.min_fdv_spin)

        filters_layout.addWidget(date_group);
        filters_layout.addWidget(boost_group);
        filters_layout.addWidget(fdv_group);
        filters_layout.addStretch()

        self.run_btn = QtWidgets.QPushButton("Оптимизировать");
        self.btn_reset = QtWidgets.QPushButton("Сбросить установки")
        self.btn_pause = QtWidgets.QPushButton("Пауза")
        self.btn_resume = QtWidgets.QPushButton("Продолжить")
        self.btn_stop = QtWidgets.QPushButton("Стоп")

        self.progress_label = QtWidgets.QLabel("Готово.")
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setVisible(False)
        self.btn_pause.setVisible(False)
        self.btn_resume.setVisible(False)
        self.btn_stop.setVisible(False)

        buttons_layout = QtWidgets.QHBoxLayout()
        buttons_layout.addWidget(self.run_btn)
        buttons_layout.addWidget(self.btn_reset)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self.btn_pause)
        buttons_layout.addWidget(self.btn_resume)
        buttons_layout.addWidget(self.btn_stop)

        progress_layout = QtWidgets.QHBoxLayout()
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar, 1)

        top_layout = QtWidgets.QVBoxLayout()
        top_layout.addLayout(path_layout)
        h_layout = QtWidgets.QHBoxLayout();
        h_layout.addWidget(params_group, 1)
        h_layout.addLayout(filters_layout);
        top_layout.addLayout(h_layout)
        top_layout.addLayout(buttons_layout);
        top_layout.addLayout(progress_layout);

        self.tabs = QtWidgets.QTabWidget();
        self.trades_table = TradesTable();
        self.summary_table = SummaryTable()

        summary_widget = QtWidgets.QWidget()
        summary_layout = QtWidgets.QVBoxLayout(summary_widget)

        sort_buttons_layout = QtWidgets.QHBoxLayout()
        self.btn_sort_profit = QtWidgets.QPushButton("Макс. профит")
        self.btn_sort_losses = QtWidgets.QPushButton("Мин. убытков")
        self.btn_sort_stops = QtWidgets.QPushButton("Мин. стопов")
        sort_buttons_layout.addWidget(self.btn_sort_profit)
        sort_buttons_layout.addWidget(self.btn_sort_losses)
        sort_buttons_layout.addWidget(self.btn_sort_stops)
        sort_buttons_layout.addStretch()

        summary_layout.addLayout(sort_buttons_layout)
        summary_layout.addWidget(self.summary_table)

        self.summary_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.summary_table.customContextMenuRequested.connect(self._show_summary_context_menu)

        # New "Strategies" Tab
        self.saved_strategies_table = SavedStrategiesTable()
        strategies_widget = QtWidgets.QWidget()
        strategies_layout = QtWidgets.QVBoxLayout(strategies_widget)

        strat_buttons_layout = QtWidgets.QHBoxLayout()
        self.btn_test_strategy = QtWidgets.QPushButton("Тестировать стратегию")
        self.btn_show_test_results = QtWidgets.QPushButton("Результаты тестов")
        self.strat_status_label = QtWidgets.QLabel("Выберите стратегию для теста.")
        self.btn_show_test_results.setEnabled(False)
        strat_buttons_layout.addWidget(self.btn_test_strategy)
        strat_buttons_layout.addWidget(self.btn_show_test_results)
        strat_buttons_layout.addStretch()
        strat_buttons_layout.addWidget(self.strat_status_label)

        strategies_layout.addLayout(strat_buttons_layout)
        strategies_layout.addWidget(self.saved_strategies_table)

        self.tabs.addTab(self.trades_table, "Все сделки");
        self.tabs.addTab(summary_widget, "Сводка")
        self.tabs.addTab(strategies_widget, "Стратегии")

        split = QtWidgets.QSplitter(QtCore.Qt.Vertical);
        top_widget = QtWidgets.QWidget();
        top_widget.setLayout(top_layout)
        split.addWidget(top_widget);
        split.addWidget(self.tabs);
        split.setStretchFactor(1, 1)
        central = QtWidgets.QWidget();
        main_layout = QtWidgets.QVBoxLayout(central);
        main_layout.addWidget(split)
        self.setCentralWidget(central);
        self.summary = QtWidgets.QLabel("");
        self.statusBar().addPermanentWidget(self.summary, 1)

        # --- Connections & Initial State ---
        self.btn_browse.clicked.connect(self._browse);
        self.run_btn.clicked.connect(self._run)
        self.btn_reset.clicked.connect(self._reset_settings)
        self.btn_pause.clicked.connect(self._pause_worker)
        self.btn_resume.clicked.connect(self._resume_worker)
        self.btn_stop.clicked.connect(self._stop_worker)
        self.summary_table.cellDoubleClicked.connect(self._show_detailed_trades)

        self.btn_sort_profit.clicked.connect(self._sort_by_profit)
        self.btn_sort_losses.clicked.connect(self._sort_by_losses)
        self.btn_sort_stops.clicked.connect(self._sort_by_stops)

        self.btn_test_strategy.clicked.connect(self._test_saved_strategy)
        self.btn_show_test_results.clicked.connect(self._show_saved_strategy_results)

        self.trades_table.progress_updated.connect(self.progress_bar.setValue)
        self.summary_table.progress_updated.connect(self.progress_bar.setValue)

        self.worker = None
        self.test_worker = None

        self._set_default_values()
        self._load_settings()

    def _set_default_values(self):
        self.path_edit.setText(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "price_history.jsonl"))
        self.dep_spin.setValue(500.0)
        self.per_trade_spin.setValue(10.0)
        self.tp_spin.setValue(20.0)
        self.sl_spin.setValue(50.0)
        self.hold_days_spin.setValue(7)
        self.fee_spin.setValue(0.10)
        self.slip_spin.setValue(5.0)
        self.high_window_spin.setValue(60)
        self.entry_mode.setCurrentIndex(0)

        self.cb_trade_size_sweep.setChecked(False)
        self.cb_tp_sweep.setChecked(False)
        self.cb_sl_sweep_tight.setChecked(False)
        self.cb_sl_sweep.setChecked(False)
        self.cb_slip_sweep.setChecked(False)
        self.cb_fdv_sweep.setChecked(False)
        self.cb_boost_sweep.setChecked(False)

        self.cb_date_filter.setChecked(False)
        self.start_date_edit.setDate(QtCore.QDate(2025, 9, 1))
        self.end_date_edit.setDate(QtCore.QDate(2025, 10, 1))

        self.cb_boost_filter.setChecked(False)
        self.min_boost_spin.setValue(0)
        self.max_boost_spin.setValue(0)

        self.cb_fdv_filter.setChecked(False)
        self.min_fdv_spin.setValue(50000)

    def _reset_settings(self):
        reply = QtWidgets.QMessageBox.question(self, 'Сброс настроек',
                                               "Вы уверены, что хотите сбросить все настройки к значениям по умолчанию?",
                                               QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                                               QtWidgets.QMessageBox.No)
        if reply == QtWidgets.QMessageBox.Yes:
            self._set_default_values()

    def _load_settings(self):
        settings = QtCore.QSettings(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "optimizer_settings.ini"), QtCore.QSettings.IniFormat)
        try:
            self.path_edit.setText(settings.value("path", self.path_edit.text()))
            self.dep_spin.setValue(float(settings.value("deposit", self.dep_spin.value())))
            self.per_trade_spin.setValue(float(settings.value("per_trade", self.per_trade_spin.value())))
            self.tp_spin.setValue(float(settings.value("tp", self.tp_spin.value())))
            self.sl_spin.setValue(float(settings.value("sl", self.sl_spin.value())))
            self.hold_days_spin.setValue(int(settings.value("hold_days", self.hold_days_spin.value())))
            self.fee_spin.setValue(float(settings.value("fee", self.fee_spin.value())))
            self.slip_spin.setValue(float(settings.value("slip", self.slip_spin.value())))
            self.high_window_spin.setValue(int(settings.value("high_window", self.high_window_spin.value())))
            self.entry_mode.setCurrentIndex(int(settings.value("entry_mode_index", self.entry_mode.currentIndex())))

            self.cb_trade_size_sweep.setChecked(
                settings.value("trade_size_sweep", self.cb_trade_size_sweep.isChecked(), type=bool))
            self.cb_tp_sweep.setChecked(settings.value("tp_sweep", self.cb_tp_sweep.isChecked(), type=bool))
            self.cb_sl_sweep_tight.setChecked(
                settings.value("sl_sweep_tight", self.cb_sl_sweep_tight.isChecked(), type=bool))
            self.cb_sl_sweep.setChecked(settings.value("sl_sweep", self.cb_sl_sweep.isChecked(), type=bool))
            self.cb_slip_sweep.setChecked(settings.value("slip_sweep", self.cb_slip_sweep.isChecked(), type=bool))
            self.cb_fdv_sweep.setChecked(settings.value("fdv_sweep", self.cb_fdv_sweep.isChecked(), type=bool))
            self.cb_boost_sweep.setChecked(settings.value("boost_sweep", self.cb_boost_sweep.isChecked(), type=bool))

            self.cb_date_filter.setChecked(
                settings.value("date_filter_enabled", self.cb_date_filter.isChecked(), type=bool))
            self.start_date_edit.setDate(settings.value("start_date", self.start_date_edit.date(), type=QtCore.QDate))
            self.end_date_edit.setDate(settings.value("end_date", self.end_date_edit.date(), type=QtCore.QDate))

            self.cb_boost_filter.setChecked(
                settings.value("boost_filter_enabled", self.cb_boost_filter.isChecked(), type=bool))
            self.min_boost_spin.setValue(int(settings.value("min_boosts", self.min_boost_spin.value())))
            self.max_boost_spin.setValue(int(settings.value("max_boosts", self.max_boost_spin.value())))

            self.cb_fdv_filter.setChecked(
                settings.value("fdv_filter_enabled", self.cb_fdv_filter.isChecked(), type=bool))
            self.min_fdv_spin.setValue(float(settings.value("min_fdv", self.min_fdv_spin.value())))

            geometry = settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)

            saved_strategies_json = settings.value("saved_strategies", "[]")
            self.saved_strategies = json.loads(saved_strategies_json)
            self.saved_strategies_table.load_strategies(self.saved_strategies)

        except Exception as e:
            print(f"Не удалось загрузить настройки: {e}")

    def _save_settings(self):
        settings = QtCore.QSettings(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "optimizer_settings.ini"), QtCore.QSettings.IniFormat)

        settings.setValue("path", self.path_edit.text())
        settings.setValue("deposit", self.dep_spin.value())
        settings.setValue("per_trade", self.per_trade_spin.value())
        settings.setValue("tp", self.tp_spin.value())
        settings.setValue("sl", self.sl_spin.value())
        settings.setValue("hold_days", self.hold_days_spin.value())
        settings.setValue("fee", self.fee_spin.value())
        settings.setValue("slip", self.slip_spin.value())
        settings.setValue("high_window", self.high_window_spin.value())
        settings.setValue("entry_mode_index", self.entry_mode.currentIndex())

        settings.setValue("trade_size_sweep", self.cb_trade_size_sweep.isChecked())
        settings.setValue("tp_sweep", self.cb_tp_sweep.isChecked())
        settings.setValue("sl_sweep_tight", self.cb_sl_sweep_tight.isChecked())
        settings.setValue("sl_sweep", self.cb_sl_sweep.isChecked())
        settings.setValue("slip_sweep", self.cb_slip_sweep.isChecked())
        settings.setValue("fdv_sweep", self.cb_fdv_sweep.isChecked())
        settings.setValue("boost_sweep", self.cb_boost_sweep.isChecked())

        settings.setValue("date_filter_enabled", self.cb_date_filter.isChecked())
        settings.setValue("start_date", self.start_date_edit.date())
        settings.setValue("end_date", self.end_date_edit.date())

        settings.setValue("boost_filter_enabled", self.cb_boost_filter.isChecked())
        settings.setValue("min_boosts", self.min_boost_spin.value())
        settings.setValue("max_boosts", self.max_boost_spin.value())

        settings.setValue("fdv_filter_enabled", self.cb_fdv_filter.isChecked())
        settings.setValue("min_fdv", self.min_fdv_spin.value())

        settings.setValue("geometry", self.saveGeometry())

        saved_strategies_json = json.dumps(self.saved_strategies, indent=2)
        settings.setValue("saved_strategies", saved_strategies_json)

    def closeEvent(self, event):
        self._save_settings()
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait()
        super().closeEvent(event)

    def _sort_by_profit(self):
        self.summary_table.sortByColumn(12, QtCore.Qt.DescendingOrder)

    def _sort_by_losses(self):
        self.summary_table.sortByColumn(10, QtCore.Qt.AscendingOrder)

    def _sort_by_stops(self):
        self.summary_table.sortByColumn(11, QtCore.Qt.AscendingOrder)

    def _browse(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Выберите файл", "", "JSONL (*.jsonl);;Все файлы (*.*)");
        if path: self.path_edit.setText(path)

    def _run(self):
        if (self.worker and self.worker.isRunning()) or (self.test_worker and self.test_worker.isRunning()):
            return

        if not self.path_edit.text().strip(): QtWidgets.QMessageBox.warning(self, "Нет файла",
                                                                            "Укажите путь к БД"); return
        params = {"path": self.path_edit.text().strip(), "deposit": self.dep_spin.value(),
                  "per_trade": self.per_trade_spin.value(),
                  "tp": self.tp_spin.value() / 100.0, "sl": self.sl_spin.value() / 100.0,
                  "hold_days": self.hold_days_spin.value(),
                  "fee": self.fee_spin.value() / 100.0, "slip": self.slip_spin.value() / 100.0,
                  "slip_sweep": self.cb_slip_sweep.isChecked(),
                  "tp_sweep": self.cb_tp_sweep.isChecked(),
                  "sl_sweep": self.cb_sl_sweep.isChecked(),
                  "sl_sweep_tight": self.cb_sl_sweep_tight.isChecked(),
                  "trade_size_sweep": self.cb_trade_size_sweep.isChecked(),
                  "fdv_sweep": self.cb_fdv_sweep.isChecked(),
                  "boost_sweep": self.cb_boost_sweep.isChecked(),
                  "entry_mode": ["instant_boost", "drawdown_local", "drawdown_boost"][self.entry_mode.currentIndex()],
                  "high_window": self.high_window_spin.value(),
                  "boost_filter_enabled": self.cb_boost_filter.isChecked(),
                  "min_boosts": self.min_boost_spin.value(), "max_boosts": self.max_boost_spin.value(),
                  "fdv_filter_enabled": self.cb_fdv_filter.isChecked(),
                  "min_fdv": self.min_fdv_spin.value(),
                  "date_filter_enabled": self.cb_date_filter.isChecked(),
                  "start_date": datetime.combine(self.start_date_edit.date().toPyDate(), datetime.min.time()).replace(
                      tzinfo=timezone.utc),
                  "end_date": datetime.combine(self.end_date_edit.date().toPyDate(), datetime.max.time()).replace(
                      tzinfo=timezone.utc)}

        self.run_btn.setEnabled(False)
        self.btn_reset.setEnabled(False)
        self.btn_pause.setVisible(True)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setVisible(True)
        self.btn_stop.setEnabled(True)
        self.btn_resume.setVisible(False)

        self.trades_table.setRowCount(0);
        self.summary_table.setRowCount(0)
        self.summary.setText("");
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self.worker = OptimizerWorker(params);
        self.worker.progressed.connect(self.progress_label.setText)
        self.worker.progress_updated.connect(self.progress_bar.setValue)
        self.worker.failed.connect(self._failed);
        self.worker.finished.connect(self._worker_finished_cleanup)
        self.worker.finishedOK.connect(self._finished);
        self.worker.start()

    def _pause_worker(self):
        if self.worker and self.worker.isRunning():
            self.worker.pause()
            self.btn_pause.setVisible(False)
            self.btn_resume.setVisible(True)
            self.btn_resume.setEnabled(True)
            self.progress_label.setText("Пауза...")

    def _resume_worker(self):
        if self.worker and self.worker.isRunning():
            self.worker.resume()
            self.btn_resume.setVisible(False)
            self.btn_pause.setVisible(True)
            self.btn_pause.setEnabled(True)
            self.progress_label.setText("Запуск оптимизации...")

    def _stop_worker(self):
        if self.worker and self.worker.isRunning():
            reply = QtWidgets.QMessageBox.question(self, 'Остановка',
                                                   "Вы уверены, что хотите остановить оптимизацию?",
                                                   QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                                                   QtWidgets.QMessageBox.No)
            if reply == QtWidgets.QMessageBox.Yes:
                self.worker.stop()
                self.progress_label.setText("Остановка...")
                self.btn_pause.setEnabled(False)
                self.btn_resume.setEnabled(False)
                self.btn_stop.setEnabled(False)

    def _worker_finished_cleanup(self):
        self.run_btn.setEnabled(True)
        self.btn_reset.setEnabled(True)
        self.btn_pause.setVisible(False)
        self.btn_resume.setVisible(False)
        self.btn_stop.setVisible(False)
        self.progress_bar.setVisible(False)
        if self.progress_label.text() != "Готово." and self.progress_label.text() != "Ошибка":
            self.progress_label.setText("Остановлено.")

    def _failed(self, msg: str):
        self.progress_label.setText("Ошибка");
        QtWidgets.QMessageBox.critical(self, "Ошибка", msg)

    def _show_summary_context_menu(self, pos):
        item = self.summary_table.itemAt(pos)
        if not item: return

        strategy_data = self.summary_table.item(item.row(), 0).data(QtCore.Qt.UserRole)
        if not strategy_data: return

        menu = QMenu(self)
        save_action = menu.addAction("Сохранить стратегию")

        action = menu.exec_(self.summary_table.mapToGlobal(pos))

        if action == save_action:
            self.saved_strategies.append(strategy_data)
            self.saved_strategies_table.load_strategies(self.saved_strategies)
            self._save_settings()

    def _show_detailed_trades(self, row, column):
        item = self.summary_table.item(row, 0)
        if not item: return
        strategy_data = item.data(QtCore.Qt.UserRole)
        if not strategy_data: return

        key_tuple = strategy_data["_key"]
        filtered_trades = [t for t in self.all_trades if t.get("_key") == key_tuple]
        if not filtered_trades: return

        th_val, slip_val, tp_val, sl_val, ts_val, fdv_val, boost_val = key_tuple

        params_str = (
            f"FDV: {fdv_val:,}$, Бусты: {boost_val}+, Порог: {-th_val if th_val is not None else '-'}%, TP: {tp_val}%, SL: {sl_val}%, "
            f"Проскальзывание: {slip_val}%, Сумма: ${ts_val}")
        self.detail_window = DetailedTradesWindow(filtered_trades, self.all_tokens_data, params_str, self)
        self.detail_window.exec_()

    def _test_saved_strategy(self):
        if (self.worker and self.worker.isRunning()) or (self.test_worker and self.test_worker.isRunning()): return

        selected_rows = self.saved_strategies_table.selectionModel().selectedRows()
        if not selected_rows:
            QtWidgets.QMessageBox.warning(self, "Ошибка", "Выберите стратегию для тестирования.")
            return

        item = self.saved_strategies_table.item(selected_rows[0].row(), 0)
        strategy_params = item.data(QtCore.Qt.UserRole)

        # Use general params from main window
        params = {"path": self.path_edit.text().strip(),
                  "deposit": self.dep_spin.value(),
                  "hold_days": self.hold_days_spin.value(),
                  "fee": self.fee_spin.value() / 100.0,
                  "entry_mode": ["instant_boost", "drawdown_local", "drawdown_boost"][self.entry_mode.currentIndex()],
                  "high_window": self.high_window_spin.value(),
                  "date_filter_enabled": self.cb_date_filter.isChecked(),
                  "start_date": datetime.combine(self.start_date_edit.date().toPyDate(), datetime.min.time()).replace(
                      tzinfo=timezone.utc),
                  "end_date": datetime.combine(self.end_date_edit.date().toPyDate(), datetime.max.time()).replace(
                      tzinfo=timezone.utc)
                  }

        self.btn_test_strategy.setEnabled(False)
        self.btn_show_test_results.setEnabled(False)
        self.strat_status_label.setText("Тестирование...")

        self.test_worker = SingleTestWorker(params, strategy_params)
        self.test_worker.finished.connect(self._test_finished)
        self.test_worker.failed.connect(self._test_failed)
        self.test_worker.start()

    def _test_finished(self, trades, tokens_data, status_msg):
        self.last_test_results = {"trades": trades, "tokens": tokens_data}
        self.strat_status_label.setText(status_msg)
        self.btn_test_strategy.setEnabled(True)
        self.btn_show_test_results.setEnabled(True)

    def _test_failed(self, msg):
        self.strat_status_label.setText("Ошибка теста.")
        QtWidgets.QMessageBox.critical(self, "Ошибка теста", msg)
        self.btn_test_strategy.setEnabled(True)
        self.btn_show_test_results.setEnabled(False)

    def _show_saved_strategy_results(self):
        if not self.last_test_results: return

        trades = self.last_test_results["trades"]
        tokens = self.last_test_results["tokens"]

        # Recreate params_str for the title
        selected_rows = self.saved_strategies_table.selectionModel().selectedRows()
        if not selected_rows: return
        item = self.saved_strategies_table.item(selected_rows[0].row(), 0)
        strategy_data = item.data(QtCore.Qt.UserRole)

        params_str = (
            f"FDV: {strategy_data.get('fdv', 0):,}$, Бусты: {strategy_data.get('boosts', 0)}+, Порог: {-strategy_data['threshold'] if strategy_data['threshold'] is not None else '-'}%, "
            f"TP: {strategy_data['tp']}%, SL: {strategy_data['sl']}%, Проскальзывание: {strategy_data['slip']}%, Сумма: ${strategy_data['trade_size']}")

        results_dialog = DetailedTradesWindow(trades, tokens, f"Результаты теста: {params_str}", self)
        results_dialog.exec_()

    def _finished(self, info: dict, trades: List, diag: tuple, tokens_data: dict):
        if self.worker and self.worker.is_stopped: return

        self.progress_label.setText("Обработка данных...")
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self.all_trades = trades
        self.all_tokens_data = tokens_data
        self.trades_table.load_rows_with_progress(trades)

        best, worst, summ = info.get("best", {}), info.get("worst", {}), info.get("summary", {})

        if not summ: return

        self.summary_table.load_summary_with_progress(summ, format_key(best), format_key(worst))

        self.progress_label.setText("Готово.")
        self.progress_bar.setVisible(False)

        total, with_ticks, _, entered_cnt = diag

        def format_status_string(label: str, res: dict) -> str:
            capital = res.get('capital', 0);
            th_val = res.get('threshold')
            th_str = "-" if th_val is None else f"-{th_val}%"
            tp = int(res.get('tp', 0) * 100);
            sl = int(res.get('sl', 0) * 100)
            slip = int(res.get('slip', 0) * 100);
            trade = int(res.get('trade_size', 0))
            fdv = int(res.get('fdv', 0))
            boosts = int(res.get('boosts', 0))
            return f"{label}: ${capital:.2f} (f>={fdv},b>={boosts},dd={th_str},tp={tp}%,sl={sl}%,slip={slip}%,trade=${trade})"

        best_str = format_status_string("Лучший", best)
        worst_str = format_status_string("Худший", worst)

        self.summary.setText(
            f"{best_str} | {worst_str}. Комбинаций: {len(summ)} | Сделок: {len(trades)} | Токенов (всего): {total} / вошли: {entered_cnt}")


def format_key(res: dict) -> str:
    th = "-" if res.get('threshold') is None else f"{res.get('threshold')}%"
    tp = int(res.get('tp', 0) * 100);
    sl = int(res.get('sl', 0) * 100)
    slip = int(res.get('slip', 0) * 100);
    trade = int(res.get('trade_size', 0))
    fdv = int(res.get('fdv', 0))
    boosts = int(res.get('boosts', 0))
    return f"fdv>={fdv}|b>={boosts}|{th}|tp={tp}%|sl={sl}%|slip={slip}%|trade=${trade}"


def main():
    app = QtWidgets.QApplication(sys.argv);
    w = MainWindow();
    w.show();
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
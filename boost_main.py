# -*- coding: utf-8 -*-
"""
Главное приложение PumpFun — мониторинг/анализ/архив/симулятор стратегий.
Пишет META в data/price_history.jsonl, чтобы отдельное приложение-история
могло «подхватывать» новые бусты в реальном времени.
"""

import os, sys, webbrowser, random
from datetime import datetime, timedelta

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTableView, QWidget, QVBoxLayout, QHeaderView,
    QLabel, QTabWidget, QMenu, QSplitter, QGroupBox, QPushButton, QHBoxLayout,
    QMessageBox, QFormLayout, QDoubleSpinBox, QSpinBox, QScrollArea
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QCloseEvent

from common_dsx import (
    # файлы
    MEMORY_FILE, PRICE_TRACKER_FILE, ARCHIVE_FILE, STRATEGY_STATE_FILE, SETTINGS_FILE,
    TRADING_LOG_S1, TRADING_LOG_S2, TRADING_LOG_S3, PRICE_HISTORY_FILE,
    # утилиты
    load_json, save_json, append_jsonl, log_event,
    fmt_date, fmt_time, pct_cell, CustomTableModel, Worker
)

# --- Стратегии ---
STRATEGIES = [
    {"code": "S1", "name": "Стратегия S1 (TP 20% | $100)", "log": TRADING_LOG_S1},
    {"code": "S2", "name": "Стратегия S2 (TP 5%  | $50)",  "log": TRADING_LOG_S2},
    {"code": "S3", "name": "Стратегия S3 (TP 10% | $50)",  "log": TRADING_LOG_S3},
]

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PumpFunBot — Мониторинг / Симулятор стратегий (модульная версия)")
        self.setGeometry(80, 60, 1880, 1040)

        # --- загрузка данных ---
        self.is_first_run = not os.path.exists(MEMORY_FILE)
        self.token_discovery_times = load_json(MEMORY_FILE, default={})
        self.tracker_jobs = load_json(PRICE_TRACKER_FILE, default=[])
        self.archive_jobs = load_json(ARCHIVE_FILE, default=[])
        self.persisted_state = load_json(STRATEGY_STATE_FILE, default={})
        self.settings = self._merge_settings(
            self._default_settings(), load_json(SETTINGS_FILE, default={})
        )
        self._apply_settings_to_runtime()

        # --- стратегии: состояние ---
        self.strategy_states = {}
        for s in STRATEGIES:
            code, log_file = s["code"], s["log"]
            trades = load_json(log_file, default=[])
            paused_default = bool(self.persisted_state.get(code, {}).get("paused", False))
            init_dep = float(self.settings["strategies"][code]["initial_deposit"])
            current_dep = self._calc_current_deposit(trades, init_dep)
            self.strategy_states[code] = {
                "cfg": {"code": code, "log": log_file},
                "trades": trades,
                "current_deposit": current_dep,
                "paused": paused_default
            }

        self.last_all_details = {}
        self.last_boosted = {}
        self.fetch_in_progress = False
        self._pending_fetch = None

        # --- UI ---
        self._setup_ui()

        # --- таймеры (раздельные) ---
        self.timer_monitor = QTimer(self)
        self.timer_monitor.setInterval(self.refresh_ms_monitor)
        self.timer_monitor.timeout.connect(lambda: self.start_data_fetch(origin="monitor"))
        self.timer_monitor.start()

        self.timer_strategies = QTimer(self)
        self.timer_strategies.setInterval(self.refresh_ms_strategies)
        self.timer_strategies.timeout.connect(lambda: self.start_data_fetch(origin="strategies"))
        self.timer_strategies.start()

        # первичный заход
        self.start_data_fetch(origin="monitor")
        self.start_data_fetch(origin="strategies")

    # ---------------- Настройки ----------------
    def _default_settings(self):
        return {
            "global": {
                "refresh_ms_monitor": 30000,
                "refresh_ms_strategies": 30000,
                "archive_after_hours": 6,
                "min_fdv": 50000
            },
            "fees": {"commission_open_pct": 0.003, "commission_close_pct": 0.003},
            "slippage": {"min": 0.05, "max": 0.09},
            "strategies": {
                "S1": {"initial_deposit": 1000.0, "entry_usd": 100.0, "tp": 0.20, "sl": -0.50},
                "S2": {"initial_deposit": 1000.0, "entry_usd": 50.0,  "tp": 0.05, "sl": -0.50},
                "S3": {"initial_deposit": 1000.0, "entry_usd": 50.0,  "tp": 0.10, "sl": -0.50}
            }
        }

    @staticmethod
    def _merge_settings(defaults, saved):
        """Дополняет старые настройки, сохраняя заданные пользователем значения."""
        result = dict(saved) if isinstance(saved, dict) else {}
        for key, value in defaults.items():
            if isinstance(value, dict):
                result[key] = MainWindow._merge_settings(value, result.get(key))
            elif key not in result:
                result[key] = value
        return result

    def _apply_settings_to_runtime(self):
        g = self.settings["global"]; f = self.settings["fees"]; s = self.settings["slippage"]
        self.refresh_ms_monitor = int(g.get("refresh_ms_monitor", 30000))
        self.refresh_ms_strategies = int(g.get("refresh_ms_strategies", 30000))
        self.archive_after_hours = int(g["archive_after_hours"])
        self.min_fdv = float(g["min_fdv"])
        self.commission_open_pct = float(f["commission_open_pct"])
        self.commission_close_pct = float(f["commission_close_pct"])
        self.slippage_min = float(s["min"])
        self.slippage_max = float(s["max"])

    # ---------------- Деньги ----------------
    def _calc_current_deposit(self, trades, initial_deposit):
        dep = initial_deposit
        for t in trades:
            if t.get("status") == "OPEN":
                dep -= t.get("entry_size_usd", 0) or 0
            elif t.get("status") == "CLOSED":
                dep -= t.get("entry_size_usd", 0) or 0
                dep += t.get("exit_size_usd", 0) or 0
        return dep

    def _estimate_balance_now(self, trades, all_details, code):
        balance = self.strategy_states[code]["current_deposit"]
        for t in trades:
            if t.get("status") != "OPEN": continue
            d = all_details.get(t["address"])
            if not d or not d.get("priceUsd"): continue
            curr = float(d["priceUsd"])
            s_close = t.get("slippage_close", self.slippage_min)
            qty = t.get("qty")
            if qty is None:
                s_open = t.get("slippage_open", self.slippage_min)
                eff_entry = t["entry_price"] * (1 + s_open + self.commission_open_pct)
                qty = (t["entry_size_usd"] / eff_entry) if eff_entry > 0 else 0
            eff_exit = curr * (1 - s_close - self.commission_close_pct)
            balance += qty * eff_exit
        return balance

    # ---------------- UI ----------------
    def _setup_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        self.tabs = QTabWidget(); main_layout.addWidget(self.tabs)

        # Мониторинг
        self.monitor_widget = QWidget()
        ml = QVBoxLayout(self.monitor_widget)
        self.monitor_table = QTableView()
        self.monitor_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.monitor_table.customContextMenuRequested.connect(lambda pos: self._show_context_menu(self.monitor_table, pos))
        self.monitor_table.doubleClicked.connect(lambda idx: self._copy_link_on_double_click(self.monitor_table, idx))
        ml.addWidget(self.monitor_table)
        self.tabs.addTab(self.monitor_widget, "Мониторинг")

        # Анализ цены
        self.tracker_widget = QWidget()
        tl = QVBoxLayout(self.tracker_widget)
        self.tracker_table = QTableView()
        self.tracker_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tracker_table.customContextMenuRequested.connect(lambda pos: self._show_context_menu(self.tracker_table, pos))
        self.tracker_table.doubleClicked.connect(lambda idx: self._copy_link_on_double_click(self.tracker_table, idx))
        tl.addWidget(self.tracker_table)
        self.tabs.addTab(self.tracker_widget, "Анализ Цены")

        # Архив (свод)
        self.archive_widget = QWidget()
        al = QVBoxLayout(self.archive_widget)
        self.archive_table = QTableView()
        self.archive_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.archive_table.customContextMenuRequested.connect(lambda pos: self._show_context_menu(self.archive_table, pos))
        self.archive_table.doubleClicked.connect(lambda idx: self._copy_link_on_double_click(self.archive_table, idx))
        al.addWidget(self.archive_table)
        self.tabs.addTab(self.archive_widget, "Архив")

        # Симулятор стратегий
        self._build_simulator_tabs()

        # Настройки + скролл
        self._build_settings_tab()

        # Нижний статус
        self.status_label = QLabel("Запуск...")
        main_layout.addWidget(self.status_label)

        # Первичный статус по стратегиям
        for s in STRATEGIES:
            self._update_strategy_status_ui(s["code"])

    def _build_simulator_tabs(self):
        self.sim_root = QWidget()
        sim_root_layout = QVBoxLayout(self.sim_root)
        self.strategy_tabs = QTabWidget()
        sim_root_layout.addWidget(self.strategy_tabs)
        self.strategy_ui = {}

        for s in STRATEGIES:
            code, name = s["code"], s["name"]
            w = QWidget(); lay = QVBoxLayout(w)

            # Кнопки управления
            btns = QHBoxLayout()
            btn_close_all = QPushButton("Закрыть все позиции")
            btn_pause = QPushButton("Пауза")
            btn_resume = QPushButton("Продолжить тест")
            btn_restart = QPushButton("Начать тест с начала")
            btns.addWidget(btn_close_all); btns.addWidget(btn_pause); btns.addWidget(btn_resume); btns.addWidget(btn_restart)
            lay.addLayout(btns)
            btn_close_all.clicked.connect(lambda _=None, c=code: self._on_close_all_clicked(c))
            btn_pause.clicked.connect(lambda _=None, c=code: self._on_pause_clicked(c))
            btn_resume.clicked.connect(lambda _=None, c=code: self._on_resume_clicked(c))
            btn_restart.clicked.connect(lambda _=None, c=code: self._on_restart_clicked(c))

            status_lbl = QLabel(); status_lbl.setStyleSheet("font-size:13px;font-weight:bold;")
            lay.addWidget(status_lbl)

            info_lbl = QLabel(); info_lbl.setStyleSheet("font-size:14px;font-weight:bold;")
            lay.addWidget(info_lbl)

            splitter = QSplitter(Qt.Vertical)
            grp_open = QGroupBox("Открытые позиции")
            grp_open_l = QVBoxLayout(); tv_open = QTableView(); grp_open_l.addWidget(tv_open); grp_open.setLayout(grp_open_l)
            grp_closed = QGroupBox("История сделок")
            grp_closed_l = QVBoxLayout(); tv_closed = QTableView(); grp_closed_l.addWidget(tv_closed); grp_closed.setLayout(grp_closed_l)
            splitter.addWidget(grp_open); splitter.addWidget(grp_closed)
            lay.addWidget(splitter)

            self.strategy_tabs.addTab(w, name)
            self.strategy_ui[code] = {"status_lbl": status_lbl, "info_lbl": info_lbl, "open_tv": tv_open, "closed_tv": tv_closed}

        self.tabs.addTab(self.sim_root, "Симулятор стратегий")

    def _build_settings_tab(self):
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        c = QWidget()
        layout = QVBoxLayout(c)

        # Глобальные
        gb_global = QGroupBox("Глобальные")
        form_g = QFormLayout(gb_global)
        self.sb_refresh_monitor = QSpinBox(); self.sb_refresh_monitor.setRange(1000, 600000); self.sb_refresh_monitor.setSingleStep(1000)
        self.sb_refresh_monitor.setValue(int(self.settings["global"]["refresh_ms_monitor"])); form_g.addRow("Интервал обновления мониторинга, мс", self.sb_refresh_monitor)
        self.sb_refresh_strat = QSpinBox(); self.sb_refresh_strat.setRange(1000, 600000); self.sb_refresh_strat.setSingleStep(1000)
        self.sb_refresh_strat.setValue(int(self.settings["global"]["refresh_ms_strategies"])); form_g.addRow("Интервал обновления стратегий, мс", self.sb_refresh_strat)
        self.sb_archive_h = QSpinBox(); self.sb_archive_h.setRange(1, 72)
        self.sb_archive_h.setValue(int(self.settings["global"]["archive_after_hours"])); form_g.addRow("Архивация спустя, часов", self.sb_archive_h)
        self.sb_min_fdv = QSpinBox(); self.sb_min_fdv.setRange(0, 10_000_000)
        self.sb_min_fdv.setValue(int(self.settings["global"]["min_fdv"])); form_g.addRow("Минимальный FDV для отслеживания", self.sb_min_fdv)
        layout.addWidget(gb_global)

        # Комиссии
        gb_fees = QGroupBox("Комиссии")
        form_f = QFormLayout(gb_fees)
        self.dsb_comm_open = QDoubleSpinBox(); self.dsb_comm_open.setDecimals(5); self.dsb_comm_open.setRange(0.0, 0.1); self.dsb_comm_open.setSingleStep(0.0005)
        self.dsb_comm_open.setValue(float(self.settings["fees"]["commission_open_pct"])); form_f.addRow("Комиссия на вход (доля)", self.dsb_comm_open)
        self.dsb_comm_close = QDoubleSpinBox(); self.dsb_comm_close.setDecimals(5); self.dsb_comm_close.setRange(0.0, 0.1); self.dsb_comm_close.setSingleStep(0.0005)
        self.dsb_comm_close.setValue(float(self.settings["fees"]["commission_close_pct"])); form_f.addRow("Комиссия на выход (доля)", self.dsb_comm_close)
        layout.addWidget(gb_fees)

        # Проскальзывание
        gb_slip = QGroupBox("Проскальзывание")
        form_s = QFormLayout(gb_slip)
        self.dsb_slip_min = QDoubleSpinBox(); self.dsb_slip_min.setDecimals(4); self.dsb_slip_min.setRange(0.0, 1.0); self.dsb_slip_min.setSingleStep(0.005)
        self.dsb_slip_min.setValue(float(self.settings["slippage"]["min"])); form_s.addRow("Мин. проскальзывание (доля)", self.dsb_slip_min)
        self.dsb_slip_max = QDoubleSpinBox(); self.dsb_slip_max.setDecimals(4); self.dsb_slip_max.setRange(0.0, 1.0); self.dsb_slip_max.setSingleStep(0.005)
        self.dsb_slip_max.setValue(float(self.settings["slippage"]["max"])); form_s.addRow("Макс. проскальзывание (доля)", self.dsb_slip_max)
        layout.addWidget(gb_slip)

        # Параметры стратегий
        self.strategy_forms = {}
        for s in STRATEGIES:
            code, name = s["code"], s["name"]
            gb = QGroupBox(f"{name} — параметры")
            form = QFormLayout(gb)
            st_cfg = self.settings["strategies"][code]
            dsb_init_dep = QDoubleSpinBox(); dsb_init_dep.setRange(0, 1_000_000); dsb_init_dep.setDecimals(2); dsb_init_dep.setValue(float(st_cfg["initial_deposit"]))
            form.addRow("Начальный депозит ($) *", dsb_init_dep)
            dsb_entry = QDoubleSpinBox(); dsb_entry.setRange(0, 100_000); dsb_entry.setDecimals(2); dsb_entry.setValue(float(st_cfg["entry_usd"]))
            form.addRow("Размер входа ($)", dsb_entry)
            dsb_tp = QDoubleSpinBox(); dsb_tp.setRange(-1.0, 5.0); dsb_tp.setDecimals(4); dsb_tp.setSingleStep(0.005); dsb_tp.setValue(float(st_cfg["tp"]))
            form.addRow("TP (доля)", dsb_tp)
            dsb_sl = QDoubleSpinBox(); dsb_sl.setRange(-5.0, 0.0); dsb_sl.setDecimals(4); dsb_sl.setSingleStep(0.01); dsb_sl.setValue(float(st_cfg["sl"]))
            form.addRow("SL (доля)", dsb_sl)
            self.strategy_forms[code] = {"init_dep": dsb_init_dep, "entry": dsb_entry, "tp": dsb_tp, "sl": dsb_sl}
            layout.addWidget(gb)

        note = QLabel("(*) Применяется при «Начать тест с начала». Остальные параметры — сразу.")
        note.setStyleSheet("color:#555;font-size:12px;")
        layout.addWidget(note)

        btns = QHBoxLayout()
        btn_save = QPushButton("Сохранить")
        btn_cancel = QPushButton("Отмена")
        btns.addWidget(btn_save); btns.addWidget(btn_cancel)
        layout.addLayout(btns)
        btn_save.clicked.connect(self._on_save_settings_clicked)
        btn_cancel.clicked.connect(self._on_cancel_settings_clicked)

        layout.addStretch(1)
        self.settings_scroll.setWidget(c)
        self.tabs.addTab(self.settings_scroll, "Настройки")

    def _show_context_menu(self, table, position):
        index = table.indexAt(position)
        if not index.isValid(): return
        try:
            ca = table.model().index(index.row(), 1).data()
        except Exception:
            return
        if not ca: return
        menu = QMenu()
        open_action = menu.addAction("Открыть на Dexscreener")
        copy_link_action = menu.addAction("Копировать ссылку")
        copy_ca_action = menu.addAction("Копировать адрес (CA)")
        action = menu.exec_(table.viewport().mapToGlobal(position))
        url = f"https://dexscreener.com/solana/{ca}"
        if action == open_action:
            webbrowser.open(url)
        elif action == copy_link_action:
            QApplication.clipboard().setText(url)
            self.status_label.setText(f"Ссылка на {ca[:10]}... скопирована.")
        elif action == copy_ca_action:
            QApplication.clipboard().setText(ca)
            self.status_label.setText(f"Адрес {ca[:10]}... скопирован.")

    def _copy_link_on_double_click(self, table, index):
        if index.column() == 1:
            ca = index.data()
            if ca:
                url = f"https://dexscreener.com/solana/{ca}"
                QApplication.clipboard().setText(url)
                self.status_label.setText(f"Ссылка на {ca[:10]}... скопирована.")

    # ---------------- Сохранение/отмена настроек ----------------
    def _on_save_settings_clicked(self):
        slip_min = float(self.dsb_slip_min.value())
        slip_max = float(self.dsb_slip_max.value())
        if slip_max < slip_min:
            QMessageBox.warning(self, "Настройки", "Макс. проскальзывание не может быть меньше мин.")
            return
        new_settings = {
            "global": {
                "refresh_ms_monitor": int(self.sb_refresh_monitor.value()),
                "refresh_ms_strategies": int(self.sb_refresh_strat.value()),
                "archive_after_hours": int(self.sb_archive_h.value()),
                "min_fdv": int(self.sb_min_fdv.value())
            },
            "fees": {"commission_open_pct": float(self.dsb_comm_open.value()), "commission_close_pct": float(self.dsb_comm_close.value())},
            "slippage": {"min": slip_min, "max": slip_max},
            "strategies": {}
        }
        for s in STRATEGIES:
            code = s["code"]
            new_settings["strategies"][code] = {
                "initial_deposit": float(self.strategy_forms[code]["init_dep"].value()),
                "entry_usd": float(self.strategy_forms[code]["entry"].value()),
                "tp": float(self.strategy_forms[code]["tp"].value()),
                "sl": float(self.strategy_forms[code]["sl"].value()),
            }
        self.settings = new_settings
        save_json(self.settings, SETTINGS_FILE)
        self._apply_settings_to_runtime()
        self.timer_monitor.setInterval(self.refresh_ms_monitor)
        self.timer_strategies.setInterval(self.refresh_ms_strategies)
        self._update_ui(self.last_boosted or {}, self.last_all_details or {})
        log_event("SETTINGS_SAVED",
                  refresh_ms_monitor=self.refresh_ms_monitor,
                  refresh_ms_strategies=self.refresh_ms_strategies,
                  archive_after_h=self.archive_after_hours,
                  min_fdv=self.min_fdv,
                  comm_open=self.commission_open_pct,
                  comm_close=self.commission_close_pct,
                  slip_min=self.slippage_min,
                  slip_max=self.slippage_max)
        self.status_label.setText("Настройки сохранены и применены.")

    def _on_cancel_settings_clicked(self):
        self._update_ui(self.last_boosted or {}, self.last_all_details or {})
        self.status_label.setText("Изменения откатены.")

    # ---------------- Обновление данных ----------------
    def start_data_fetch(self, origin="monitor"):
        if self.fetch_in_progress:
            self._pending_fetch = origin
            return
        self.fetch_in_progress = True
        self.status_label.setText(f"Обновление ({origin})...")

        tracked = [job["address"] for job in self.tracker_jobs]
        for st in self.strategy_states.values():
            tracked.extend([t["address"] for t in st["trades"] if t.get("status") == "OPEN"])

        self.worker = Worker(tracked, fetch_boosts=True)
        self.worker.completed.connect(self._process_completed_fetch)
        self.worker.start()

    def _process_completed_fetch(self, result: dict):
        try:
            boosted = result.get("boosts", {})
            all_details = result.get("details", {})
            self.last_all_details = all_details
            self.last_boosted = boosted
            now = datetime.now()

            memory_changed = tracker_changed = archive_changed = False
            strategies_changed = {s["code"]: False for s in STRATEGIES}

            # Новые адреса (только Solana + FDV фильтр)
            for address, boost_info in boosted.items():
                details = all_details.get(address)
                if not details: continue
                if details.get("chainId") != "solana": continue

                if address not in self.token_discovery_times:
                    fdv_val = float(details.get("fdv", 0) or 0)
                    if fdv_val < self.min_fdv:
                        continue

                    disc = now
                    self.token_discovery_times[address] = "Обнаружен ранее" if self.is_first_run else disc.strftime("%Y-%m-%d %H:%M:%S")
                    memory_changed = True

                    price = float(details.get("priceUsd", 0) or 0)
                    job = {
                        "address": address,
                        "name": details.get("baseToken", {}).get("name", "N/A"),
                        "discoveryTime": disc.isoformat(),
                        "initialPrice": price,
                        "initialMarketCap": details.get("fdv", 0),
                        "initialBoosts": boost_info.get("totalAmount", 0) or boost_info.get("amount", 0),
                        "maxPrice": price, "minPrice": price,
                        "maxPriceTime": disc.isoformat(), "minPriceTime": disc.isoformat(),
                        "timeTo5Profit": None, "timeTo10Profit": None, "timeTo20Profit": None
                    }
                    self.tracker_jobs.append(job); tracker_changed = True

                    log_event("NEW_TOKEN",
                              address=address, name=job["name"], fdv=int(fdv_val),
                              chain=details.get("chainId"), discovery=job["discoveryTime"], init_price=price)

                    # META в историю (для отдельного приложения)
                    meta = {
                        "type": "meta",
                        "address": address,
                        "name": job["name"],
                        "fdv": details.get("fdv", 0),
                        "boosts": job["initialBoosts"],
                        "discoveryTime": job["discoveryTime"]
                    }
                    append_jsonl(meta, PRICE_HISTORY_FILE)

                    # Автовход по активным стратегиям
                    for s in STRATEGIES:
                        code = s["code"]
                        st = self.strategy_states[code]
                        st_cfg = self.settings["strategies"][code]
                        if st["paused"]: continue
                        if any(t["address"] == address and t.get("status") == "OPEN" for t in st["trades"]): continue
                        if st["current_deposit"] < st_cfg["entry_usd"]: continue
                        s_open = random.uniform(self.slippage_min, self.slippage_max)
                        s_close = random.uniform(self.slippage_min, self.slippage_max)
                        eff_entry = price * (1 + s_open + self.commission_open_pct)
                        qty = (st_cfg["entry_usd"] / eff_entry) if eff_entry > 0 else 0.0
                        trade = {
                            "address": address, "name": job["name"],
                            "entry_time": now.isoformat(), "entry_price": price,
                            "entry_size_usd": st_cfg["entry_usd"], "qty": qty,
                            "slippage_open": s_open, "slippage_close": s_close,
                            "peak_price": price, "max_drawdown_pct": 0.0,
                            "status": "OPEN", "exit_time": None, "exit_price": None,
                            "exit_size_usd": None, "pnl_pct": None
                        }
                        st["trades"].append(trade); st["current_deposit"] -= st_cfg["entry_usd"]
                        strategies_changed[code] = True

            if self.is_first_run and self.token_discovery_times:
                self.is_first_run = False

            # TP/SL обработка
            for s in STRATEGIES:
                code = s["code"]; st = self.strategy_states[code]; st_cfg = self.settings["strategies"][code]
                if st["paused"]: continue
                for t in st["trades"]:
                    if t.get("status") != "OPEN": continue
                    d = all_details.get(t["address"])
                    if not d or d.get("chainId") != "solana" or not d.get("priceUsd"): continue
                    curr = float(d["priceUsd"])
                    t["peak_price"] = max(t.get("peak_price", curr), curr)
                    peak = t["peak_price"]
                    if peak > 0:
                        dd_now = (curr / peak - 1.0) * 100.0
                        t["max_drawdown_pct"] = min(t.get("max_drawdown_pct", 0.0), dd_now)
                    s_open = t.get("slippage_open", self.slippage_min)
                    s_close = t.get("slippage_close", self.slippage_min)
                    eff_entry = t["entry_price"] * (1 + s_open + self.commission_open_pct)
                    eff_exit_now = curr * (1 - s_close - self.commission_close_pct)
                    pnl = (eff_exit_now - eff_entry) / eff_entry if eff_entry > 0 else 0.0
                    close_reason = "TP" if pnl >= st_cfg["tp"] else ("SL" if pnl <= st_cfg["sl"] else None)
                    if close_reason:
                        qty = t.get("qty", 0.0)
                        net = qty * eff_exit_now
                        t["status"] = "CLOSED"
                        t["exit_time"] = datetime.now().isoformat()
                        t["exit_price"] = curr
                        t["exit_size_usd"] = net
                        t["pnl_pct"] = pnl * 100.0
                        st["current_deposit"] += net
                        strategies_changed[code] = True

            # Трекер и перенос в архив
            for job in list(self.tracker_jobs):
                try:
                    t_disc = datetime.fromisoformat(job["discoveryTime"])
                except Exception:
                    continue
                if datetime.now() - t_disc > timedelta(hours=self.archive_after_hours):
                    self.tracker_jobs.remove(job)
                    self.archive_jobs.append(job)
                    tracker_changed = True; archive_changed = True
                    continue
                d = all_details.get(job["address"])
                if not d or d.get("chainId")!="solana" or not d.get("priceUsd"): continue
                price = float(d["priceUsd"])
                if price > job.get("maxPrice", job["initialPrice"]):
                    job["maxPrice"] = price; job["maxPriceTime"] = datetime.now().isoformat(); tracker_changed = True
                if price < job.get("minPrice", job["initialPrice"]):
                    job["minPrice"] = price; job["minPriceTime"] = datetime.now().isoformat(); tracker_changed = True
                init = job.get("initialPrice", 0.0)
                if init > 0:
                    if job.get("timeTo5Profit") is None and price >= init * 1.05:
                        job["timeTo5Profit"] = datetime.now().isoformat(); tracker_changed = True
                    if job.get("timeTo10Profit") is None and price >= init * 1.10:
                        job["timeTo10Profit"] = datetime.now().isoformat(); tracker_changed = True
                    if job.get("timeTo20Profit") is None and price >= init * 1.20:
                        job["timeTo20Profit"] = datetime.now().isoformat(); tracker_changed = True

            # сохранения
            if memory_changed: save_json(self.token_discovery_times, MEMORY_FILE)
            if tracker_changed: save_json(self.tracker_jobs, PRICE_TRACKER_FILE)
            if archive_changed: save_json(self.archive_jobs, ARCHIVE_FILE)
            for code, changed in strategies_changed.items():
                if changed:
                    save_json(self.strategy_states[code]["trades"], self._strategy_log_by_code(code))

            # UI
            self._update_ui(boosted, all_details)
            log_event("CYCLE_DONE",
                      tracked=len(self.tracker_jobs),
                      open_S1=sum(1 for t in self.strategy_states["S1"]["trades"] if t.get("status")=="OPEN"),
                      open_S2=sum(1 for t in self.strategy_states["S2"]["trades"] if t.get("status")=="OPEN"),
                      open_S3=sum(1 for t in self.strategy_states["S3"]["trades"] if t.get("status")=="OPEN"))
        finally:
            self.fetch_in_progress = False
            if self._pending_fetch:
                origin = self._pending_fetch
                self._pending_fetch = None
                QTimer.singleShot(0, lambda: self.start_data_fetch(origin=origin))

    # ---------------- Вспомогательное ----------------
    def _strategy_log_by_code(self, code):  # уже абсолютные пути в common_dsx
        for s in STRATEGIES:
            if s["code"] == code: return s["log"]
        return TRADING_LOG_S1

    def _confirm(self, title, text):
        res = QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return res == QMessageBox.Yes

    # Кнопки стратегий
    def _on_pause_clicked(self, code):
        st = self.strategy_states[code]
        if st["paused"]:
            self.status_label.setText(f"{code}: уже на паузе.")
            return
        if self._confirm("Пауза", f"Вы точно уверены, что хотите поставить стратегию {code} на паузу?"):
            st["paused"] = True
            self._persist_paused()
            self._update_strategy_status_ui(code)
            self.status_label.setText(f"{code}: поставлена на паузу.")
            log_event("PAUSE", strategy=code)

    def _on_resume_clicked(self, code):
        st = self.strategy_states[code]
        if not st["paused"]:
            self.status_label.setText(f"{code}: уже запущена.")
            return
        if self._confirm("Продолжить тест", f"Вы точно уверены, что хотите продолжить стратегию {code}?"):
            st["paused"] = False
            self._persist_paused()
            self._update_strategy_status_ui(code)
            self.status_label.setText(f"{code}: возобновлена.")
            log_event("RESUME", strategy=code)

    def _on_restart_clicked(self, code):
        st = self.strategy_states[code]
        st_cfg = self.settings["strategies"][code]
        if not self._confirm("Начать заново", f"Вы точно уверены, что хотите начать стратегию {code} с начала?\nВсе сделки будут удалены, депозит сброшен."):
            return
        old_cnt = len(st["trades"])
        st["trades"].clear()
        st["current_deposit"] = float(st_cfg["initial_deposit"])
        save_json(st["trades"], self._strategy_log_by_code(code))
        self._update_ui(self.last_boosted or {}, self.last_all_details or {})
        self.status_label.setText(f"{code}: тест начат заново.")
        log_event("RESTART", strategy=code, cleared_trades=old_cnt, new_deposit=st["current_deposit"])

    def _on_close_all_clicked(self, code):
        st = self.strategy_states[code]
        if not self._confirm("Закрыть все", f"Вы точно уверены, что хотите закрыть все ОТКРЫТЫЕ позиции {code} по текущей цене?"):
            return
        details = self.last_all_details or {}
        now = datetime.now().isoformat()
        changed = False; closed_cnt = 0
        for t in st["trades"]:
            if t.get("status") != "OPEN": continue
            d = details.get(t["address"])
            if not d or not d.get("priceUsd"): continue
            curr = float(d["priceUsd"])
            s_close = t.get("slippage_close", self.slippage_min)
            s_open = t.get("slippage_open", self.slippage_min)
            eff_entry = t["entry_price"] * (1 + s_open + self.commission_open_pct)
            eff_exit_now = curr * (1 - s_close - self.commission_close_pct)
            qty = t.get("qty", 0.0)
            net = qty * eff_exit_now
            pnl = (eff_exit_now - eff_entry) / eff_entry if eff_entry > 0 else 0.0
            t["status"] = "CLOSED"; t["exit_time"] = now; t["exit_price"] = curr
            t["exit_size_usd"] = net; t["pnl_pct"] = pnl * 100.0
            st["current_deposit"] += net
            changed = True; closed_cnt += 1
        if changed:
            save_json(st["trades"], self._strategy_log_by_code(code))
        self._update_ui(self.last_boosted or {}, details)
        self.status_label.setText(f"{code}: закрыто {closed_cnt} позиций.")

    def _persist_paused(self):
        data = {code: {"paused": st["paused"]} for code, st in self.strategy_states.items()}
        save_json(data, STRATEGY_STATE_FILE)

    def _update_strategy_status_ui(self, code):
        st = self.strategy_states[code]
        lbl = self.strategy_ui[code]["status_lbl"]
        if st["paused"]:
            lbl.setText("тест на паузе")
            lbl.setStyleSheet("font-size:13px;font-weight:bold;color:#d11;")
        else:
            lbl.setText("идёт тестирование стратегии")
            lbl.setStyleSheet("font-size:13px;font-weight:bold;color:#0a0;")

    # ---------------- Обновление UI ----------------
    def _update_ui(self, boosted_tokens_info: dict, all_details: dict):
        # Мониторинг
        monitor_rows = []
        for address, token_info in boosted_tokens_info.items():
            details = all_details.get(address)
            if not details: continue
            if details.get("chainId") != "solana": continue
            fdv = float(details.get("fdv", 0) or 0)
            if fdv < self.min_fdv: continue
            name = details.get("baseToken", {}).get("name", "N/A")
            price = float(details.get("priceUsd", 0) or 0)
            discovered = self.token_discovery_times.get(address, "N/A")
            date_str, time_str = "N/A", ""
            if isinstance(discovered, str):
                try:
                    dt = datetime.fromisoformat(discovered)
                    date_str = dt.strftime('%Y-%m-%d')
                    time_str = dt.strftime('%H:%M:%S')
                except Exception:
                    date_str = str(discovered)
            boosts = token_info.get("totalAmount") or token_info.get("amount", 0)
            monitor_rows.append([name, address, f"${fdv:,.0f}", str(boosts), date_str, time_str,
                                 f"{price:.10f}".rstrip('0').rstrip('.')])
        monitor_rows.sort(key=lambda r: (
            int(r[3]) if r[3].isdigit() else 0,
            int(r[2].replace("$", "").replace(",", "")) if isinstance(r[2], str) else 0
        ), reverse=True)
        self.monitor_table.setModel(CustomTableModel(
            monitor_rows,
            ["Название", "Адрес (CA)", "Капитализация ($)", "Бусты", "Дата", "Время", "Цена ($)"],
            [2,3,4,5,6]
        ))
        self.monitor_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.monitor_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.monitor_table.setColumnWidth(1, 300)

        # Анализ Цены
        tracker_rows = []
        for job in sorted(self.tracker_jobs, key=lambda j: datetime.fromisoformat(j["discoveryTime"]), reverse=True):
            init = job.get("initialPrice", 0.0)
            mx = job.get("maxPrice", init); mn = job.get("minPrice", init)
            mx_val = ((mx - init)/init)*100 if init > 0 else 0.0
            mn_val = ((mn - init)/init)*100 if init > 0 else 0.0
            tracker_rows.append([
                job.get("name","N/A"), job.get("address"),
                f"${job.get('initialMarketCap',0):,.0f}",
                str(job.get("initialBoosts",0)),
                fmt_date(job["discoveryTime"]), fmt_time(job["discoveryTime"]),
                f"{init:.8f}",
                pct_cell(mx_val), fmt_time(job["maxPriceTime"]),
                pct_cell(mn_val), fmt_time(job["minPriceTime"]),
                self._fmt_delta(job["discoveryTime"], job.get("timeTo5Profit")),
                self._fmt_delta(job["discoveryTime"], job.get("timeTo10Profit")),
                self._fmt_delta(job["discoveryTime"], job.get("timeTo20Profit"))
            ])
        self.tracker_table.setModel(CustomTableModel(
            tracker_rows,
            ["Название","Адрес (CA)","Капитализация","Бусты","Дата обнаружения","Время обнаружения",
             "Нач. цена ($)","Макс. рост","Время макс.","Макс. падение","Время мин.",
             "Время до +5%","Время до +10%","Время до +20%"],
            [2,3,4,5,6,7,8,9,10,11,12,13]
        ))
        self.tracker_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tracker_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.tracker_table.setColumnWidth(1, 300)

        # Архив (свод)
        arch_rows = []
        for job in sorted(self.archive_jobs, key=lambda j: datetime.fromisoformat(j["discoveryTime"]), reverse=True):
            init = job.get("initialPrice", 0.0)
            mx = job.get("maxPrice", init); mn = job.get("minPrice", init)
            mx_val = ((mx - init)/init)*100 if init > 0 else 0.0
            mn_val = ((mn - init)/init)*100 if init > 0 else 0.0
            arch_rows.append([
                job.get("name","N/A"), job.get("address"),
                f"${job.get('initialMarketCap',0):,.0f}",
                str(job.get("initialBoosts",0)),
                fmt_date(job["discoveryTime"]), fmt_time(job["discoveryTime"]),
                f"{init:.8f}",
                pct_cell(mx_val), fmt_time(job["maxPriceTime"]),
                pct_cell(mn_val), fmt_time(job["minPriceTime"]),
                self._fmt_delta(job.get("discoveryTime"), job.get("timeTo5Profit")),
                self._fmt_delta(job.get("discoveryTime"), job.get("timeTo10Profit")),
                self._fmt_delta(job.get("discoveryTime"), job.get("timeTo20Profit"))
            ])
        self.archive_table.setModel(CustomTableModel(
            arch_rows,
            ["Название","Адрес (CA)","Капитализация","Бусты","Дата обнаружения","Время обнаружения",
             "Нач. цена ($)","Макс. рост","Время макс.","Макс. падение","Время мин.",
             "Время до +5%","Время до +10%","Время до +20%"],
            [2,3,4,5,6,7,8,9,10,11,12,13]
        ))
        self.archive_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.archive_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.archive_table.setColumnWidth(1, 300)

        # Симулятор стратегий
        for s in STRATEGIES:
            code = s["code"]; st = self.strategy_states[code]; trades = st["trades"]
            self._update_strategy_status_ui(code)
            balance_now = self._estimate_balance_now(trades, self.last_all_details or {}, code)
            info_lbl = self.strategy_ui[code]["info_lbl"]
            name = s["name"]; paused_str = " (ПАУЗА)" if st["paused"] else ""
            init_dep = self.settings["strategies"][code]["initial_deposit"]
            info_lbl.setText(
                f"{name}{paused_str}\n"
                f"Начальный депозит: ${init_dep:.2f} | "
                f"Текущий депозит: ${st['current_deposit']:.2f} | "
                f"Баланс (если закрыть все): ${balance_now:.2f}"
            )

            open_rows, closed_rows = [], []
            price_map = {}
            for a, d in (self.last_all_details or {}).items():
                if d and d.get("priceUsd"):
                    try: price_map[a] = float(d["priceUsd"])
                    except: pass
            for t in trades:
                if t.get("status") == "OPEN":
                    curr_price = price_map.get(t["address"])
                    max_dd_val = t.get("max_drawdown_pct", 0.0)
                    max_dd_cell = pct_cell(max_dd_val)
                    pnl_cell = {"text": "---", "color": None}
                    if curr_price:
                        s_open = t.get("slippage_open", self.slippage_min)
                        s_close = t.get("slippage_close", self.slippage_min)
                        eff_entry = t["entry_price"] * (1 + s_open + self.commission_open_pct)
                        eff_exit_now = curr_price * (1 - s_close - self.commission_close_pct)
                        pnl_now = (eff_exit_now - eff_entry) / eff_entry if eff_entry > 0 else 0.0
                        pnl_cell = pct_cell(pnl_now * 100.0)
                    open_rows.append([
                        t.get("name","N/A"), t["address"],
                        fmt_date(t["entry_time"]), fmt_time(t["entry_time"]),
                        f"${t['entry_price']:.8f}", f"${t['entry_size_usd']:.2f}",
                        max_dd_cell, pnl_cell
                    ])
                elif t.get("status") == "CLOSED":
                    pnl_val = t.get("pnl_pct")
                    pnl_cell2 = pct_cell(pnl_val if isinstance(pnl_val, (int, float)) else 0.0)
                    closed_rows.append([
                        t.get("name","N/A"), t["address"],
                        fmt_date(t["exit_time"]), fmt_time(t["exit_time"]),
                        f"${(t.get('exit_size_usd') or 0):.2f}", pnl_cell2
                    ])

            tv_open = self.strategy_ui[code]["open_tv"]
            tv_open.setModel(CustomTableModel(
                open_rows,
                ["Название","Адрес (CA)","Дата входа","Время входа","Цена входа","Размер ($)","Макс. просадка","Просадка сейчас"],
                [2,3,4,5,6,7]
            ))
            tv_open.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

            tv_closed = self.strategy_ui[code]["closed_tv"]
            tv_closed.setModel(CustomTableModel(
                closed_rows,
                ["Название","Адрес (CA)","Дата выхода","Время выхода","Результат ($)","PnL (%)"],
                [2,3,4,5]
            ))
            tv_closed.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        next_update_m = (datetime.now() + timedelta(milliseconds=self.refresh_ms_monitor)).strftime('%H:%M:%S')
        next_update_s = (datetime.now() + timedelta(milliseconds=self.refresh_ms_strategies)).strftime('%H:%M:%S')
        self.status_label.setText(
            f"Обновлено. Трекер: {len(self.tracker_jobs)} токенов. "
            f"След. мониторинг ~{next_update_m}, стратегий ~{next_update_s}"
        )

    def _fmt_delta(self, start_iso, end_iso):
        if not end_iso or not start_iso: return "---"
        d = datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)
        m, s = divmod(int(d.total_seconds()), 60); h, m = divmod(m, 60)
        return f"+ {h}ч {m}м" if h else f"+ {m}м {s}с"

    # ---------------- Закрытие ----------------
    def closeEvent(self, event: QCloseEvent):
        save_json(self.token_discovery_times, MEMORY_FILE)
        save_json(self.tracker_jobs, PRICE_TRACKER_FILE)
        save_json(self.archive_jobs, ARCHIVE_FILE)
        for s in STRATEGIES:
            code = s["code"]
            save_json(self.strategy_states[code]["trades"], s["log"])
        self._persist_paused()
        save_json(self.settings, SETTINGS_FILE)
        log_event("APP_CLOSE", saved="ok")
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())

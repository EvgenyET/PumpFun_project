# -*- coding: utf-8 -*-
"""
candle_engine.py — автономный модуль загрузки/дозагрузки минутных свечей из GeckoTerminal.

Версия 1.9
- ИЗМЕНЕНО: Централизован RateLimiter для предотвращения ошибок 429.
- Добавлена полноценная система "серого списка" (greylist) для обработки ошибок 404.
- Логика загрузки строго ограничена временным окном, начинающимся с discoveryTime.
"""

from __future__ import annotations
import time
import random
import threading
from urllib.request import getproxies
from urllib.parse import urlsplit, urlunsplit
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
GECKO_OHLCV = GECKO_BASE + "/networks/solana/pools/{pool}/ohlcv/minute"

OnProgress = Callable[[int, int, str], None]
OnFinish = Callable[[], None]
OnError = Callable[[str], None]
OnLog = Callable[[str, Dict[str, Any]], None]
OnLiveAppended = Callable[[int, int], None]


class RateLimiter:
    def __init__(self, rps: float = 2.0) -> None:
        self._lock = threading.Lock()
        self._period = max(0.001, 1.0 / max(0.1, rps))
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.perf_counter()
            if self._next > now:
                time.sleep(self._next - now)
            self._next = time.perf_counter() + self._period

    def set_rps(self, rps: float) -> None:
        with self._lock:
            self._period = max(0.001, 1.0 / max(0.1, rps))


def get_request_proxies() -> Dict[str, str]:
    """Исправляет схему локального HTTP CONNECT-прокси из настроек Windows."""
    proxies = getproxies()
    for protocol, proxy_url in list(proxies.items()):
        parsed = urlsplit(proxy_url)
        if parsed.scheme == "https" and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            proxies[protocol] = urlunsplit(parsed._replace(scheme="http"))
    return proxies


def http_get(url: str, *, params: Optional[Dict[str, Any]] = None,
             timeout: float = 15.0, headers: Optional[Dict[str, str]] = None) -> requests.Response:
    r = requests.get(url, params=params or {}, timeout=timeout, headers=headers or {},
                     proxies=get_request_proxies())
    r.raise_for_status()
    return r


def parse_iso_to_ts(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(float(s))
        except Exception:
            pass
        from datetime import datetime, timezone
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s.replace("T", " "), fmt).replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            except Exception:
                continue
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return None
    return None


class GeckoClient:
    def __init__(self, *, rps: float = 2.0, max_retries: int = 6,
                 backoff_base: float = 1.5, on_log: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
        self.rlim = RateLimiter(rps)
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self.on_log = on_log

    def _log(self, evt: str, **fields: Any) -> None:
        if self.on_log:
            try:
                self.on_log(evt, fields)
            except Exception:
                pass

    def fetch_ohlcv(self, pool_id: str, *, limit: int = 1000,
                    before_timestamp: Optional[int] = None) -> Tuple[List[List[Any]], Dict[str, Any]]:
        self.rlim.wait()
        params = {"aggregate": 1, "limit": max(1, min(1000, int(limit)))}
        if before_timestamp:
            params["before_timestamp"] = str(int(before_timestamp))
        headers = {"accept": "application/json"}

        for i in range(self.max_retries):
            try:
                r = http_get(GECKO_OHLCV.format(pool=pool_id), params=params, headers=headers, timeout=15)
                j = r.json()
                arr = j.get("data", {}).get("attributes", {}).get("ohlcv_list", [])
                meta = j.get("meta", {})
                return arr, meta
            except requests.exceptions.HTTPError as e:
                code = getattr(e.response, "status_code", None)
                if code == 404:
                    self._log("GECKO_API_WARN", pool=pool_id, error="POOL_NOT_FOUND")
                    return [], {}
                if code == 429:
                    ra = e.response.headers.get("Retry-After")
                    sleep_s = int(ra) if ra and ra.isdigit() else self.backoff_base ** i
                    self._log("GECKO_429_WAIT", pool=pool_id, sleep_s=sleep_s, try_num=i + 1)
                    time.sleep(sleep_s)
                    continue
                if i == self.max_retries - 1: raise
                time.sleep(self.backoff_base ** i + random.uniform(0, 0.25))
            except Exception as e:
                if i == self.max_retries - 1:
                    self._log("GECKO_MAX_RETRIES", pool=pool_id, error=str(e))
                    raise RuntimeError("GECKO_MAX_RETRIES")
                time.sleep(self.backoff_base ** i + random.uniform(0, 0.25))
        return [], {}


class BatchWorker(threading.Thread):
    def __init__(self, *, service: 'CandleService', mode: str, addresses: List[str],
                 stop_event: threading.Event,
                 on_progress: Optional[OnProgress] = None,
                 on_finish: Optional[OnFinish] = None,
                 on_error: Optional[OnError] = None,
                 on_log: Optional[OnLog] = None) -> None:
        super().__init__(daemon=True)
        self.service = service
        self.adapter = service.adapter
        self.client = service.client
        self.mode = mode
        self.addresses = list(addresses)
        self.stop_event = stop_event
        self.on_progress = on_progress
        self.on_finish = on_finish
        self.on_error = on_error
        self.on_log = on_log

    def _emit(self, evt: str, **fields: Any) -> None:
        if self.on_log:
            try:
                self.on_log(evt, fields)
            except Exception:
                pass

    def _error(self, msg: str) -> None:
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception:
                pass

    def _finish(self) -> None:
        if self.on_finish:
            try:
                self.on_finish()
            except Exception:
                pass

    def _bounds_for_addr(self, addr: str) -> Tuple[int, int, int]:
        meta = getattr(self.adapter, "meta", {}).get(addr, {})
        disc_ts = parse_iso_to_ts(meta.get("discoveryTime") or meta.get("discovery_time"))

        if not disc_ts:
            return 0, 0, 0

        now_utc = int(time.time())
        state = getattr(self.adapter, "state", {})
        days_limit = state.get("history_days_limit", 7)
        full_history = bool(state.get("full_history", False))

        last_saved = int(self.adapter.last_candle_ts(addr) or 0)

        min_ts = disc_ts

        if full_history:
            max_ts = now_utc
        else:
            if days_limit is None or days_limit <= 0: days_limit = 7
            max_ts = min(now_utc, disc_ts + int(days_limit) * 86400)

        return int(min_ts), int(max_ts), int(last_saved)

    def _backfill_for(self, addr: str, pool_id: str) -> Tuple[int, int, int]:
        pages, appended_total = 0, 0
        min_ts, max_ts, last_saved_before_run = self._bounds_for_addr(addr)

        if not min_ts:
            return 0, 0, 0

        current_oldest_ts = max_ts

        if self.mode == "update_all":
            try:
                self.adapter.erase_candles_for(addr)
                last_saved_before_run = 0
            except Exception:
                pass

        total_span = max(1, max_ts - min_ts)
        before = None

        reached_end_of_history = False
        completed_timespan = False

        while not self.stop_event.is_set():
            pages += 1
            settings = getattr(self.adapter, "settings", {})
            page_limit = int(settings.get("batch_page_limit", 1000) or 1000)
            arr, _ = self.client.fetch_ohlcv(pool_id, limit=page_limit, before_timestamp=before)

            if not arr:
                reached_end_of_history = True
                break

            bars = [(x[0], x[1], x[2], x[3], x[4], x[5]) for x in arr if isinstance(x, (list, tuple)) and len(x) >= 6]
            if not bars:
                reached_end_of_history = True
                break

            ts_list = [b[0] for b in bars]
            oldest_in_page = min(ts_list)
            newest_in_page = max(ts_list)

            new_items = [b for b in bars if min_ts <= b[0] <= max_ts and b[0] > last_saved_before_run]

            if new_items:
                new_items.sort(key=lambda x: x[0])
                for it in new_items:
                    self.adapter.append_candle(addr, it)
                appended_total += len(new_items)

            current_oldest_ts = min(current_oldest_ts, oldest_in_page)

            if oldest_in_page <= min_ts:
                completed_timespan = True
                break

            if self.mode != "update_all" and last_saved_before_run > 0 and newest_in_page < last_saved_before_run:
                break

            before = oldest_in_page

        if reached_end_of_history or completed_timespan:
            pct_done = 100
        else:
            covered_span = max(0, max_ts - current_oldest_ts)
            pct_done = int(round(100.0 * min(covered_span, total_span) / total_span))

        return pages, appended_total, max(0, min(100, pct_done))

    def run(self) -> None:
        total = len(self.addresses)
        if self.on_progress:
            try:
                self.on_progress(0, total, "Начало пакетной обработки")
            except Exception:
                pass
        done = 0
        for addr in self.addresses:
            if self.stop_event.is_set(): break
            try:
                if self.service.is_greylisted(addr):
                    done += 1
                    name = (getattr(self.adapter, "meta", {}).get(addr, {}) or {}).get("name") or "—"
                    msg = f"{name} | {addr} | pages=0 | added=0 | 0% | SKIPPED (Greylist)"
                    if self.on_progress: self.on_progress(done, total, msg)
                    continue

                force_pool_refresh = self.mode == "update_all"
                pool_id = self.adapter.ensure_pool_for(addr, rl=self.client.rlim, force_refresh=force_pool_refresh)

                name = (getattr(self.adapter, "meta", {}).get(addr, {}) or {}).get("name") or "—"
                if not pool_id:
                    done += 1
                    msg = f"{name} | {addr} | pages=0 | added=0 | 0% | NO_POOL"
                    self._emit("TOKEN_SUMMARY", name=name, address=addr, pages=0, added=0, percent=0, note="NO_POOL")
                    if self.on_progress: self.on_progress(done, total, msg)
                    continue

                pages, added, pct = self._backfill_for(addr, pool_id)
                msg = f"{name} | {addr} | pages={pages} | added={added} | {pct}%"
                self._emit("TOKEN_SUMMARY", name=name, address=addr, pages=pages, added=added, percent=pct)
                if self.on_progress: self.on_progress(done + 1, total, msg)

            except Exception as e:
                self._error(f"{addr}: {repr(e)}")
                name = (getattr(self.adapter, "meta", {}).get(addr, {}) or {}).get("name") or "—"
                msg = f"{name} | {addr} | pages=0 | added=0 | 0% | ERROR"
                self._emit("TOKEN_SUMMARY", name=name, address=addr, pages=0, added=0, percent=0, note="ERROR")
                if self.on_progress: self.on_progress(done + 1, total, msg)
            done += 1
        self._finish()


class LiveWorker(threading.Thread):
    def __init__(self, *, service: 'CandleService', stop_event: threading.Event,
                 on_live_appended: Optional[Callable[[int, int], None]] = None) -> None:
        super().__init__(daemon=True)
        self.service = service
        self.adapter = service.adapter
        self.client = service.client
        self.stop_event = stop_event
        self.on_live_appended = on_live_appended

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                state = getattr(self.adapter, "state", {})
                settings = getattr(self.adapter, "settings", {})

                if state.get("paused"):
                    time.sleep(1.0);
                    continue
                if getattr(self.adapter, "batch_in_progress", False) and settings.get("pause_live_while_batch", True):
                    time.sleep(1.0);
                    continue

                addrs = list(self.adapter.active_addresses() or [])
                if not addrs:
                    time.sleep(1.0);
                    continue

                limit_last = int(settings.get("live_recent_limit", 5) or 5)
                appended_cnt = 0
                for addr in addrs:
                    if self.stop_event.is_set(): break

                    if self.service.is_greylisted(addr):
                        continue

                    pool = self.adapter.ensure_pool_for(addr, rl=self.client.rlim)
                    if not pool: continue
                    try:
                        arr, _ = self.client.fetch_ohlcv(pool, limit=limit_last)
                    except Exception as e:
                        if self.client and self.client.on_log: self.client.on_log("LIVE_FETCH_ERR",
                                                                                  {"error": str(e), "pool": pool})
                        continue
                    last_ts = int(self.adapter.last_candle_ts(addr) or 0)
                    new_items = [(ts, o, h, l, c, v) for (ts, o, h, l, c, v) in arr if ts > last_ts]
                    if new_items:
                        new_items.sort(key=lambda x: x[0])
                        for it in new_items:
                            self.adapter.append_candle(addr, it)
                            appended_cnt += 1
                if callable(self.on_live_appended):
                    try:
                        self.on_live_appended(len(addrs), appended_cnt)
                    except Exception:
                        pass
            except Exception as e:
                if self.client and self.client.on_log: self.client.on_log("LIVE_LOOP_ERR", {"error": str(e)})
            time.sleep(int(settings.get("live_pull_interval_sec", 30) or 30))


class CandleService:
    def __init__(self, *, adapter: Any, settings: Optional[Dict[str, Any]] = None,
                 logger: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> None:
        self.adapter = adapter
        self.settings: Dict[str, Any] = dict(settings or {})
        self.logger = logger
        self._stop = threading.Event()
        self._batch_thr: Optional[BatchWorker] = None
        self._live_thr: Optional[LiveWorker] = None
        self._on_progress: Optional[OnProgress] = None;
        self._on_finish: Optional[OnFinish] = None
        self._on_error: Optional[OnError] = None;
        self._on_log = logger
        self._on_live_appended: Optional[Callable[[int, int], None]] = None
        self.client = GeckoClient(
            rps=float(self.settings.get("gecko_rps_limit", 2.0) or 2.0),
            max_retries=int(self.settings.get("gecko_max_retries", 6) or 6),
            backoff_base=float(self.settings.get("gecko_backoff_base", 1.5) or 1.5),
            on_log=self._emit_log,
        )
        self.greylist: Dict[str, Tuple[int, int]] = {}  # addr -> (timestamp, attempts)

    def add_to_greylist(self, addr: str):
        with threading.Lock():
            now = int(time.time())
            attempts = self.greylist.get(addr, (0, 0))[1] + 1
            self.greylist[addr] = (now, attempts)
            self._emit_log("GREYLIST_ADD", {"address": addr, "attempts": attempts})

    def is_greylisted(self, addr: str) -> bool:
        with threading.Lock():
            if addr not in self.greylist:
                return False

            last_attempt_ts, attempts = self.greylist[addr]
            now = int(time.time())

            if attempts >= 5:
                return True

            wait_period = (2 ** (attempts - 1)) * 300  # 5, 10, 20, 40 минут

            if (now - last_attempt_ts) < wait_period:
                return True
            else:
                return False

    def set_callbacks(self, *, on_progress: Optional[OnProgress] = None, on_finish: Optional[OnFinish] = None,
                      on_error: Optional[OnError] = None,
                      on_log: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                      on_live_appended: Optional[Callable[[int, int], None]] = None) -> None:
        self._on_progress = on_progress;
        self._on_finish = on_finish
        self._on_error = on_error;
        self._on_log = on_log or self._on_log
        self._on_live_appended = on_live_appended
        self.client.on_log = self._emit_log

    def start_batch(self, *, mode: str, addresses: Iterable[str]) -> None:
        self.stop_batch();
        self._stop.clear();
        self._set_batch_flag(True)
        thr = BatchWorker(
            service=self, mode=mode, addresses=list(addresses), stop_event=self._stop,
            on_progress=self._emit_progress, on_finish=self._wrap_finish, on_error=self._emit_error,
            on_log=self._emit_log,
        )
        self._batch_thr = thr;
        thr.start()

    def stop_batch(self) -> None:
        if self._batch_thr and self._batch_thr.is_alive():
            self._stop.set();
            self._batch_thr.join(timeout=2.0)
        self._batch_thr = None;
        self._stop.clear();
        self._set_batch_flag(False)

    def start_live(self) -> None:
        if self._live_thr and self._live_thr.is_alive(): return
        self._stop.clear()
        thr = LiveWorker(service=self, stop_event=self._stop, on_live_appended=self._emit_live_appended)
        self._live_thr = thr;
        thr.start()

    def stop_live(self) -> None:
        if self._live_thr and self._live_thr.is_alive():
            self._stop.set();
            self._live_thr.join(timeout=2.0)
        self._live_thr = None;
        self._stop.clear()

    def stop_all(self) -> None:
        self._stop.set()
        if self._live_thr and self._live_thr.is_alive(): self._live_thr.join(timeout=2.0)
        if self._batch_thr and self._batch_thr.is_alive(): self._batch_thr.join(timeout=2.0)
        self._live_thr = None
        self._batch_thr = None

    def update_settings(self, **kwargs: Any) -> None:
        self.settings.update(kwargs)
        if "gecko_rps_limit" in kwargs:
            try:
                self.client.rlim.set_rps(float(kwargs["gecko_rps_limit"]))
            except Exception:
                pass

    def _emit_progress(self, done: int, total: int, msg: str) -> None:
        if self._on_progress:
            try:
                self._on_progress(done, total, msg)
            except Exception:
                pass

    def _wrap_finish(self) -> None:
        self._set_batch_flag(False)
        if self._on_finish:
            try:
                self._on_finish()
            except Exception:
                pass

    def _emit_error(self, msg: str) -> None:
        if self._on_error:
            try:
                self._on_error(msg)
            except Exception:
                pass

    def _emit_log(self, evt: str, fields: Dict[str, Any]) -> None:
        if self._on_log:
            try:
                self._on_log(evt, fields or {})
            except Exception:
                pass

    def _emit_live_appended(self, scanned: int, appended: int) -> None:
        if self._on_live_appended:
            try:
                self._on_live_appended(scanned, appended)
            except Exception:
                pass

    def _set_batch_flag(self, value: bool) -> None:
        try:
            setattr(self.adapter, "batch_in_progress", bool(value))
        except Exception:
            pass

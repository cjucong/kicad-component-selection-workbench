"""Bounded, restartable stock-only reads. No selection or CAD writes."""
from concurrent.futures import CancelledError
from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from part_stock_lookup import StockQueryGroup, load_stock_cache, lookup_stock_with_cache, replace_stock_file


def successful(stock):
    return bool(stock.get('ok') and not stock.get('error') and
                'cached' not in str(stock.get('status', '')).lower())


def fetched_timestamp(stock):
    try:
        return datetime.fromisoformat(str(stock.get('fetched_at', ''))).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


class StockRefreshJobs:
    def __init__(self, path, cache_path, fetcher=None, *, workers=4):
        self.path, self.cache_path = Path(path), Path(cache_path)
        self.fetcher = fetcher or (lambda code: lookup_stock_with_cache(self.cache_path, code))
        self.workers = max(1, min(4, workers))
        self.lock = threading.RLock()
        self.group = StockQueryGroup()
        self.threads = []
        self.state = {'schema':1, 'state':'idle', 'items':{}, 'revision':0}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('schema') != 1 or not isinstance(data.get('items'), dict):
                raise ValueError('Invalid stock progress schema')
            if len(data['items']) > 5000 or any(not re.fullmatch(r'C\d+', code) or
                    not isinstance(item, dict) for code, item in data['items'].items()):
                raise ValueError('Invalid stock progress entries')
            self.state = data
            for item in data['items'].values():
                if item.get('state') == 'running':
                    item['state'] = 'pending'
            if data.get('state') in ('running', 'pausing'):
                data['state'] = 'paused'
                self._save()
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, AttributeError) as exc:
            self.state['state'] = 'error'
            self.state['error'] = '库存查询进度损坏，请重新开始查询：' + str(exc)

    def _save(self):
        self.state['revision'] = int(self.state.get('revision', 0)) + 1
        self.state['updated_at'] = datetime.now().isoformat(timespec='seconds')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            replace_stock_file(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _codes(codes):
        if not isinstance(codes, list) or len(codes) > 5000 or any(
                not isinstance(code, str) or not re.fullmatch(r'C\d{1,20}', code) for code in codes):
            raise ValueError('库存查询料号无效（最多 5000 项）')
        return list(dict.fromkeys(codes))

    def busy(self):
        with self.lock:
            return any(thread.is_alive() for thread in self.threads)

    def start(self, codes, mode='stale', max_age_hours=24):
        codes = self._codes(codes)
        if mode not in ('stale', 'missing', 'all') or type(max_age_hours) not in (int, float) or not (
                math.isfinite(max_age_hours) and 0 < max_age_hours <= 8760):
            raise ValueError('库存查询范围或有效期无效')
        with self.lock:
            if self.busy():
                raise ValueError('库存仍在查询或暂停中，请先暂停后再开始')
            cache = load_stock_cache(self.cache_path)
            now = time.time()
            selected = []
            skipped = 0
            for code in codes:
                stock = cache.get(code, {})
                timestamp = fetched_timestamp(stock)
                fresh = successful(stock) and 0 <= now-timestamp < max_age_hours*3600
                if (mode == 'stale' and fresh) or (mode == 'missing' and successful(stock)):
                    skipped += 1
                else:
                    selected.append(code)
            selected.sort(key=lambda code: fetched_timestamp(cache.get(code, {})) if successful(cache.get(code, {})) else 0)
            self.state = {'schema':1, 'id':uuid4().hex, 'revision':0, 'state':'paused',
                'started_at':datetime.now().isoformat(timespec='seconds'), 'mode':mode,
                'max_age_hours':max_age_hours, 'skipped_fresh':skipped,
                'items':{code:{'state':'pending'} for code in selected}}
            return self._launch()

    def resume(self, codes):
        allowed = set(self._codes(codes))
        with self.lock:
            if self.busy():
                raise ValueError('库存仍在查询或暂停中，请稍候')
            if self.state.get('state') != 'paused':
                raise ValueError('没有可继续的库存查询')
            for code, item in self.state['items'].items():
                if item.get('state') == 'pending' and code not in allowed:
                    item['state'] = 'skipped'
                    item['error'] = '当前查询范围不再包含此料号'
            self.state.pop('error', None)
            return self._launch()

    def _launch(self):
        self.state['state'] = 'running' if any(
            item.get('state') == 'pending' for item in self.state['items'].values()) else 'completed'
        try:
            self._save()
        except OSError:
            self.state['state'] = 'paused'
            self.state['error'] = '无法保存库存查询进度，尚未开始网络查询'
            raise
        if self.state['state'] == 'running':
            self.threads = [threading.Thread(target=self._worker, daemon=True, name='stock-refresh')
                            for _ in range(self.workers)]
            for thread in self.threads:
                thread.start()
        return self.status()

    def pause(self):
        with self.lock:
            if self.state.get('state') == 'running':
                self.state['state'] = 'pausing'
                try:
                    self._save()
                finally:
                    self.group.cancel()
            return self.status()

    def _worker(self):
        item = None
        try:
            while True:
                with self.lock:
                    if self.state['state'] != 'running':
                        return
                    code = next((code for code, item in self.state['items'].items()
                                 if item.get('state') == 'pending'), None)
                    if code is None:
                        return
                    item = self.state['items'][code]
                    item['state'] = 'running'
                    self._save()
                try:
                    def fetch():
                        with self.lock:
                            if self.state['state'] != 'running':
                                raise CancelledError('Stock refresh paused')
                        return self.fetcher(code)
                    stock = self.group.run(fetch)
                    outcome = {'state':'succeeded' if successful(stock) else 'failed',
                               'checked_at':datetime.now().isoformat(timespec='seconds'),
                               'fetched_at':stock.get('fetched_at', ''), 'error':stock.get('error', '')}
                except CancelledError:
                    outcome = {'state':'pending'}
                except Exception as exc:
                    outcome = {'state':'failed', 'error':str(exc),
                               'checked_at':datetime.now().isoformat(timespec='seconds')}
                with self.lock:
                    item.clear()
                    item.update(outcome)
                    item['revision'] = int(self.state.get('revision', 0)) + 1
                    self._save()
        except Exception as exc:
            with self.lock:
                if item is not None and item.get('state') == 'running':
                    item['state'] = 'pending'
                self.state['state'] = 'pausing'
                self.state['error'] = '保存库存查询进度失败：' + str(exc)
                self.group.cancel()
        finally:
            with self.lock:
                if not any(item.get('state') == 'running' for item in self.state['items'].values()):
                    pending = any(item.get('state') == 'pending' for item in self.state['items'].values())
                    self.state['state'] = 'paused' if pending else 'completed'
                    try:
                        self._save()
                    except OSError as exc:
                        self.state['error'] = '保存库存查询进度失败：' + str(exc)

    def status(self, since=None, job_id=None):
        with self.lock:
            data = deepcopy(self.state)
        if job_id is not None and data.get('id', '') != job_id:
            since = 0
        items = data.pop('items', {})
        for key in ('pending','running','succeeded','failed','skipped'):
            data[key] = sum(item.get('state') == key for item in items.values())
        data.update(ok=True, total=len(items), done=data['succeeded']+data['failed']+data['skipped'],
                    active_codes=[code for code, item in items.items() if item.get('state') == 'running'])
        if since is not None:
            cache = load_stock_cache(self.cache_path)
            data['results'] = {code:cache[code] for code, item in items.items()
                if item.get('revision', 0) > since and item.get('state') in ('succeeded','failed') and code in cache}
            data['items'] = items
        return data

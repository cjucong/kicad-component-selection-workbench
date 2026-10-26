"""Durable inventory reads use disposable state, never production selections."""
import json
import threading
import time
from datetime import datetime, timedelta

import pytest

from part_stock_lookup import StockQueryGroup, check_stock_cancelled, save_stock_cache
from part_selection_stock_refresh import StockRefreshJobs


def wait_for(jobs, state='completed'):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = jobs.status()
        if result['state'] == state:
            return result
        time.sleep(.01)
    pytest.fail(str(jobs.status()))


def make_jobs(root, fetcher, workers=1):
    return StockRefreshJobs(root / 'progress.json', root / 'stock.json', fetcher, workers=workers)


def ok(code):
    return {'ok': True, 'status': 'OK', 'c_code': code, 'stock': '123',
            'fetched_at': datetime.now().isoformat(timespec='seconds')}


def test_freshness_deduplicates_and_prioritizes_oldest(scratch_project):
    root = scratch_project
    now = datetime.now()
    save_stock_cache(root / 'stock.json', {
        'C100': {**ok('C100'), 'fetched_at': now.isoformat()},
        'C200': {**ok('C200'), 'fetched_at': (now-timedelta(days=2)).isoformat()},
        'C300': {**ok('C300'), 'fetched_at': (now-timedelta(days=8)).isoformat()}})
    seen = []
    jobs = make_jobs(root, lambda code: seen.append(code) or ok(code))
    jobs.start(['C100','C200','C300','C400','C200'], mode='stale', max_age_hours=24)
    result = wait_for(jobs)
    assert seen == ['C400','C300','C200']
    assert result['skipped_fresh'] == 1 and result['succeeded'] == 3


def test_pause_restart_and_resume_only_unfinished(scratch_project):
    started = threading.Event()
    seen = []
    def fetch(code):
        seen.append(code)
        if code == 'C200':
            started.set()
            while True:
                check_stock_cancelled()
                time.sleep(.01)
        return ok(code)
    jobs = make_jobs(scratch_project, fetch)
    jobs.start(['C100','C200','C300'], mode='all')
    assert started.wait(2)
    jobs.pause()
    paused = wait_for(jobs, 'paused')
    assert paused['succeeded'] == 1 and paused['pending'] == 2
    second = []
    reopened = make_jobs(scratch_project, lambda code: second.append(code) or ok(code))
    assert reopened.status()['state'] == 'paused'
    reopened.resume(['C100','C200','C300'])
    assert wait_for(reopened)['succeeded'] == 3
    assert second == ['C200','C300']


def test_abrupt_restart_recovers_running_items_as_paused(scratch_project):
    path = scratch_project / 'progress.json'
    path.write_text(json.dumps({'schema':1, 'state':'running', 'items':{
        'C100':{'state':'succeeded'}, 'C200':{'state':'running'}}}))
    jobs = make_jobs(scratch_project, ok)
    assert jobs.status()['state'] == 'paused'
    assert jobs.status()['pending'] == 1


def test_resume_drops_codes_no_longer_selected_and_never_changes_selections(scratch_project):
    path = scratch_project / 'progress.json'
    path.write_text(json.dumps({'schema':1, 'state':'paused', 'items':{
        'C100':{'state':'pending'}, 'C200':{'state':'pending'}}}))
    seen = []
    jobs = make_jobs(scratch_project, lambda code: seen.append(code) or ok(code))
    jobs.resume(['C200'])
    assert wait_for(jobs)['succeeded'] == 1
    assert seen == ['C200']


def test_failure_and_old_cached_fallback_do_not_count_as_fresh_success(scratch_project):
    fallback = {**ok('C100'), 'status':'QueryFailed/Cached','error':'timeout'}
    save_stock_cache(scratch_project / 'stock.json', {'C100':fallback})
    jobs = make_jobs(scratch_project, lambda code: fallback)
    jobs.start(['C100'], mode='stale')
    result = wait_for(jobs)
    assert result['failed'] == 1 and result['succeeded'] == 0


def test_parallel_workers_bounded_and_pause_does_not_cancel_row_reads(scratch_project):
    active = 0
    peak = 0
    lock = threading.Lock()
    entered = threading.Event()
    def fetch(code):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                entered.set()
        try:
            while True:
                check_stock_cancelled()
                time.sleep(.01)
        finally:
            with lock:
                active -= 1
    jobs = make_jobs(scratch_project, fetch, workers=4)
    jobs.start(['C'+str(x) for x in range(100,110)], mode='all')
    try:
        assert entered.wait(2)
    finally:
        jobs.pause()
    wait_for(jobs, 'paused')
    assert peak == 4
    assert StockQueryGroup().run(lambda: ok('C500'))['ok']


def test_invalid_queue_and_overlapping_start_rejected(scratch_project):
    jobs = make_jobs(scratch_project, ok)
    with pytest.raises(ValueError):
        jobs.start(['C100<script>'], mode='all')
    with pytest.raises(ValueError):
        jobs.start(['C100'], max_age_hours=-1)


def test_domestic_shipping_starts_while_international_request_is_pending():
    from unittest.mock import patch
    from part_stock_lookup import query_lcsc_stock
    entered = threading.Event()
    response = {'code':200,'result':{'dataList':[{'productCode':'C100','stockNumber':10,
        'cnyProductPriceList':[{'ladder':1,'productPrice':1}]}]}}
    def domestic(*args):
        entered.set()
        return []
    def read():
        assert entered.wait(.5), 'Domestic lookup still waits for international lookup'
        return json.dumps(response).encode()
    with patch('part_stock_lookup.urllib.request.urlopen') as network:
        network.return_value.__enter__.return_value.read.side_effect = read
        assert query_lcsc_stock('C100', domestic_fetcher=domestic)['ok']


def test_default_domestic_refresh_bypasses_short_search_cache():
    from unittest.mock import patch
    from part_stock_lookup import query_lcsc_stock
    with patch('part_stock_lookup.urllib.request.urlopen', side_effect=OSError('offline')), \
         patch('part_stock_lookup.fetch_szlcsc_domestic_products', return_value=[]) as domestic:
        assert not query_lcsc_stock('C100')['ok']
    domestic.assert_called_once_with('C100', 8, force_refresh=True)


def test_cancel_releases_only_the_owned_reader_process():
    from concurrent.futures import CancelledError
    from unittest.mock import patch
    import os
    import subprocess
    import sys
    from part_stock_lookup import _read_domestic_browser
    started = threading.Event()
    owned, errors = [], []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        owned.append(process)
        started.set()
        return process
    group = StockQueryGroup()
    def read():
        try:
            group.run(lambda: _read_domestic_browser(
                [sys.executable, '-c', 'import time; time.sleep(15)'], 15,
                subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
        except BaseException as exc:
            errors.append(exc)
    with patch('part_stock_lookup.subprocess.Popen', side_effect=launch):
        worker = threading.Thread(target=read)
        worker.start()
        try:
            assert started.wait(2)
        finally:
            group.cancel()
            worker.join(timeout=4)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], CancelledError)
    assert owned[0].poll() is not None


def test_failed_refresh_keeps_success_time_and_persists_attempt(scratch_project):
    from part_stock_lookup import load_stock_cache, lookup_stock_with_cache
    previous = {**ok('C100'), 'fetched_at':'2026-01-01T00:00:00'}
    path = scratch_project / 'stock.json'
    save_stock_cache(path, {'C100':previous})
    result = lookup_stock_with_cache(path, 'C100', lambda code:
        {'ok':False, 'c_code':code, 'status':'QueryFailed', 'error':'timeout'})
    assert result['fetched_at'] == previous['fetched_at']
    assert result['last_checked_at']
    assert result['error'] == 'timeout' and 'Cached' in result['status']
    assert load_stock_cache(path)['C100'] == result


def test_progress_cursor_from_previous_run_cannot_hide_new_results(scratch_project):
    from part_stock_lookup import lookup_stock_with_cache
    path = scratch_project / 'stock.json'
    jobs = make_jobs(scratch_project, lambda code: lookup_stock_with_cache(path, code, ok))
    first = jobs.start(['C100'], mode='all')
    wait_for(jobs)
    while jobs.busy():
        time.sleep(.01)
    jobs.start(['C200'], mode='all')
    wait_for(jobs)
    status = jobs.status(since=10000, job_id=first['id'])
    assert 'C200' in status['results']
    assert not jobs.status(since=status['revision'], job_id=status['id'])['results']


def test_progress_save_failure_never_starts_network(scratch_project, monkeypatch):
    seen = []
    jobs = make_jobs(scratch_project, lambda code: seen.append(code) or ok(code))
    def fail():
        raise OSError('disk full')
    monkeypatch.setattr(jobs, '_save', fail)
    with pytest.raises(OSError):
        jobs.start(['C100'], mode='all')
    assert seen == [] and not jobs.busy()
    assert jobs.status()['state'] == 'paused'


def test_all_fast_parallel_results_finish_without_premature_pause(scratch_project):
    seen = []
    jobs = make_jobs(scratch_project, lambda code: seen.append(code) or ok(code), workers=4)
    codes = ['C'+str(i) for i in range(100,108)]
    for _ in range(3):
        jobs.start(codes, mode='all')
        result = wait_for(jobs)
        for worker in jobs.threads:
            worker.join(timeout=2)
        assert result['succeeded'] == len(codes)
    assert len(seen) == len(codes)*3


def test_pause_cancels_reads_even_when_progress_cannot_be_saved(scratch_project, monkeypatch):
    started = threading.Event()
    def fetch(code):
        started.set()
        while True:
            check_stock_cancelled()
            time.sleep(.01)
    jobs = make_jobs(scratch_project, fetch)
    jobs.start(['C100'], mode='all')
    assert started.wait(2)
    def fail():
        raise OSError('disk full')
    monkeypatch.setattr(jobs, '_save', fail)
    with pytest.raises(OSError):
        jobs.pause()
    for thread in jobs.threads:
        thread.join(timeout=2)
    assert not jobs.busy()
    assert jobs.status()['pending'] == 1
    assert jobs.status()['state'] == 'paused'
    assert 'disk full' in jobs.status()['error']


def test_only_authenticated_exit_pauses_persistent_queue_and_paths_wait(scratch_project):
    from unittest.mock import Mock, patch
    from part_selection_server import PartSelectionApp
    from part_selection_lifecycle import DesktopExitPending, ServiceLifecycle
    config = scratch_project / 'config.json'
    config.write_text(json.dumps({'schematic_dir':'.'}))
    app = PartSelectionApp(config)
    started = threading.Event()
    def fetch(code):
        started.set()
        while True:
            check_stock_cancelled()
            time.sleep(.01)
    app.stock_refresh.fetcher = fetch
    try:
        app.stock_refresh.start(['C100'], mode='all')
        assert started.wait(2)
        lifecycle = ServiceLifecycle(app, Mock(), 'secret')
        with patch.object(app.stock_refresh, 'pause', wraps=app.stock_refresh.pause) as pause:
            with pytest.raises(ValueError, match='身份'):
                lifecycle.shutdown('other')
            pause.assert_not_called()
        with pytest.raises(ValueError, match='暂停库存查询'):
            app.save_path_settings({})
        app.settings_gate.active = 2
        with pytest.raises(DesktopExitPending):
            lifecycle.shutdown('secret')
        assert wait_for(app.stock_refresh, 'paused')['pending'] == 1
        assert not app.settings_gate.stopping
        restored = app.create_stock_refresh()
        assert restored.status()['state'] == 'paused'
        assert restored.status()['pending'] == 1
    finally:
        app.stock_refresh.pause()
        for worker in app.stock_refresh.threads:
            worker.join(timeout=2)
        app.search_jobs.close()
        app.selection_jobs.close()

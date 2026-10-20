"""Bounded candidate-search jobs and short-lived, timestamped result snapshots."""

from collections import OrderedDict
from concurrent.futures import CancelledError, ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import threading
import time
from uuid import uuid4


class CandidateSearchJobs:
    def __init__(self, loader, seed=None, *, clock=time.monotonic, max_workers=2):
        self._loader = loader
        self._seed = seed
        self._clock = clock
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="candidate-search")
        self._jobs = OrderedDict()
        self._clients = OrderedDict()
        self._cache = OrderedDict()

    def close(self):
        with self._lock:
            for job in self._jobs.values():
                self._cancel(job)
        self._pool.shutdown(wait=True, cancel_futures=True)

    def busy(self):
        with self._lock:
            return any(job.get('future') and not job['future'].done() for job in self._jobs.values())

    def _snapshot(self, job):
        state = deepcopy(job["state"])
        state["elapsed_ms"] = round((self._clock() - job["started"]) * 1000)
        return state

    def _cancel(self, job):
        if not job["state"]["done"]:
            job["cancel"].set()
            job["state"].update(done=True, cancelled=True)
            future = job.get("future")
            if future:
                future.cancel()

    def cancel(self, client, sequence):
        with self._lock:
            now = self._clock()
            for name, entry in list(self._clients.items()):
                if now - entry[2] > 600:
                    self._clients.pop(name)
            if client not in self._clients and len(self._clients) >= 128:
                return {"ok": False, "error": "Search queue is busy; please retry"}
            previous = self._clients.get(client)
            if previous and sequence >= previous[0]:
                job = self._jobs.get(previous[1])
                if job:
                    self._cancel(job)
                self._clients[client] = (sequence, "", self._clock())
            elif not previous:
                self._clients[client] = (sequence, "", self._clock())
            return {"ok": True}

    def status(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"ok": False, "done": True, "error": "Search expired; please search again", "items": []}
            return self._snapshot(job)

    def start(self, keyword, substitutes, client, sequence, *, force_refresh=False):
        keyword = " ".join(str(keyword).split())
        if not keyword or not client or len(client) > 100 or len(keyword) > 500:
            return {"ok": False, "done": True, "error": "Invalid search request", "items": []}
        key = (keyword.casefold(), bool(substitutes))
        with self._lock:
            now = self._clock()
            previous = self._clients.get(client)
            if previous and sequence <= previous[0]:
                old = self._jobs.get(previous[1])
                if sequence == previous[0] and old and old["key"] == key:
                    return self._snapshot(old)
                return {"ok": True, "done": True, "cancelled": True, "items": []}
            if previous:
                old = self._jobs.get(previous[1])
                if old:
                    self._cancel(old)
            # Keep only bounded snapshots and short-lived session ordering guards.
            for job_id, old in list(self._jobs.items()):
                if old["state"]["done"] and (now - old["started"] > 600 or len(self._jobs) >= 64):
                    del self._jobs[job_id]
            for name, entry in list(self._clients.items()):
                if now - entry[2] > 600:
                    self._clients.pop(name)
            if len(self._jobs) >= 64 or len(self._clients) >= 128 and client not in self._clients:
                return {"ok": False, "done": True, "error": "Search queue is busy; please retry", "items": []}
            cached = self._cache.get(key)
            initial = {}
            age = 0
            if cached and now - cached[0] < 600:
                age = now - cached[0]
                initial = deepcopy(cached[1])
                self._cache.move_to_end(key)
            elif self._seed:
                initial = self._seed(keyword) or {}
                age = 600
            fresh = bool(initial.get("items")) and age < 60 and not force_refresh
            job_id = uuid4().hex
            state = {**initial, "ok": True, "error": "", "items": initial.get("items", []),
                     "job_id": job_id, "done": fresh, "cancelled": False, "revision": 0,
                     "cached": bool(initial.get("items")), "stale": not fresh and bool(initial.get("items")),
                     "sources": initial.get("sources", {}) if fresh else {}}
            job = {"key": key, "state": state, "started": now, "cancel": threading.Event()}
            self._jobs[job_id] = job
            self._clients[client] = (sequence, job_id, now)
            # Capture before submit so even an instant refresh first returns its cached snapshot.
            snapshot = self._snapshot(job)
            if not fresh:
                job["future"] = self._pool.submit(self._run, job, keyword, substitutes, force_refresh)
            return snapshot

    def _run(self, job, keyword, substitutes, force_refresh):
        def publish(result):
            with self._lock:
                if job["cancel"].is_set():
                    raise CancelledError()
                state = job["state"]
                # A source-only progress update must not erase a useful cached result.
                if result.get("items"):
                    state.update(deepcopy(result), cached=False, stale=False,
                                 checked_at=datetime.now().isoformat(timespec="seconds"))
                else:
                    state["sources"] = deepcopy(result.get("sources", {}))
                state["revision"] += 1

        try:
            if job["cancel"].is_set():
                return
            result = self._loader(keyword, substitutes, on_partial=publish,
                                  cancelled=job["cancel"].is_set, force_refresh=force_refresh)
            with self._lock:
                if job["cancel"].is_set():
                    return
                state = job["state"]
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "Search failed")
                state.update(deepcopy(result), cached=False, stale=False, done=True,
                             checked_at=datetime.now().isoformat(timespec="seconds"))
                state["revision"] += 1
                if result.get("items") and not any(s in {"failed", "querying"} for s in result.get("sources", {}).values()):
                    self._cache[job["key"]] = (self._clock(), deepcopy(state))
                    self._cache.move_to_end(job["key"])
                    while len(self._cache) > 32:
                        self._cache.popitem(last=False)
                else:
                    self._cache.pop(job["key"], None)
        except CancelledError:
            pass
        except Exception as exc:
            with self._lock:
                if not job["cancel"].is_set():
                    state = job["state"]
                    cached = self._cache.get(job["key"])
                    if cached:
                        self._cache[job["key"]] = (min(cached[0], self._clock() - 60), cached[1])
                    state.update(ok=bool(state.get("items")), error=str(exc), done=True,
                                 cached=bool(state.get("items")), stale=bool(state.get("items")))
                    state["revision"] += 1

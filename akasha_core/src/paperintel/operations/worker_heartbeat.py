"""Worker-process heartbeat, independent of broker backlog and task duration.

Prefork children start after fork; solo/thread workers start at worker_ready.
No threads or filesystem writes are created just by importing this module.
"""

from __future__ import annotations

import logging
import threading

from paperintel.operations.heartbeat import prune_heartbeats, write_heartbeat

logger = logging.getLogger(__name__)


class HeartbeatWriter:
    def __init__(self, data_dir, *, interval: float = 30.0):
        self.data_dir = data_dir
        self.interval = interval
        self._tasks: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="worker-heartbeat")

    def emit(self):
        with self._lock:
            try:
                write_heartbeat(
                    self.data_dir,
                    state="RUNNING" if self._tasks else "IDLE",
                    detail={"task_ids": sorted(self._tasks)},
                )
                prune_heartbeats(self.data_dir)
            except OSError:
                logger.warning("Cannot write worker heartbeat", exc_info=True)

    def start(self):
        self.emit()
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            self.emit()

    def task_started(self, task_id):
        with self._lock:
            self._tasks.add(str(task_id))
        self.emit()

    def task_finished(self, task_id):
        with self._lock:
            self._tasks.discard(str(task_id))
        self.emit()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)


_writer: HeartbeatWriter | None = None
_installed = False


def _start(**kwargs):
    global _writer
    if _writer is None:
        from paperintel.config.settings import get_settings

        _writer = HeartbeatWriter(get_settings().core.data_dir)
        _writer.start()


def _ready(sender=None, **kwargs):
    if sender is not None and type(sender.pool).__module__ in (
        "celery.concurrency.solo", "celery.concurrency.thread",
    ):
        _start()


def _stop(**kwargs):
    global _writer
    if _writer is not None:
        _writer.stop()
        _writer = None


def _prerun(task_id=None, **kwargs):
    if _writer is not None:
        _writer.task_started(task_id)


def _postrun(task_id=None, **kwargs):
    if _writer is not None:
        _writer.task_finished(task_id)


def install_signals():
    global _installed
    if _installed:
        return
    from celery import signals

    for signal, receiver in (
        (signals.worker_process_init, _start),
        (signals.worker_ready, _ready),
        (signals.worker_process_shutdown, _stop),
        (signals.worker_shutdown, _stop),
        (signals.task_prerun, _prerun),
        (signals.task_postrun, _postrun),
    ):
        signal.connect(receiver, weak=False)
    _installed = True

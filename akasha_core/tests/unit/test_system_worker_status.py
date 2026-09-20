"""Legacy status must use liveness, not task activity, for worker count."""

import time
from unittest.mock import Mock

import pytest

from paperintel.config.settings import AppConfig, CoreSettings
from paperintel.operations.heartbeat import write_heartbeat
from paperintel.services.read_models import system_status


@pytest.mark.parametrize(('running', 'fresh'), [(0, 2), (3, 0), (1, 1)])
def test_worker_count_tracks_fresh_heartbeats(tmp_path, monkeypatch, running, fresh):
    monkeypatch.delenv('PAPERINTEL_TASKS_EAGER', raising=False)
    for index in range(fresh):
        write_heartbeat(tmp_path, identity=f'live-{index}', state='IDLE')
    write_heartbeat(tmp_path, identity='old', now=time.time() - 600)
    session = Mock()
    session.scalar.side_effect = [0, running, 0, 0] + [0] * 20
    settings = AppConfig(core=CoreSettings(data_dir=tmp_path))
    result = system_status(session, settings=settings)
    assert result['queue']['running'] == running
    assert result['workers'] == {'worker_count': fresh, 'mode': 'celery'}

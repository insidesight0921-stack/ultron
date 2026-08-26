from __future__ import annotations

import signal
import subprocess

import pytest

import smoke_data_api as smoke


class _Process:
    def __init__(self, *, exit_code=None, output=""):
        self.exit_code = exit_code
        self.stdout = _Output(output)
        self.signals = []
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.exit_code

    def send_signal(self, value):
        self.signals.append(value)
        self.exit_code = 0

    def wait(self, timeout):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 0

    def kill(self):
        self.killed = True
        self.exit_code = -9


class _Output:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


def test_validate_health_accepts_expected_boundary():
    smoke._validate_health(
        {
            "status": "ok",
            "service": "shareable_data_api",
            "boundary": "shareable-only",
        }
    )


def test_validate_health_rejects_private_boundary():
    with pytest.raises(smoke.SmokeError):
        smoke._validate_health(
            {
                "status": "ok",
                "service": "shareable_data_api",
                "boundary": "private",
            }
        )


def test_wait_for_health_retries_then_succeeds():
    process = _Process()
    replies = iter(
        [
            None,
            {
                "status": "ok",
                "service": "shareable_data_api",
                "boundary": "shareable-only",
            },
        ]
    )
    clock = iter([0.0, 0.1, 0.2, 0.3])
    result = smoke._wait_for_health(
        process,
        timeout=1,
        probe=lambda: next(replies),
        sleeper=lambda value: None,
        monotonic=lambda: next(clock),
    )
    assert result["status"] == "ok"


def test_wait_for_health_reports_early_exit():
    process = _Process(exit_code=1, output="bind failed")
    clock = iter([0.0, 0.1, 0.2])
    with pytest.raises(smoke.SmokeError, match="bind failed"):
        smoke._wait_for_health(
            process,
            timeout=1,
            probe=lambda: None,
            sleeper=lambda value: None,
            monotonic=lambda: next(clock),
        )


def test_stop_owned_process_only_stops_live_child():
    process = _Process()
    smoke._stop_owned_process(process)
    assert process.signals == [signal.SIGINT]
    assert process.terminated is False
    assert process.killed is False

    stopped = _Process(exit_code=0)
    smoke._stop_owned_process(stopped)
    assert stopped.signals == []


def test_stop_owned_process_escalates_only_after_timeouts():
    class Stubborn(_Process):
        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("data_api", timeout)
            return self.exit_code

        def terminate(self):
            self.terminated = True

    process = Stubborn()
    smoke._stop_owned_process(process)
    assert process.signals == [signal.SIGINT]
    assert process.terminated is True
    assert process.killed is True

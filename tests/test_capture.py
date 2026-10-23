import threading
import wave
from types import SimpleNamespace

import pytest
from voice_journal import capture_worker


class WaitingInput:
    def __init__(self):
        self.release = threading.Event()

    def __iter__(self):
        self.release.wait(5)
        yield "STOP"


def fake_audio(fail=False):
    class Audio:
        reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_loopback_device_info_generator(self):
            yield {
                "index": 1,
                "name": "Synthetic device",
                "defaultSampleRate": 16000,
                "maxInputChannels": 1,
            }

        def open(self, **kwargs):
            return self

        def read(self, frames, **kwargs):
            self.reads += 1
            if fail and self.reads > 2:
                raise OSError("Synthetic device disconnect")
            return b"\0\0" * frames

    return SimpleNamespace(PyAudio=Audio, paInt16=8)


def test_capture_rotates_files_and_stops_at_limit(monkeypatch, tmp_path):
    control = WaitingInput()
    events = []
    monkeypatch.setitem(__import__("sys").modules, "pyaudiowpatch", fake_audio())
    monkeypatch.setattr(capture_worker.sys, "stdin", control)
    monkeypatch.setattr(capture_worker, "emit", events.append)
    try:
        capture_worker.run(
            SimpleNamespace(action="record", device=1, output=str(tmp_path), maximum=2, segment=1)
        )
    finally:
        control.release.set()
    chunks = [item for item in events if item["event"] == "chunk"]
    assert [item["offset"] for item in chunks] == [0, 1]
    assert events[-1]["limit_reached"]
    for chunk in chunks:
        with wave.open(str(tmp_path / chunk["file"]), "rb") as audio:
            assert audio.getnframes() == 16000


def test_device_failure_seals_and_reports_partial_recording(monkeypatch, tmp_path):
    control = WaitingInput()
    events = []
    monkeypatch.setitem(__import__("sys").modules, "pyaudiowpatch", fake_audio(fail=True))
    monkeypatch.setattr(capture_worker.sys, "stdin", control)
    monkeypatch.setattr(capture_worker, "emit", events.append)
    try:
        with pytest.raises(OSError, match="disconnect"):
            capture_worker.run(
                SimpleNamespace(
                    action="record", device=1, output=str(tmp_path), maximum=2, segment=1
                )
            )
    finally:
        control.release.set()
    chunk = next(item for item in events if item["event"] == "chunk")
    with wave.open(str(tmp_path / chunk["file"]), "rb") as audio:
        assert audio.getnframes() == 2048

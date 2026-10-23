import sys
import time
import wave
from types import SimpleNamespace

from voice_journal.client.recorder import Recorder
from voice_journal.client.spool import Spool


def fake_devices(fail_mic=False):
    class Stream:
        def __init__(self, index):
            self.index, self.reads = index, 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def start_stream(self):
            pass

        def stop_stream(self):
            pass

        def read(self, frames, **kwargs):
            self.reads += 1
            time.sleep(0.001)
            if fail_mic and self.index == 2 and self.reads > 2:
                raise OSError("Microphone disconnected")
            return (b"\x01\x00\x03\x00" if self.index == 1 else b"\x07\x00") * frames

    class Audio:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get_device_info_by_index(self, index):
            return {
                "index": index,
                "name": str(index),
                "defaultSampleRate": 16000,
                "maxInputChannels": 2 if index == 1 else 1,
            }

        def open(self, **kwargs):
            return Stream(kwargs["input_device_index"])

    return SimpleNamespace(PyAudio=Audio, paInt16=8)


def run_capture(monkeypatch, tmp_path, fail=False):
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", fake_devices(fail))
    spool = Spool(tmp_path)
    folder = spool.create({"fingerprint": "a" * 64})
    events = []
    selected = {"playback": {"index": 1, "name": "1"}, "microphone": {"index": 2, "name": "2"}}
    recorder = Recorder(spool, folder, selected, 1, 2, lambda *event: events.append(event))
    recorder.start()
    recorder.thread.join(timeout=10)
    assert not recorder.thread.is_alive()
    return spool.read(folder), folder, events


def test_separate_tracks_offsets_mono_and_bound(monkeypatch, tmp_path):
    data, folder, events = run_capture(monkeypatch, tmp_path)
    assert data["complete"] and not data["error"]
    assert {item["track"] for item in data["parts"]} == {"playback", "microphone"}
    assert len(data["parts"]) == 4
    for item in data["parts"]:
        with wave.open(str(folder / item["file"]), "rb") as audio:
            assert audio.getnchannels() == 1
            assert item["offset"] + audio.getnframes() / audio.getframerate() <= 2.001
            assert audio.readframes(1) == (
                b"\x02\x00" if item["track"] == "playback" else b"\x07\x00"
            )
    assert events[-1][0] == "stopped"


def test_microphone_failure_stops_both_and_preserves_partial(monkeypatch, tmp_path):
    data, folder, events = run_capture(monkeypatch, tmp_path, fail=True)
    assert data["complete"] and "Microphone disconnected" in data["error"]
    assert any(item["track"] == "microphone" for item in data["parts"])
    assert any(item[0] == "error" for item in events)
    for item in data["parts"]:
        with wave.open(str(folder / item["file"]), "rb") as audio:
            assert audio.getnframes() > 0


def test_recover_does_not_restart_capture_or_upload_to_other_server(tmp_path):
    spool = Spool(tmp_path)
    folder = spool.create({"fingerprint": "a" * 64})
    assert spool.recover() == 1
    assert spool.read(folder)["complete"]

    class OtherServer:
        profile = {"fingerprint": "b" * 64}

        def request(self, *args, **kwargs):
            raise AssertionError("must not send old audio to a different server")

    assert spool.sync(OtherServer())["other_server"] == 1


def test_rejected_recording_does_not_block_new_recordings(tmp_path):
    spool = Spool(tmp_path)
    profile = {"fingerprint": "a" * 64}
    rejected, ready = spool.create(profile), spool.create(profile)
    spool.update(rejected, complete=True)
    spool.update(ready, complete=True)

    class Server:
        def __init__(self):
            self.profile = profile

        def request(self, method, path, data):
            if data.get("client_id") == rejected.name:
                raise ValueError("A has closed this recording")
            return {"maximum_seconds": 7200}

    result = spool.sync(Server())
    assert result["completed"] == 1 and result["pending"] == 1
    assert "closed" in result["errors"][0]["error"]
    assert spool.read(ready)["synced"]
    assert not spool.read(rejected)["synced"]

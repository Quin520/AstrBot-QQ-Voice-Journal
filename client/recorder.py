"""Simultaneous WASAPI playback and microphone capture into separate timed WAV tracks."""

import threading
import time
import wave
from array import array


def devices() -> dict:
    import pyaudiowpatch as audio

    with audio.PyAudio() as client:
        host = client.get_host_api_info_by_type(audio.paWASAPI)["index"]
        rows = list(client.get_device_info_generator())
        fields = ("index", "name", "defaultSampleRate", "maxInputChannels")
        return {
            "playback": [
                {key: row[key] for key in fields} for row in rows if row.get("isLoopbackDevice")
            ],
            "microphone": [
                {key: row[key] for key in fields}
                for row in rows
                if row["hostApi"] == host
                and row["maxInputChannels"] > 0
                and not row.get("isLoopbackDevice")
            ],
        }


def mono_pcm(block: bytes, channels: int) -> bytes:
    if channels == 1:
        return block
    samples = array("h", block)
    return array(
        "h",
        (
            int(sum(samples[index : index + channels]) / channels)
            for index in range(0, len(samples), channels)
        ),
    ).tobytes()


class Recorder:
    def __init__(self, spool, folder, selected: dict, segment: int, maximum: int, notify):
        self.spool, self.folder, self.selected = spool, folder, selected
        self.segment, self.maximum, self.notify = segment, maximum, notify
        self.stopped, self.ready = threading.Event(), threading.Event()
        self.barrier = threading.Barrier(len(selected))
        self.errors, self.threads = [], []
        self.started = 0.0
        self.thread = None

    def start(self) -> None:
        if self.thread:
            raise ValueError("Capture already started")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopped.set()

    def _run(self) -> None:
        self.started = time.monotonic()
        for track, device in self.selected.items():
            thread = threading.Thread(target=self._track, args=(track, device), daemon=True)
            self.threads.append(thread)
            thread.start()
        for thread in self.threads:
            thread.join()
        error = "; ".join(self.errors)
        self.spool.update(self.folder, complete=True, error=error)
        self.notify("stopped", error or "录音已停止，正在同步剩余分段。")

    def _track(self, track: str, device: dict) -> None:
        import pyaudiowpatch as audio

        try:
            with audio.PyAudio() as client:
                current = client.get_device_info_by_index(int(device["index"]))
                if current["name"] != device["name"]:
                    raise ValueError("音频设备已变化，请刷新后重新选择。")
                rate, channels = (
                    int(current["defaultSampleRate"]),
                    min(2, int(current["maxInputChannels"])),
                )
                if not 8000 <= rate <= 192000 or channels < 1:
                    raise ValueError("音频设备采样率或声道数不支持。")
                with client.open(
                    format=audio.paInt16,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=int(device["index"]),
                    frames_per_buffer=1024,
                    start=False,
                ) as stream:
                    self.barrier.wait(timeout=20)
                    stream.start_stream()
                    offset = time.monotonic() - self.started
                    if track == "playback":
                        self.ready.set()
                        self.notify(
                            "ready", "正在分别录制群聊播放和麦克风；停止后自动补传剩余音频。"
                        )
                    total, part = 0, 0
                    while (
                        not self.stopped.is_set() and time.monotonic() - self.started < self.maximum
                    ):
                        if (self.maximum - offset) * rate - total <= 0:
                            break
                        part += 1
                        filename = f"{track}-{part:04d}.wav"
                        temporary = self.folder / (filename + ".tmp")
                        count = 0
                        try:
                            with wave.open(str(temporary), "wb") as output:
                                output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                                while (
                                    not self.stopped.is_set()
                                    and count < self.segment * rate
                                    and time.monotonic() - self.started < self.maximum
                                ):
                                    frames = min(
                                        1024,
                                        self.segment * rate - count,
                                        int((self.maximum - offset) * rate) - total - count,
                                    )
                                    if frames <= 0:
                                        break
                                    block = stream.read(frames, exception_on_overflow=True)
                                    output.writeframes(mono_pcm(block, channels))
                                    count += len(block) // (2 * channels)
                        finally:
                            if count:
                                temporary.replace(self.folder / filename)
                                self.spool.add_part(
                                    self.folder, track, part, filename, offset + total / rate
                                )
                                self.notify(
                                    "part",
                                    f"已保存{'麦克风' if track == 'microphone' else '群聊播放'}分段 {part}。",
                                )
                            else:
                                temporary.unlink(missing_ok=True)
                        total += count
                    stream.stop_stream()
        except Exception as error:
            self.errors.append(f"{track}: {error}")
            self.notify("error", str(error))
            self.barrier.abort()
            self.stopped.set()

"""Windows WASAPI capture process. It never starts or accepts a QQ call."""

import argparse
import json
import sys
import threading
import wave
from pathlib import Path


def emit(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


def run(args) -> None:
    import pyaudiowpatch as audio

    with audio.PyAudio() as client:
        devices = list(client.get_loopback_device_info_generator())
        if args.action == "devices":
            emit(
                {
                    "devices": [
                        {
                            "index": row["index"],
                            "name": row["name"],
                            "rate": row["defaultSampleRate"],
                            "channels": row["maxInputChannels"],
                        }
                        for row in devices
                    ]
                }
            )
            return
        matches = [row for row in devices if row["index"] == args.device]
        if len(matches) != 1:
            raise ValueError("所选回环设备已变化，请刷新设备列表后重新选择。")
        device = matches[0]
        rate, channels = int(device["defaultSampleRate"]), int(device["maxInputChannels"])
        destination = Path(args.output).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        stopped = threading.Event()

        def read_control():
            # EOF means the controlling plugin has gone away: never keep recording.
            for line in sys.stdin:
                if line.strip() == "STOP":
                    break
            stopped.set()

        threading.Thread(target=read_control, daemon=True).start()
        total, part = 0, 0
        maximum = int(args.maximum * rate)
        segment_frames = int(args.segment * rate)
        with client.open(
            format=audio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=1024,
        ) as stream:
            emit({"event": "ready", "device": device["name"], "rate": rate})
            while not stopped.is_set() and total < maximum:
                part += 1
                path = destination / f"part-{part:04d}.wav"
                count, offset = 0, total / rate
                try:
                    with wave.open(str(path), "wb") as output:
                        output.setnchannels(channels)
                        output.setsampwidth(2)
                        output.setframerate(rate)
                        while not stopped.is_set() and total < maximum and count < segment_frames:
                            frames = min(1024, maximum - total, segment_frames - count)
                            block = stream.read(frames, exception_on_overflow=True)
                            output.writeframesraw(block)
                            actual = len(block) // (2 * channels)
                            count += actual
                            total += actual
                finally:
                    # Publish the sealed partial chunk even if an audio device disconnects.
                    if count:
                        emit(
                            {
                                "event": "chunk",
                                "file": path.name,
                                "offset": offset,
                                "duration": count / rate,
                            }
                        )
        emit({"event": "stopped", "seconds": total / rate, "limit_reached": total >= maximum})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["devices", "record"])
    parser.add_argument("--device", type=int, default=-1)
    parser.add_argument("--output", default="")
    parser.add_argument("--segment", type=int, default=600)
    parser.add_argument("--maximum", type=int, default=7200)
    try:
        run(parser.parse_args())
    except Exception as error:
        emit({"event": "error", "error": str(error)})
        raise SystemExit(1) from error

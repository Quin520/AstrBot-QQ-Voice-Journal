"""Offline speech worker, isolated from AstrBot and all other plugins."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from audio_windows import review_windows


def process(request: dict) -> dict:
    """Decode bounded audio, diarize it, and export reviewable voice samples."""
    threads = max(1, min(8, int(request.get("threads", 2))))
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[name] = str(threads)
    os.environ["MODELSCOPE_CACHE"] = request["cache"]
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import imageio_ffmpeg
    import numpy as np
    import soundfile as sf
    import torch
    from funasr import AutoModel

    torch.set_num_threads(threads)
    device = request.get("device", "cpu")
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA 不可用，请选择 CPU 或安装兼容的独立语音环境。")
    root = Path(request["root"]).resolve()
    source = Path(request["input"]).resolve()
    output = Path(request["output"]).resolve()
    if not source.is_relative_to(root) or not output.is_relative_to(root):
        raise ValueError("Audio paths must be inside this plugin's data directory")
    output.mkdir(parents=True, exist_ok=True)
    normalized = output / "normalized.wav"
    maximum = int(request.get("maximum_seconds", 7200))
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    decoded = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "wav,mp3,flac,ogg,mov,matroska,aac",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-t",
            str(maximum + 1),
            "-y",
            str(normalized),
        ],
        capture_output=True,
        timeout=180,
        creationflags=flags,
        check=False,
    )
    if decoded.returncode:
        raise ValueError("音频解码失败：" + decoded.stderr.decode("utf-8", errors="replace")[-600:])
    samples, rate = sf.read(normalized, dtype="float32")
    duration = len(samples) / rate
    if duration > maximum:
        raise ValueError("音频超过本次时长上限，请分段导入。")
    if duration < 1:
        raise ValueError("音频太短，请至少录制 1 秒。")
    # Use built-in architectures; no remote model Python is executed.
    common = {
        "device": device,
        "ncpu": threads,
        "disable_update": True,
        "disable_pbar": True,
        "trust_remote_code": False,
    }
    model = AutoModel(
        model="iic/SenseVoiceSmall",
        vad_model="fsmn-vad",
        punc_model="ct-punc",
        spk_model="cam++",
        spk_mode="vad_segment",
        **common,
    )
    # Dynamic silence can merge several turns into one VAD span. Speaker labels
    # are assigned per span, so preserve the measured pauses between speakers.
    results = model.generate(
        input=samples,
        fs=rate,
        batch_size_s=60,
        max_end_silence_time=800,
        dynamic_silence=False,
        merge_vad=False,
    )
    raw = results[0] if results else {}
    segments = []
    import re

    for row in raw.get("sentence_info", []):
        text = re.sub(r"<\|[^|]*\|>", "", row.get("text", row.get("sentence", ""))).strip()
        if not text:
            continue
        if "spk" not in row:
            raise RuntimeError("识别模型没有返回说话人编号，不能生成可靠的审核分组。")
        start = max(0.0, float(row["start"]) / 1000)
        end = min(duration, float(row["end"]) / 1000)
        if end > start:
            segments.append({"speaker": str(row["spk"]), "start": start, "end": end, "text": text})
    if not segments and re.sub(r"<\|[^|]*\|>", "", raw.get("text", "")).strip():
        raise RuntimeError("已识别到文字，但缺少分段说话人结果。")
    # A separate public embedding entry point avoids depending on internal ASR state.
    voice = AutoModel(model="cam++", **common) if segments else None
    import funasr

    model_id = "cam++/" + str(getattr(funasr, "__version__", "unknown"))
    if voice:
        weight = (
            Path(voice.kwargs["model_path"]) / "campplus_cn_common.bin"
            if "model_path" in voice.kwargs
            else None
        )
        if weight and weight.is_file():
            model_id += "/" + hashlib.sha256(weight.read_bytes()).hexdigest()[:16]
    clusters = []
    for label in sorted({row["speaker"] for row in segments}):
        rows = [row for row in segments if row["speaker"] == label]
        selected = review_windows(rows)
        clips, vectors, seconds = [], [], 0.0
        for index, (start, end) in enumerate(selected):
            clip = samples[int(start * rate) : int(end * rate)]
            path = output / f"speaker-{label}-{index}.wav"
            sf.write(path, clip, rate, subtype="PCM_16")
            clips.append(str(path.relative_to(root)))
            if end - start >= 1.5:
                embedding = voice.generate(input=clip, fs=rate)[0]["spk_embedding"]
                vector = embedding.detach().cpu().numpy().reshape(-1)
                vector = vector / max(float(np.linalg.norm(vector)), 1e-8)
                vectors.append(vector)
                seconds += end - start
        eligible, warning, center = (
            False,
            "有效独立人声不足 5 秒；可标注身份，但不会加入声纹库。",
            [],
        )
        if vectors:
            mean = np.mean(vectors, axis=0)
            mean = mean / max(float(np.linalg.norm(mean)), 1e-8)
            center = mean.tolist()
            consistency = min(float(np.dot(mean, vector)) for vector in vectors)
            eligible = seconds >= 5 and consistency >= 0.65
            if seconds >= 5 and consistency < 0.65:
                warning = "同组片段声纹差异较大，可能混有多人；不会加入声纹库。"
            elif eligible:
                warning = "请试听确认：聚类无法保证排除抢话、背景声或多人混合。"
        clusters.append(
            {
                "label": label,
                "embedding": center,
                "seconds": seconds,
                "samples": clips,
                "eligible": eligible,
                "warning": warning,
            }
        )
    return {"duration": duration, "model": model_id, "segments": segments, "clusters": clusters}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    args = parser.parse_args()
    request_path = Path(args.request)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    destination = Path(request["result"])
    try:
        result = {"ok": True, "result": process(request)}
    except Exception as error:  # noqa: BLE001 -- report all worker failures to the controller
        import traceback

        traceback.print_exc()
        result = {"ok": False, "error": str(error)}
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)
    raise SystemExit(0 if result["ok"] else 1)

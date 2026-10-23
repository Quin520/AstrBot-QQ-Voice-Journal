"""Durable client queue. Acknowledgements are persisted; audio stays local until deleted."""

import json
import threading
import time
import uuid
from pathlib import Path


class Spool:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def read(self, folder: Path) -> dict:
        with self.lock:
            return json.loads((folder / "session.private.json").read_text(encoding="utf-8"))

    def update(self, folder: Path, **changes) -> dict:
        with self.lock:
            data = self.read(folder)
            data.update(changes)
            self._write(folder, data)
            return data

    def _write(self, folder: Path, data: dict) -> None:
        path = folder / "session.private.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def create(self, profile: dict) -> Path:
        client_id = uuid.uuid4().hex
        folder = self.root / client_id
        folder.mkdir()
        self._write(
            folder,
            {
                "client_id": client_id,
                "title": time.strftime("B 端群语音 %m-%d %H:%M"),
                "fingerprint": profile["fingerprint"],
                "created": time.time(),
                "complete": False,
                "synced": False,
                "parts": [],
                "error": "",
            },
        )
        return folder

    def add_part(self, folder: Path, track: str, part: int, filename: str, offset: float) -> None:
        with self.lock:
            data = self.read(folder)
            data["parts"].append(
                {
                    "track": track,
                    "part": part,
                    "file": filename,
                    "offset": offset,
                    "uploaded": False,
                }
            )
            self._write(folder, data)

    def recover(self) -> int:
        count = 0
        for folder in self.pending():
            data = self.read(folder)
            if not data["complete"]:
                self.update(
                    folder,
                    complete=True,
                    error="B 端上次意外退出；已封口的分段已保留，未封口文件留在本地。",
                )
                count += 1
        return count

    def pending(self) -> list[Path]:
        return [
            path.parent
            for path in sorted(self.root.glob("*/session.private.json"))
            if not self.read(path.parent)["synced"]
        ]

    def sync(self, connection) -> dict:
        uploaded, completed, other_server = 0, 0, 0
        errors = []
        for folder in self.pending():
            data = self.read(folder)
            if data["fingerprint"] != connection.profile["fingerprint"]:
                other_server += 1
                continue
            try:
                sent, finished = self.sync_recording(connection, folder, data)
                uploaded += sent
                completed += int(finished)
            except Exception as error:
                errors.append({"recording": data["title"], "error": str(error)})
        return {
            "uploaded": uploaded,
            "completed": completed,
            "pending": len(self.pending()),
            "other_server": other_server,
            "errors": errors,
        }

    def sync_recording(self, connection, folder: Path, data: dict) -> tuple[int, bool]:
        uploaded = 0
        remote = connection.request(
            "POST", "/v1/sessions", {"client_id": data["client_id"], "title": data["title"]}
        )
        base = "/v1/sessions/" + data["client_id"]
        for item in data["parts"]:
            if item["uploaded"]:
                continue
            audio = folder / item["file"]
            if audio.resolve().parent != folder.resolve():
                raise ValueError("Invalid local audio path")
            if item["offset"] >= remote["maximum_seconds"]:
                raise ValueError("A 的录音时长限制已变化，请在 A 检查配置；本地音频已保留。")
            response = connection.request(
                "POST",
                f"{base}/parts/{item['track']}/{item['part']}?offset={item['offset']:.6f}",
                audio=audio,
            )
            if not response.get("chunk_id"):
                raise ValueError("A 未确认收到分段，稍后重试。")
            with self.lock:
                latest = self.read(folder)
                for saved in latest["parts"]:
                    if saved["file"] == item["file"]:
                        saved["uploaded"] = True
                self._write(folder, latest)
            uploaded += 1
        latest = self.read(folder)
        if latest["complete"] and all(item["uploaded"] for item in latest["parts"]):
            connection.request(
                "POST",
                base + "/finish",
                {
                    "parts": [
                        {"track": item["track"], "part": item["part"]} for item in latest["parts"]
                    ],
                    "error": latest["error"],
                },
            )
            self.update(folder, synced=True)
            return uploaded, True
        return uploaded, False

"""Transactional receipts for resumable, separate-track client uploads."""

import math
import re
import time
import uuid
from pathlib import Path


class RemoteStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS remote_sessions (
                    client_id TEXT PRIMARY KEY, session_id TEXT UNIQUE NOT NULL
                    REFERENCES sessions(id) ON DELETE CASCADE,
                    segment_seconds INTEGER NOT NULL, maximum_seconds INTEGER NOT NULL,
                    finished INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS remote_parts (
                    client_id TEXT NOT NULL REFERENCES remote_sessions(client_id) ON DELETE CASCADE,
                    track TEXT NOT NULL, part INTEGER NOT NULL, digest TEXT NOT NULL,
                    offset REAL NOT NULL, duration REAL NOT NULL,
                    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                    PRIMARY KEY(client_id, track, part)
                );
                CREATE TABLE IF NOT EXISTS remote_closed (client_id TEXT PRIMARY KEY);
            """)

    def create(self, client_id: str, group: str, title: str, segment: int, maximum: int) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", client_id):
            raise ValueError("Invalid client recording ID")
        if not re.fullmatch(r"[0-9]{5,20}", group):
            raise ValueError("请在 A 配置目标群号。")
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM remote_closed WHERE client_id=?", (client_id,)).fetchone():
                raise ValueError("A 已结束或删除这条录音，本地副本保留，不再上传。")
            existing = db.execute(
                "SELECT * FROM remote_sessions WHERE client_id=?", (client_id,)
            ).fetchone()
            if existing:
                return dict(existing)
            session_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO sessions(id,group_id,title,created,status) VALUES(?,?,?,?,?)",
                (session_id, group, title[:100], time.time(), "receiving"),
            )
            db.execute(
                "INSERT INTO remote_sessions VALUES(?,?,?,?,0)",
                (client_id, session_id, segment, maximum),
            )
        (self.store.directory / session_id).mkdir(exist_ok=True)
        return self.get(client_id)

    def close(self, session_id: str) -> None:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT client_id FROM remote_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return
            db.execute("INSERT OR IGNORE INTO remote_closed VALUES(?)", (row[0],))
            db.execute("UPDATE remote_sessions SET finished=1 WHERE session_id=?", (session_id,))
            db.execute(
                "UPDATE sessions SET status='stopped',error='A 已手动结束接收；B 未传完的录音仍保存在 B。' WHERE id=?",
                (session_id,),
            )

    def get(self, client_id: str) -> dict:
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM remote_closed WHERE client_id=?", (client_id,)).fetchone():
                raise ValueError("A 已结束或删除这条录音，本地副本保留，不再上传。")
            row = db.execute(
                "SELECT * FROM remote_sessions WHERE client_id=?", (client_id,)
            ).fetchone()
        if row is None:
            raise ValueError("请先建立远程录音记录。")
        return dict(row)

    def accept(
        self,
        client_id: str,
        track: str,
        part: int,
        path: Path,
        digest: str,
        offset: float,
        duration: float,
    ) -> tuple[str, bool]:
        """Commit the audio receipt and chunk together; retries never duplicate learning data."""
        if track not in {"playback", "microphone"} or not 1 <= part <= 240:
            raise ValueError("Invalid audio track or part")
        record = self.get(client_id)
        if (
            not math.isfinite(offset)
            or offset < 0
            or not 0 < duration <= record["segment_seconds"] + 1
        ):
            raise ValueError("音频时间范围无效。")
        if offset + duration > record["maximum_seconds"] + 2:
            raise ValueError("录音超过 A 允许的时长。")
        with self.store.connect() as db:
            prior = db.execute(
                "SELECT * FROM remote_parts WHERE client_id=? AND track=? AND part=?",
                (client_id, track, part),
            ).fetchone()
            if prior:
                if prior["digest"] != digest or abs(prior["offset"] - offset) > 0.001:
                    raise ValueError("同一分段已上传不同内容，已拒绝覆盖。")
                return prior["chunk_id"], False
            if record["finished"]:
                raise ValueError("此录音已完成，不再接受新分段。")
            overlap = db.execute(
                "SELECT 1 FROM remote_parts WHERE client_id=? AND track=? AND offset<? AND offset+duration>?",
                (client_id, track, offset + duration - 0.01, offset + 0.01),
            ).fetchone()
            if overlap:
                raise ValueError("同一音轨的分段时间重叠。")
            destination = (
                self.store.directory / record["session_id"] / f"remote-{track}-{part:04d}.wav"
            )
            path.replace(destination)
            chunk_id = self.store.chunk(
                record["session_id"],
                destination,
                offset,
                "B 麦克风" if track == "microphone" else "B 群聊播放",
                db,
            )
            db.execute(
                "INSERT INTO remote_parts VALUES(?,?,?,?,?,?,?)",
                (client_id, track, part, digest, offset, duration, chunk_id),
            )
        return chunk_id, True

    def finish(self, client_id: str, parts: list, error: str = "") -> str:
        record = self.get(client_id)
        expected = {(str(item["track"]), int(item["part"])) for item in parts}
        if len(expected) != len(parts):
            raise ValueError("分段清单重复。")
        with self.store.connect() as db:
            actual = {
                (row[0], row[1])
                for row in db.execute(
                    "SELECT track,part FROM remote_parts WHERE client_id=?", (client_id,)
                )
            }
            if actual != expected:
                raise ValueError("还有分段未上传，录音暂未结束。")
            db.execute("UPDATE remote_sessions SET finished=1 WHERE client_id=?", (client_id,))
            db.execute(
                "UPDATE sessions SET status=?,error=? WHERE id=?",
                ("interrupted" if error else "stopped", error[:1000], record["session_id"]),
            )
        return record["session_id"]

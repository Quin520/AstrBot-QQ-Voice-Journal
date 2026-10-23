"""Own SQLite storage for sessions, reviewed identities, and revocable exemplars."""

import json
import re
import sqlite3
import time
import uuid
import wave
from contextlib import contextmanager
from pathlib import Path

from .matching import candidates, unit_vector


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "journal.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, title TEXT NOT NULL,
                    created REAL NOT NULL, status TEXT NOT NULL, error TEXT DEFAULT '',
                    revision INTEGER DEFAULT 0, summary TEXT DEFAULT '',
                    summary_revision INTEGER DEFAULT -1, summary_error TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    offset REAL NOT NULL, duration REAL DEFAULT 0, status TEXT NOT NULL,
                    error TEXT DEFAULT '', audio TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clusters (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    chunk_id TEXT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                    label TEXT NOT NULL, model TEXT NOT NULL, embedding TEXT NOT NULL,
                    seconds REAL NOT NULL, samples TEXT NOT NULL, eligible INTEGER NOT NULL,
                    warning TEXT DEFAULT '', status TEXT DEFAULT 'pending',
                    qq TEXT DEFAULT '', nickname TEXT DEFAULT '', updated REAL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS segments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    cluster_id TEXT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
                    start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, cluster_id TEXT NOT NULL,
                    at REAL NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cluster_session ON clusters(session_id);
                CREATE INDEX IF NOT EXISTS segment_session ON segments(session_id, start);
            """)

    @contextmanager
    def connect(self):
        """Open one transaction; rollback on all exceptional exits."""
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self) -> None:
        """Expose interrupted work after an unclean stop instead of losing it."""
        with self.connect() as db:
            db.execute(
                "UPDATE sessions SET status='interrupted', error='进程已重启；已完成的分段仍可审核。' WHERE status='recording'"
            )
            db.execute(
                "UPDATE chunks SET status='error', error='处理被重启中断，请重试。' WHERE status IN ('queued','processing')"
            )
            interrupted = [
                row[0] for row in db.execute("SELECT id FROM sessions WHERE status='interrupted'")
            ]
        for session_id in interrupted:
            with self.connect() as db:
                known = {
                    row[0]
                    for row in db.execute(
                        "SELECT audio FROM chunks WHERE session_id=?", (session_id,)
                    )
                }
            offset = 0.0
            for path in sorted((self.directory / session_id).glob("part-*.wav")):
                try:
                    with wave.open(str(path), "rb") as audio:
                        duration = audio.getnframes() / audio.getframerate()
                except (wave.Error, EOFError) as error:
                    self.set_session(
                        session_id, "interrupted", f"部分录音未正常封口，需手动检查：{error}"
                    )
                    continue
                relative = str(path.resolve().relative_to(self.directory.resolve()))
                if relative not in known and duration > 0:
                    chunk_id = self.chunk(session_id, path, offset)
                    self.set_chunk(chunk_id, "error", "已恢复中断时落盘的录音，请点击重试。")
                offset += duration

    def create(self, group_id: str, title: str, status: str = "imported") -> str:
        if not re.fullmatch(r"[0-9]{5,20}", group_id):
            raise ValueError("请先配置目标群号。")
        session_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO sessions(id,group_id,title,created,status) VALUES(?,?,?,?,?)",
                (session_id, group_id, title[:100], time.time(), status),
            )
        (self.directory / session_id).mkdir()
        return session_id

    def set_session(self, session_id: str, status: str, error: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE sessions SET status=?, error=? WHERE id=?",
                (status, error[:1500], session_id),
            )

    def chunk(self, session_id: str, path: Path, offset: float) -> str:
        chunk_id = uuid.uuid4().hex
        relative = str(path.resolve().relative_to(self.directory.resolve()))
        with self.connect() as db:
            db.execute(
                "INSERT INTO chunks(id,session_id,offset,status,audio) VALUES(?,?,?,'queued',?)",
                (chunk_id, session_id, offset, relative),
            )
        return chunk_id

    def get_chunk(self, chunk_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            if row is None:
                raise ValueError("音频分段不存在。")
            return dict(row)

    def set_chunk(self, chunk_id: str, status: str, error: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE chunks SET status=?,error=? WHERE id=?", (status, error[:1500], chunk_id)
            )

    def add_result(self, chunk_id: str, result: dict) -> None:
        """Commit all segments and pending groups together, exactly once."""
        chunk = self.get_chunk(chunk_id)
        with self.connect() as db:
            if db.execute("SELECT 1 FROM clusters WHERE chunk_id=?", (chunk_id,)).fetchone():
                raise ValueError("分段已有转写结果，禁止覆盖已审核声纹。")
            mapping = {}
            for group in result["clusters"]:
                cluster_id = uuid.uuid4().hex
                mapping[str(group["label"])] = cluster_id
                embedding = unit_vector(group["embedding"]) if group["embedding"] else []
                db.execute(
                    "INSERT INTO clusters(id,session_id,chunk_id,label,model,embedding,seconds,samples,eligible,warning) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        cluster_id,
                        chunk["session_id"],
                        chunk_id,
                        str(group["label"]),
                        result["model"],
                        json.dumps(embedding),
                        float(group["seconds"]),
                        json.dumps(group["samples"]),
                        int(bool(group["eligible"] and embedding)),
                        group.get("warning", ""),
                    ),
                )
            for segment in result["segments"]:
                db.execute(
                    "INSERT INTO segments(session_id,cluster_id,start,end,text) VALUES(?,?,?,?,?)",
                    (
                        chunk["session_id"],
                        mapping[str(segment["speaker"])],
                        chunk["offset"] + segment["start"],
                        chunk["offset"] + segment["end"],
                        segment["text"],
                    ),
                )
            db.execute(
                "UPDATE chunks SET status='done',duration=?,error='' WHERE id=?",
                (result["duration"], chunk_id),
            )
            db.execute("UPDATE sessions SET revision=revision+1 WHERE id=?", (chunk["session_id"],))

    def examples(self, group_id: str, exclude_session: str = "") -> list[dict]:
        """Use only human approvals, separated by target group and model version."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT c.* FROM clusters c JOIN sessions s ON s.id=c.session_id WHERE s.group_id=? AND s.id<>? AND c.status='approved' AND c.eligible=1 ORDER BY c.updated DESC",
                (group_id, exclude_session),
            ).fetchall()
        output, counts = [], {}
        for row in rows:
            key = (row["qq"], row["model"])
            counts[key] = counts.get(key, 0) + 1
            if counts[key] <= 20:
                output.append({**dict(row), "embedding": json.loads(row["embedding"])})
        return output

    def review(self, cluster_id: str, action: str, qq: str = "", nickname: str = "") -> None:
        """Approve, correct, reject mixed speech, or revoke an identity atomically."""
        if action not in {"approve", "reject", "revoke"}:
            raise ValueError("未知审核操作。")
        if action == "approve" and (
            not re.fullmatch(r"[0-9]{5,20}", qq) or not nickname.strip() or len(nickname) > 80
        ):
            raise ValueError("请填写有效 QQ 号和昵称。")
        after = {
            "status": {"approve": "approved", "reject": "rejected", "revoke": "pending"}[action],
            "qq": qq if action == "approve" else "",
            "nickname": nickname.strip() if action == "approve" else "",
        }
        with self.connect() as db:
            row = db.execute("SELECT * FROM clusters WHERE id=?", (cluster_id,)).fetchone()
            if row is None:
                raise ValueError("声音分组不存在。")
            before = {key: row[key] for key in after}
            db.execute(
                "UPDATE clusters SET status=?,qq=?,nickname=?,updated=? WHERE id=?",
                (after["status"], after["qq"], after["nickname"], time.time(), cluster_id),
            )
            db.execute(
                "INSERT INTO reviews(cluster_id,at,before_json,after_json) VALUES(?,?,?,?)",
                (
                    cluster_id,
                    time.time(),
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                ),
            )
            db.execute("UPDATE sessions SET revision=revision+1 WHERE id=?", (row["session_id"],))

    def sessions(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT s.*, (SELECT count(*) FROM clusters c WHERE c.session_id=s.id AND c.status='pending') AS pending FROM sessions s ORDER BY created DESC LIMIT 200"
                )
            ]

    def people(self, group_id: str) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT c.qq, c.nickname, count(*) AS examples FROM clusters c JOIN sessions s ON s.id=c.session_id WHERE s.group_id=? AND c.status='approved' AND c.eligible=1 GROUP BY c.qq ORDER BY max(c.updated) DESC",
                    (group_id,),
                )
            ]

    def detail(self, session_id: str, threshold: float = 0.72, margin: float = 0.1) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise ValueError("记录不存在。")
            session = dict(row)
            groups = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM clusters WHERE session_id=? ORDER BY rowid", (session_id,)
                )
            ]
            session["chunks"] = [
                dict(row)
                for row in db.execute(
                    "SELECT id,offset,duration,status,error FROM chunks WHERE session_id=? ORDER BY offset",
                    (session_id,),
                )
            ]
            session["segments"] = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM segments WHERE session_id=? ORDER BY start", (session_id,)
                )
            ]
        examples = self.examples(session["group_id"], session_id)
        for group in groups:
            vector = json.loads(group.pop("embedding"))
            group["samples"] = json.loads(group["samples"])
            group["candidates"] = (
                candidates(vector, examples, group["model"], threshold, margin)
                if vector
                else {"ranked": [], "suggested": False, "margin": 0}
            )
        session["clusters"] = groups
        return session

    def summary_input(self, session_id: str) -> tuple[int, str]:
        session = self.detail(session_id)
        groups = {row["id"]: row for row in session["clusters"]}
        labels = {key: f"声音组 {index + 1}" for index, key in enumerate(groups)}
        lines = []
        for segment in session["segments"]:
            group = groups[segment["cluster_id"]]
            name = (
                f"{group['nickname']}（QQ {group['qq']}，已审核）"
                if group["status"] == "approved"
                else f"{labels[group['id']]}（身份未确认）"
            )
            if group["status"] == "rejected":
                name += "（混合/不可靠分组，不归因到个人）"
            lines.append(
                f"[{segment['start']:.1f}s–{segment['end']:.1f}s] {name}: {segment['text']}"
            )
        return session["revision"], "\n".join(lines)

    def save_summary(self, session_id: str, revision: int, summary: str, error: str = "") -> bool:
        """Refuse stale model output when identities changed during generation."""
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE sessions SET summary=?,summary_revision=?,summary_error=? WHERE id=? AND revision=?",
                (summary, revision, error[:1500], session_id, revision),
            )
            return bool(cursor.rowcount)

    def sample(self, cluster_id: str, index: int) -> Path:
        with self.connect() as db:
            row = db.execute("SELECT samples FROM clusters WHERE id=?", (cluster_id,)).fetchone()
        if row is None:
            raise ValueError("试听片段不存在。")
        samples = json.loads(row["samples"])
        if not 0 <= index < len(samples):
            raise ValueError("试听片段不存在。")
        path = (self.directory / samples[index]).resolve()
        if not path.is_relative_to(self.directory.resolve()) or not path.is_file():
            raise ValueError("试听文件不存在。")
        return path

    def forget(self, group_id: str, qq: str) -> int:
        """Revoke all examples for one person and invalidate affected summaries."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT c.id FROM clusters c JOIN sessions s ON s.id=c.session_id WHERE s.group_id=? AND c.qq=?",
                (group_id, qq),
            ).fetchall()
        for row in rows:
            self.review(row["id"], "revoke")
        return len(rows)

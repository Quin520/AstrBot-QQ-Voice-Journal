"""Subprocess adapter for offline inference and user-triggered audio capture."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path


class Engine:
    def __init__(self, config: dict, root: Path):
        self.config, self.root = config, root
        self.process = None
        self.closed = False

    def executable(self) -> str:
        executable = Path(str(self.config.get("engine_python", "")))
        if not executable.is_file():
            raise ValueError("请配置独立语音环境的 Python 路径。")
        return str(executable)

    @staticmethod
    def flags() -> dict:
        return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

    async def devices(self) -> list[dict]:
        process = await asyncio.create_subprocess_exec(
            self.executable(),
            "-X",
            "utf8",
            str(Path(__file__).with_name("capture_worker.py")),
            "devices",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **self.flags(),
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), 20)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise RuntimeError((out or err).decode("utf-8", errors="replace")[-1000:])
        return json.loads(out)["devices"]

    async def analyze(self, chunk: dict) -> dict:
        if self.closed:
            raise RuntimeError("语音引擎已停止。")
        folder = self.root / chunk["session_id"] / chunk["id"]
        folder.mkdir(parents=True, exist_ok=True)
        result_path, request_path = folder / "result.json", folder / "request.json"
        result_path.unlink(missing_ok=True)
        request = {
            "input": str(self.root / chunk["audio"]),
            "output": str(folder),
            "root": str(self.root),
            "cache": str(self.config.get("engine_cache") or self.root / "models"),
            "device": self.config.get("engine_device", "cpu"),
            "threads": self.config.get("cpu_threads", 2),
            "maximum_seconds": min(240, max(1, int(self.config.get("maximum_minutes", 120)))) * 60,
            "result": str(result_path),
        }
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        with (folder / "worker.log").open("w", encoding="utf-8") as log:
            self.process = await asyncio.create_subprocess_exec(
                self.executable(),
                "-X",
                "utf8",
                str(Path(__file__).with_name("worker.py")),
                str(request_path),
                stdout=log,
                stderr=log,
                **self.flags(),
            )
            try:
                await asyncio.wait_for(self.process.wait(), 3600)
            except BaseException:
                if self.process.returncode is None:
                    self.process.kill()
                    await self.process.wait()
                raise
            finally:
                self.process = None
        if not result_path.exists():
            raise RuntimeError("语音处理进程未输出结果，请查看此分段的 worker.log。")
        response = json.loads(result_path.read_text(encoding="utf-8"))
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "语音处理失败。"))
        return response["result"]

    async def close(self) -> None:
        self.closed = True
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()

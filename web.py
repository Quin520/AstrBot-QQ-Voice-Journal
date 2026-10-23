"""Loopback-only review UI with bearer authentication and explicit capture consent."""

import json
import logging
import secrets
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)


class WebPanel:
    def __init__(self, service, directory: Path, port: int, remote=None):
        self.service, self.directory, self.port = service, directory, port
        self.remote = remote
        self.runner = None
        token_path = directory / "panel-token.txt"
        if token_path.exists():
            self.token = token_path.read_text(encoding="utf-8").strip()
        else:
            self.token = secrets.token_urlsafe(32)
            token_path.write_text(self.token, encoding="utf-8")
        if len(self.token) < 32:
            raise ValueError("Panel token is invalid")
        self.app = web.Application(client_max_size=256 * 1024 * 1024, middlewares=[self.guard])
        self.app.add_routes(
            [
                web.get("/", self.static),
                web.get("/app.js", self.static),
                web.get("/style.css", self.static),
                web.get("/api/status", self.status),
                web.post("/api/remote/profile", self.remote_profile),
                web.post("/api/remote/revoke", self.remote_revoke),
                web.post("/api/remote/end/{id}", self.remote_end),
                web.get("/api/devices", self.devices),
                web.get("/api/members", self.members),
                web.get("/api/sessions", self.sessions),
                web.get("/api/voices", self.voices),
                web.get("/api/session/{id}", self.detail),
                web.post("/api/capture/start", self.capture),
                web.post("/api/capture/stop", self.stop),
                web.post("/api/import", self.upload),
                web.post("/api/review/{id}", self.review),
                web.get("/api/sample/{id}/{index}", self.sample),
                web.post("/api/summary/{id}", self.summary),
                web.post("/api/retry/{id}", self.retry),
                web.post("/api/delete/{id}", self.delete),
                web.post("/api/forget", self.forget),
            ]
        )

    @web.middleware
    async def guard(self, request, handler):
        host = request.host.split(":")[0]
        if host not in {"localhost", "127.0.0.1"}:
            raise web.HTTPForbidden()
        origin = request.headers.get("Origin")
        if origin and origin not in {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        }:
            raise web.HTTPForbidden()
        if request.path.startswith("/api/"):
            header = request.headers.get("Authorization", "")
            if not secrets.compare_digest(header, "Bearer " + self.token):
                return web.json_response(
                    {"error": "请使用管理入口或输入本机管理口令。"}, status=401
                )
        try:
            response = await handler(request)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            response = web.json_response({"error": str(error)}, status=400)
        except web.HTTPException:
            raise
        except Exception as error:
            logger.exception("Voice journal panel operation failed")
            response = web.json_response({"error": str(error)}, status=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    async def static(self, request):
        name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[request.path]
        return web.FileResponse(Path(__file__).parent / "ui" / name)

    async def status(self, request):
        return web.json_response(
            {
                **self.service.status(),
                "remote": self.remote.status() if self.remote else {"enabled": False},
            }
        )

    async def remote_profile(self, request):
        if not self.remote:
            raise ValueError("B 端接收未启用。")
        data = await request.json()
        return web.json_response(self.remote.profile(str(data["address"])))

    async def remote_revoke(self, request):
        if not self.remote or not self.remote.identity:
            raise ValueError("B 端接收未启用。")
        self.remote.identity.rotate()
        return web.json_response({"ok": True})

    async def remote_end(self, request):
        if not self.remote:
            raise ValueError("B 端接收未配置。")
        self.remote.end_receiving(request.match_info["id"])
        return web.json_response({"ok": True})

    async def devices(self, request):
        return web.json_response({"devices": await self.service.engine.devices()})

    async def members(self, request):
        return web.json_response({"members": await self.service.members_backend()})

    async def sessions(self, request):
        return web.json_response({"sessions": self.service.store.sessions()})

    async def voices(self, request):
        return web.json_response(
            {"people": self.service.store.people(str(self.service.config.get("group_id", "")))}
        )

    async def detail(self, request):
        config = self.service.config
        return web.json_response(
            self.service.store.detail(
                request.match_info["id"],
                float(config.get("match_threshold", 0.72)),
                float(config.get("match_margin", 0.1)),
            )
        )

    async def capture(self, request):
        data = await request.json()
        session_id = await self.service.start_capture(
            int(data["device"]), data.get("confirmed") is True
        )
        return web.json_response({"session_id": session_id})

    async def stop(self, request):
        await self.service.stop_capture()
        return web.json_response({"ok": True})

    async def upload(self, request):
        reader = await request.multipart()
        field = await reader.next()
        if not field or field.name != "audio" or not field.filename:
            raise ValueError("请选择音频文件。")
        extension = Path(field.filename).suffix.lower()
        if extension not in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm"}:
            raise ValueError("支持 WAV、MP3、M4A、FLAC、OGG、AAC、WEBM。")
        title = Path(field.filename).name[:100]
        session_id = self.service.store.create(str(self.service.config.get("group_id", "")), title)
        path = self.directory / session_id / ("import" + extension)
        size = 0
        try:
            with path.open("wb") as output:
                while block := await field.read_chunk(64 * 1024):
                    size += len(block)
                    if size > 256 * 1024 * 1024:
                        raise ValueError("音频最大 256 MB。")
                    output.write(block)
            if size == 0:
                raise ValueError("音频文件为空。")
            chunk_id = self.service.store.chunk(session_id, path, 0)
            self.service.enqueue(chunk_id)
        except BaseException as error:
            self.service.store.set_session(session_id, "error", str(error))
            raise
        return web.json_response({"session_id": session_id})

    async def review(self, request):
        data = await request.json()
        self.service.store.review(
            request.match_info["id"],
            str(data["action"]),
            str(data.get("qq", "")),
            str(data.get("nickname", "")),
        )
        return web.json_response({"ok": True})

    async def sample(self, request):
        return web.FileResponse(
            self.service.store.sample(request.match_info["id"], int(request.match_info["index"]))
        )

    async def summary(self, request):
        self.service.request_summary(request.match_info["id"])
        return web.json_response({"ok": True})

    async def retry(self, request):
        self.service.enqueue(request.match_info["id"])
        return web.json_response({"ok": True})

    async def delete(self, request):
        session_id = request.match_info["id"]
        detail = self.service.store.detail(session_id)
        if detail["status"] == "receiving":
            raise ValueError("请先在 B 停止并完成同步，或点击结束 B 端接收。")
        if self.remote:
            self.remote.end_receiving(session_id)
        await self.service.delete(request.match_info["id"])
        return web.json_response({"ok": True})

    async def forget(self, request):
        data = await request.json()
        count = self.service.store.forget(
            str(self.service.config.get("group_id", "")), str(data["qq"])
        )
        return web.json_response({"count": count})

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        except BaseException:
            await self.runner.cleanup()
            self.runner = None
            raise

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

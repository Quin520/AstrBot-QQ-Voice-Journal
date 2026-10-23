"""TLS ingestion service, deliberately separate from the local administration panel."""

import asyncio
import hashlib
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
import wave
from urllib.parse import urlsplit

from aiohttp import web

from .remote_store import RemoteStore
from .remote_tls import RemoteIdentity

logger = logging.getLogger(__name__)
MAX_BYTES = 256 * 1024 * 1024
NETWORKS = [
    ipaddress.ip_network(value)
    for value in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fd7a:115c:a1e0::/48",
    )
]


class RemoteReceiver:
    def __init__(self, service):
        self.service, self.runner, self.identity = service, None, None
        self.receipts = RemoteStore(service.store)
        self.port = int(service.config.get("remote_port", 7845))
        self.lock = asyncio.Lock()
        self.last_upload = 0.0
        self.uploads = set()
        self.app = web.Application(client_max_size=MAX_BYTES, middlewares=[self.guard])
        self.app.add_routes(
            [
                web.get("/v1/info", self.info),
                web.post("/v1/sessions", self.create),
                web.post("/v1/sessions/{id}/parts/{track}/{part}", self.upload),
                web.post("/v1/sessions/{id}/finish", self.finish),
            ]
        )

    @web.middleware
    async def guard(self, request, handler):
        try:
            peer = ipaddress.ip_address(request.remote or "")
        except ValueError:
            raise web.HTTPForbidden()
        if not any(peer in network for network in NETWORKS) or request.headers.get("Origin"):
            raise web.HTTPForbidden()
        if (
            not request.secure
            or not self.identity
            or not secrets.compare_digest(
                request.headers.get("Authorization", ""), "Bearer " + self.identity.token
            )
        ):
            return web.json_response(
                {"error": "连接凭据无效，请从 A 重新导出连接文件。"}, status=401
            )
        if request.headers.get("Content-Encoding", "identity") != "identity":
            raise web.HTTPUnsupportedMediaType()
        try:
            result = await handler(request)
        except (
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            wave.Error,
            EOFError,
        ) as error:
            result = web.json_response({"error": str(error)}, status=400)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("Remote audio ingestion failed")
            result = web.json_response({"error": "A 接收失败，请查看 A 的插件日志。"}, status=500)
        result.headers["Cache-Control"] = "no-store"
        return result

    def limits(self) -> dict:
        return {
            "segment_seconds": max(
                1, min(10, int(self.service.config.get("checkpoint_minutes", 10)))
            )
            * 60,
            "maximum_seconds": max(
                1, min(240, int(self.service.config.get("maximum_minutes", 120)))
            )
            * 60,
        }

    def status(self) -> dict:
        addresses = sorted(
            {item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        )
        return {
            "enabled": bool(self.runner),
            "port": self.port,
            "addresses": addresses,
            "last_upload": self.last_upload,
            **self.limits(),
        }

    def profile(self, address: str) -> dict:
        if not self.runner or not self.identity:
            raise ValueError("请先启用插件配置中的 B 端接收，并重启 AstrBot。")
        url = urlsplit(address if "://" in address else "https://" + address)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("请输入 A 的 Tailscale 地址，例如 https://100.x.x.x:7845。")
        hostname = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
        return {
            "version": 1,
            "server": f"https://{hostname}:{url.port or self.port}",
            "fingerprint": self.identity.fingerprint,
            "token": self.identity.token,
            **self.limits(),
        }

    async def info(self, request):
        return web.json_response(
            {
                "version": 1,
                "group_id": str(self.service.config.get("group_id", "")),
                **self.limits(),
            }
        )

    async def create(self, request):
        if request.content_length is None or request.content_length > 4096:
            raise web.HTTPRequestEntityTooLarge(
                max_size=4096, actual_size=request.content_length or 0
            )
        data = await request.json()
        limits = self.limits()
        async with self.lock:
            record = self.receipts.create(
                str(data["client_id"]),
                str(self.service.config.get("group_id", "")),
                str(data.get("title", "B 端群语音")),
                limits["segment_seconds"],
                limits["maximum_seconds"],
            )
        return web.json_response(record)

    async def upload(self, request):
        if request.content_length is None or not 44 < request.content_length <= MAX_BYTES:
            raise ValueError("音频必须为不超过 256 MB 的 WAV。")
        client_id, track = request.match_info["id"], request.match_info["track"]
        part, offset = int(request.match_info["part"]), float(request.query["offset"])
        if track not in {"playback", "microphone"} or not 1 <= part <= 240:
            raise ValueError("Invalid track or part")
        record = self.receipts.get(client_id)
        if record["finished"]:
            # An acknowledged final manifest closes the recording. No file mutation follows.
            with self.service.store.connect() as db:
                prior = db.execute(
                    "SELECT chunk_id FROM remote_parts WHERE client_id=? AND track=? AND part=?",
                    (client_id, track, part),
                ).fetchone()
            if prior is None:
                raise ValueError("此录音已完成，不再接受新分段。")
        folder = self.service.store.directory / record["session_id"]
        path = folder / ("upload-" + uuid.uuid4().hex + ".tmp")
        size, digest = 0, hashlib.sha256()
        credential = self.identity.token
        self.uploads.add(path)
        try:
            with path.open("wb") as output:
                while block := await asyncio.wait_for(request.content.read(64 * 1024), 60):
                    size += len(block)
                    if size > MAX_BYTES:
                        raise ValueError("音频超过 256 MB。")
                    output.write(block)
                    digest.update(block)
            with wave.open(str(path), "rb") as audio:
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or not 8000 <= audio.getframerate() <= 192000
                ):
                    raise ValueError("仅接受单音轨 16 位 PCM WAV。")
                duration = audio.getnframes() / audio.getframerate()
                remaining = audio.getnframes() * 2
                while block := audio.readframes(32768):
                    remaining -= len(block)
                if remaining != 0:
                    raise ValueError("WAV 内容不完整，请重新上传。")
            async with self.lock:
                if credential != self.identity.token:
                    raise ValueError("连接口令已撤销，请重新配对。")
                chunk_id, created = self.receipts.accept(
                    client_id, track, part, path, digest.hexdigest(), offset, duration
                )
                chunk = self.service.store.get_chunk(chunk_id)
                if chunk_id not in self.service.queued and chunk["status"] != "done":
                    self.service.enqueue(chunk_id)
                self.last_upload = time.time()
            return web.json_response({"chunk_id": chunk_id, "duplicate": not created})
        finally:
            path.unlink(missing_ok=True)
            self.uploads.discard(path)

    def end_receiving(self, session_id: str) -> None:
        if any(path.parent.name == session_id for path in self.uploads):
            raise ValueError("B 正在上传分段，请稍后结束接收。")
        self.receipts.close(session_id)

    async def finish(self, request):
        if request.content_length is None or request.content_length > 64 * 1024:
            raise ValueError("结束清单过大。")
        data = await request.json()
        async with self.lock:
            session_id = self.receipts.finish(
                request.match_info["id"], data["parts"], str(data.get("error", ""))
            )
        return web.json_response({"session_id": session_id})

    async def start(self) -> None:
        if not self.service.config.get("remote_enabled", False):
            return
        self.identity = RemoteIdentity(self.service.store.directory)
        self.runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=10)
        await self.runner.setup()
        try:
            await web.TCPSite(
                self.runner,
                str(self.service.config.get("remote_bind", "0.0.0.0")),
                self.port,
                ssl_context=self.identity.context,
            ).start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

"""Pinned TLS transport. Credentials are sent only after checking A's certificate."""

import hashlib
import http.client
import json
import re
import secrets
import ssl
from pathlib import Path
from urllib.parse import urlsplit


class Connection:
    def __init__(self, profile: dict):
        self.profile = dict(profile)
        self.url = urlsplit(str(profile.get("server", "")))
        if (
            profile.get("version") != 1
            or self.url.scheme != "https"
            or not self.url.hostname
            or self.url.username
            or self.url.password
            or self.url.path not in {"", "/"}
            or self.url.query
            or self.url.fragment
        ):
            raise ValueError("连接文件格式无效，请在 A 重新导出。")
        if not re.fullmatch(r"[a-f0-9]{64}", str(profile.get("fingerprint", ""))):
            raise ValueError("连接文件缺少服务器指纹。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", str(profile.get("token", ""))):
            raise ValueError("连接文件缺少上传口令。")
        if (
            not 60 <= int(profile.get("segment_seconds", 600)) <= 600
            or not 60 <= int(profile.get("maximum_seconds", 7200)) <= 14400
        ):
            raise ValueError("连接文件的录音时长配置无效。")

    def request(self, method: str, path: str, data=None, audio: Path | None = None) -> dict:
        # The paired certificate pin replaces public-CA hostname trust. Never send
        # a token, JSON body or audio before this explicit peer identity check.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = http.client.HTTPSConnection(
            self.url.hostname, self.url.port or 7845, context=context, timeout=45
        )
        try:
            connection.connect()
            fingerprint = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if not secrets.compare_digest(fingerprint, self.profile["fingerprint"]):
                raise ValueError("A 的证书指纹不匹配；已停止发送，请重新核对连接文件。")
            headers = {"Authorization": "Bearer " + self.profile["token"]}
            if audio is not None:
                headers.update(
                    {"Content-Type": "audio/wav", "Content-Length": str(audio.stat().st_size)}
                )
                with audio.open("rb") as stream:
                    connection.request(method, path, body=stream, headers=headers)
            else:
                payload = json.dumps(data).encode("utf-8") if data is not None else None
                headers["Content-Type"] = "application/json"
                connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError("A 的响应过大。")
            try:
                value = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError(f"A 返回了非预期响应（HTTP {response.status}）。") from None
            if response.status != 200:
                raise ValueError(str(value.get("error", f"HTTP {response.status}")))
            return value
        finally:
            connection.close()

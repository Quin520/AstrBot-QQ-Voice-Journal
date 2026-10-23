import asyncio
import io
import socket
import wave

import pytest
from test_service_web import FakeEngine, members, summary
from voice_journal.client.connection import Connection
from voice_journal.client.spool import Spool
from voice_journal.remote import RemoteReceiver
from voice_journal.service import JournalService
from voice_journal.store import Store


def wav_bytes(seconds=1):
    result = io.BytesIO()
    with wave.open(result, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * int(16000 * seconds))
    return result.getvalue()


async def receiver_at(root, address=None):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    store = Store(root)
    service = JournalService(
        {
            "remote_enabled": True,
            "remote_bind": address(port) if address else "127.0.0.1",
            "remote_port": port,
            "group_id": "100001",
            "auto_summary": False,
        },
        store,
        FakeEngine(),
        summary,
        members,
    )
    await service.start()
    receiver = RemoteReceiver(service)
    await receiver.start()
    return service, receiver


@pytest.mark.parametrize(
    "address",
    [
        lambda port: f" https://127.0.0.1:{port} ",
        lambda port: f"127.0.0.1:{port}",
        lambda port: " 127.0.0.1 ",
    ],
)
def test_listener_accepts_pasted_url_or_ip_without_dns(tmp_path, address):
    async def run():
        service, receiver = await receiver_at(tmp_path, address)
        try:
            connection = Connection(receiver.profile("127.0.0.1"))
            assert (await asyncio.to_thread(connection.request, "GET", "/v1/info"))["version"] == 1
        finally:
            await receiver.close()
            await service.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "address",
    [
        "https://127.0.0.1:1234/path",
        "https://127.0.0.1:1234",
        "https://127.0.0.1:1234?token=example",
        "not-an-ip",
    ],
)
def test_listener_rejects_invalid_address_with_config_guidance(tmp_path, address):
    async def run():
        with pytest.raises(ValueError, match="监听地址"):
            await receiver_at(tmp_path, lambda port: address)

    asyncio.run(run())


def test_pinned_upload_two_tracks_retry_review_and_restart(tmp_path):
    async def run():
        service, receiver = await receiver_at(tmp_path / "A")
        profile = receiver.profile("127.0.0.1")
        connection = Connection(profile)
        spool = Spool(tmp_path / "B")
        folder = spool.create(profile)
        for track in ("playback", "microphone"):
            name = track + ".wav"
            (folder / name).write_bytes(wav_bytes())
            spool.add_part(folder, track, 1, name, 0.2)
        spool.update(folder, complete=True)

        class LostAcknowledgement:
            profile = connection.profile
            failed = False

            def request(self, method, path, *args, **kwargs):
                result = connection.request(method, path, *args, **kwargs)
                if "/parts/" in path and not self.failed:
                    self.failed = True
                    raise ConnectionError("Acknowledgement lost after A saved audio")
                return result

        try:
            assert (await asyncio.to_thread(connection.request, "GET", "/v1/info"))["version"] == 1
            with pytest.raises(ValueError, match="指纹"):
                await asyncio.to_thread(
                    Connection({**profile, "fingerprint": "0" * 64}).request, "GET", "/v1/info"
                )
            with pytest.raises(ValueError, match="凭据"):
                await asyncio.to_thread(
                    Connection({**profile, "token": "wrong" * 9}).request, "GET", "/v1/info"
                )
            with pytest.raises(ValueError):
                await asyncio.to_thread(connection.request, "GET", "/api/sessions")
            interrupted = await asyncio.to_thread(spool.sync, LostAcknowledgement())
            assert "Acknowledgement" in interrupted["errors"][0]["error"]
            assert interrupted["pending"] == 1
            # Restarting the client reloads the durable queue. The retried part is accepted once.
            resumed = Spool(spool.root)
            result = await asyncio.to_thread(resumed.sync, connection)
            assert result["completed"] == 1 and not resumed.pending()
            await service.queue.join()
            sessions = service.store.sessions()
            assert len(sessions) == 1
            detail = service.store.detail(sessions[0]["id"])
            assert len(detail["chunks"]) == 2
            assert {row["source"] for row in detail["clusters"]} == {"B 麦克风", "B 群聊播放"}
            assert not service.store.examples("100001")
            assert all(row["start"] == 0.2 for row in detail["segments"])
            microphone = next(row for row in detail["clusters"] if row["source"] == "B 麦克风")
            service.store.review(microphone["id"], "approve", "200001", "Test Owner")
            assert len(service.store.examples("100001")) == 1
            assert "Test Owner" in service.store.summary_input(detail["id"])[1]
            service.store.review(microphone["id"], "revoke")
            assert not service.store.examples("100001")
            receiver.identity.rotate()
            with pytest.raises(ValueError, match="凭据"):
                await asyncio.to_thread(connection.request, "GET", "/v1/info")
            # A restart must preserve the certificate and rotated credential.
            fingerprint, token = receiver.identity.fingerprint, receiver.identity.token
        finally:
            await receiver.close()
            await service.close()
        service2, receiver2 = await receiver_at(tmp_path / "A")
        try:
            assert receiver2.identity.fingerprint == fingerprint
            assert receiver2.identity.token == token
            assert len(service2.store.sessions()) == 1
        finally:
            await receiver2.close()
            await service2.close()

    asyncio.run(run())


def test_reject_truncation_conflicts_overlap_and_premature_finish(tmp_path):
    async def run():
        service, receiver = await receiver_at(tmp_path / "A")
        connection = Connection(receiver.profile("127.0.0.1"))
        recording = "a" * 32
        base = "/v1/sessions/" + recording
        audio = tmp_path / "audio.wav"
        try:
            await asyncio.to_thread(
                connection.request, "POST", "/v1/sessions", {"client_id": recording}
            )
            audio.write_bytes(wav_bytes()[:-100])
            with pytest.raises(ValueError, match="不完整"):
                await asyncio.to_thread(
                    connection.request, "POST", base + "/parts/playback/1?offset=0", audio=audio
                )
            audio.write_bytes(wav_bytes())
            await asyncio.to_thread(
                connection.request, "POST", base + "/parts/playback/1?offset=0", audio=audio
            )
            with pytest.raises(ValueError, match="重叠"):
                await asyncio.to_thread(
                    connection.request, "POST", base + "/parts/playback/2?offset=0.5", audio=audio
                )
            with pytest.raises(ValueError, match="时间范围"):
                await asyncio.to_thread(
                    connection.request, "POST", base + "/parts/microphone/1?offset=nan", audio=audio
                )
            audio.write_bytes(wav_bytes(2))
            with pytest.raises(ValueError, match="不同内容"):
                await asyncio.to_thread(
                    connection.request, "POST", base + "/parts/playback/1?offset=0", audio=audio
                )
            with pytest.raises(ValueError, match="未上传"):
                await asyncio.to_thread(connection.request, "POST", base + "/finish", {"parts": []})
            await asyncio.to_thread(
                connection.request,
                "POST",
                base + "/finish",
                {"parts": [{"track": "playback", "part": 1}]},
            )
            with pytest.raises(ValueError, match="已完成"):
                await asyncio.to_thread(
                    connection.request, "POST", base + "/parts/microphone/1?offset=0", audio=audio
                )
            await service.queue.join()
            assert len(service.store.detail(service.store.sessions()[0]["id"])["chunks"]) == 1
            assert not list((tmp_path / "A").glob("*/upload-*.tmp"))
            session_id = service.store.sessions()[0]["id"]
            receiver.end_receiving(session_id)
            await service.delete(session_id)
            with pytest.raises(ValueError, match="删除"):
                await asyncio.to_thread(
                    connection.request, "POST", "/v1/sessions", {"client_id": recording}
                )
            assert not service.store.sessions()
        finally:
            await receiver.close()
            await service.close()

    asyncio.run(run())

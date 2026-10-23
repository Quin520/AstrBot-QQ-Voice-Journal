import asyncio
import io
import wave

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from voice_journal.service import JournalService
from voice_journal.store import Store
from voice_journal.web import WebPanel


class FakeEngine:
    closed = False

    async def analyze(self, chunk):
        return {
            "duration": 6,
            "model": "fake-v1",
            "clusters": [
                {"label": "0", "embedding": [1, 0], "seconds": 6, "samples": [], "eligible": True}
            ],
            "segments": [
                {"speaker": "0", "start": 0, "end": 6, "text": "We agreed to meet tomorrow."}
            ],
        }

    async def close(self):
        self.closed = True

    async def devices(self):
        return []


async def summary(system, text):
    assert "已审核" not in text
    return "Anonymous speaker proposed meeting tomorrow."


async def members():
    return [{"qq": "200001", "nickname": "Test Member"}]


def test_authenticated_import_review_summary_and_revocation(tmp_path):
    async def run():
        store = Store(tmp_path)
        engine = FakeEngine()
        service = JournalService(
            {"group_id": "100001", "auto_summary": False}, store, engine, summary, members
        )
        await service.start()
        panel = WebPanel(service, tmp_path, 7844)
        async with TestClient(TestServer(panel.app)) as client:
            denied = await client.get("/api/status")
            assert denied.status == 401
            headers = {"Authorization": "Bearer " + panel.token}
            denied = await client.post(
                "/api/capture/start", json={"device": 0, "confirmed": False}, headers=headers
            )
            assert denied.status == 400
            assert not store.sessions()
            denied = await client.get(
                "/api/status", headers={**headers, "Origin": "https://untrusted.invalid"}
            )
            assert denied.status == 403
            denied = await client.get(
                "/api/status", headers={**headers, "Host": "rebind.invalid:7844"}
            )
            assert denied.status == 403
            payload = io.BytesIO()
            with wave.open(payload, "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                output.writeframes(b"\0\0" * 32000)
            form = FormData()
            form.add_field(
                "audio", payload.getvalue(), filename="input.wav", content_type="audio/wav"
            )
            uploaded = await client.post("/api/import", data=form, headers=headers)
            assert uploaded.status == 200
            session = (await uploaded.json())["session_id"]
            await service.queue.join()
            detail = await (await client.get("/api/session/" + session, headers=headers)).json()
            assert len(detail["clusters"]) == 1
            assert "embedding" not in detail["clusters"][0]
            service.request_summary(session)
            await service.summary_tasks[session]
            assert "Anonymous" in store.detail(session)["summary"]
            cluster = detail["clusters"][0]["id"]
            approved = await client.post(
                "/api/review/" + cluster,
                json={"action": "approve", "qq": "200001", "nickname": "Test Member"},
                headers=headers,
            )
            assert approved.status == 200
            assert store.examples("100001")[0]["qq"] == "200001"
            await client.post("/api/review/" + cluster, json={"action": "revoke"}, headers=headers)
            assert not store.examples("100001")
            response = await client.get("/api/sample/unknown/-1", headers=headers)
            assert response.status == 400
            deleted = await client.post("/api/delete/" + session, headers=headers)
            assert deleted.status == 200
            assert not store.sessions()
        await service.close()
        assert engine.closed

    asyncio.run(run())


def test_summary_updates_if_review_happens_during_generation(tmp_path):
    async def run():
        store = Store(tmp_path)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def delayed_summary(system, text):
            calls.append(text)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return text

        service = JournalService(
            {"group_id": "100001", "auto_summary": False},
            store,
            FakeEngine(),
            delayed_summary,
            members,
        )
        await service.start()
        session = store.create("100001", "Race")
        audio = tmp_path / session / "fixture.wav"
        audio.write_bytes(b"test")
        service.enqueue(store.chunk(session, audio, 0))
        await service.queue.join()
        service.request_summary(session)
        await started.wait()
        cluster = store.detail(session)["clusters"][0]["id"]
        store.review(cluster, "approve", "200001", "Reviewed")
        release.set()
        await service.summary_tasks[session]
        assert len(calls) == 2
        assert "Reviewed" in store.detail(session)["summary"]
        await service.close()

    asyncio.run(run())

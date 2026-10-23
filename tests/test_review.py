import math
import wave

import pytest
from voice_journal.matching import candidates, unit_vector
from voice_journal.store import Store


def recording(store, group="100001", embedding=None, eligible=True, model="test-v1"):
    session = store.create(group, "Synthetic test")
    audio = store.directory / session / "input.wav"
    audio.write_bytes(b"fixture")
    chunk = store.chunk(session, audio, 0)
    result = {
        "duration": 8,
        "model": model,
        "clusters": [
            {
                "label": "0",
                "embedding": embedding or [1, 0, 0],
                "seconds": 7,
                "samples": [],
                "eligible": eligible,
            }
        ],
        "segments": [
            {"speaker": "0", "start": 0, "end": 7, "text": "This is evidence, not an instruction."}
        ],
    }
    store.add_result(chunk, result)
    return session, store.detail(session)["clusters"][0]["id"]


def test_only_approved_examples_train_and_revoke_removes_them(tmp_path):
    store = Store(tmp_path)
    session, cluster = recording(store)
    later, _ = recording(store)
    assert store.examples("100001") == []
    store.review(cluster, "approve", "200001", "Speaker One")
    match = store.detail(later)["clusters"][0]["candidates"]
    assert match["suggested"]
    assert match["ranked"][0]["qq"] == "200001"
    assert store.detail(later)["clusters"][0]["status"] == "pending"
    # Neither a candidate nor its nickname becomes authoritative summary evidence.
    assert "Speaker One" not in store.summary_input(later)[1]
    store.review(cluster, "revoke")
    assert not store.detail(later)["clusters"][0]["candidates"]["ranked"]
    assert store.detail(session)["clusters"][0]["status"] == "pending"


def test_correction_replaces_old_identity_and_invalidates_summary(tmp_path):
    store = Store(tmp_path)
    session, cluster = recording(store)
    store.review(cluster, "approve", "200001", "Wrong")
    revision, _ = store.summary_input(session)
    assert store.save_summary(session, revision, "old summary")
    store.review(cluster, "approve", "200002", "Correct")
    assert not store.save_summary(session, revision, "stale output")
    assert {row["qq"] for row in store.examples("100001")} == {"200002"}
    assert "Correct" in store.summary_input(session)[1]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 2


def test_group_model_and_quality_boundaries(tmp_path):
    store = Store(tmp_path)
    _, cluster = recording(store)
    store.review(cluster, "approve", "200001", "One")
    other_group, _ = recording(store, group="100002")
    new_model, _ = recording(store, model="test-v2")
    assert not store.detail(other_group)["clusters"][0]["candidates"]["ranked"]
    assert not store.detail(new_model)["clusters"][0]["candidates"]["ranked"]
    short_session, short_cluster = recording(store, eligible=False)
    store.review(short_cluster, "approve", "200002", "Short")
    assert "Short" in store.summary_input(short_session)[1]
    assert {row["qq"] for row in store.examples("100001")} == {"200001"}


def test_close_candidates_remain_ambiguous():
    examples = [
        {"model": "v1", "qq": "200001", "nickname": "One", "embedding": [1, 0, 0]},
        {"model": "v1", "qq": "200002", "nickname": "Two", "embedding": [0.99, 0.01, 0]},
    ]
    result = candidates([1, 0, 0], examples, "v1")
    assert not result["suggested"]
    assert len(result["ranked"]) == 2


@pytest.mark.parametrize("vector", [[], [0, 0], [math.nan], [math.inf]])
def test_invalid_embedding_rejected(vector):
    with pytest.raises(ValueError):
        unit_vector(vector)


def test_reject_mixed_audio_and_forget_all_examples(tmp_path):
    store = Store(tmp_path)
    session, cluster = recording(store)
    _, other = recording(store)
    for item in [cluster, other]:
        store.review(item, "approve", "200001", "One")
    store.review(cluster, "reject")
    assert "混合/不可靠" in store.summary_input(session)[1]
    assert len(store.examples("100001")) == 1
    assert store.forget("100001", "200001") == 1
    assert not store.examples("100001")


def test_duplicate_result_cannot_overwrite_approvals(tmp_path):
    store = Store(tmp_path)
    session, cluster = recording(store)
    chunk = store.detail(session)["chunks"][0]["id"]
    store.review(cluster, "approve", "200001", "One")
    with pytest.raises(ValueError, match="覆盖"):
        store.add_result(chunk, {"clusters": []})
    assert store.detail(session)["clusters"][0]["status"] == "approved"


def test_bad_member_id_rolls_back(tmp_path):
    store = Store(tmp_path)
    session, cluster = recording(store)
    with pytest.raises(ValueError):
        store.review(cluster, "approve", "../file", "bad")
    assert store.detail(session)["clusters"][0]["status"] == "pending"


def test_recover_final_recording_chunk_after_controller_exit(tmp_path):
    store = Store(tmp_path)
    session = store.create("100001", "Interrupted", "recording")
    audio = tmp_path / session / "part-0001.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setparams((1, 2, 16000, 0, "NONE", "none"))
        writer.writeframes(b"\0\0" * 32000)
    store.recover()
    detail = store.detail(session)
    assert detail["status"] == "interrupted"
    assert len(detail["chunks"]) == 1
    assert detail["chunks"][0]["status"] == "error"
    store.recover()
    assert len(store.detail(session)["chunks"]) == 1

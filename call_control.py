"""Replaceable call-control boundary; the initial adapter requires manual QQ use."""

from typing import Protocol


class CallControl(Protocol):
    def capabilities(self) -> dict: ...

    async def start(self, group_id: str) -> None: ...

    async def participants(self, group_id: str) -> list[str] | None: ...


class ManualCallControl:
    def capabilities(self) -> dict:
        return {
            "supported": False,
            "mode": "manual",
            "reason": "请先在 QQ 发起或加入群语音，再手动开始采集。",
        }

    async def start(self, group_id: str) -> None:
        raise NotImplementedError("本机尚未接入已验证的 QQ 群通话发起接口。")

    async def participants(self, group_id: str) -> list[str] | None:
        # Unknown is different from an empty room; never infer departure from silence.
        return None

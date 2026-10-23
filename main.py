"""AstrBot integration through public provider/platform APIs only."""

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .engine import Engine
from .remote import RemoteReceiver
from .service import JournalService
from .store import Store
from .web import WebPanel


class VoiceJournal(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.directory = StarTools.get_data_dir("astrbot_plugin_voice_journal")
        self.store = Store(self.directory)
        self.engine = Engine(config, self.directory)
        self.service = JournalService(config, self.store, self.engine, self.summarize, self.members)
        self.remote = RemoteReceiver(self.service)
        self.panel = WebPanel(
            self.service, self.directory, int(config.get("web_port", 7844)), self.remote
        )

    async def initialize(self) -> None:
        try:
            await self.service.start()
            await self.remote.start()
            await self.panel.start()
        except BaseException:
            await self.panel.close()
            await self.remote.close()
            await self.service.close()
            raise
        logger.info("Voice journal panel listening on loopback port %s", self.panel.port)

    async def summarize(self, system: str, transcript: str) -> str:
        provider_id = str(self.config.get("provider_id", "")).strip()
        if not provider_id:
            raise ValueError("请在插件配置里选择总结提供商；转写和声纹审核不受影响。")
        response = await self.context.llm_generate(
            chat_provider_id=provider_id, system_prompt=system, prompt=transcript
        )
        return response.completion_text or ""

    async def members(self) -> list[dict]:
        platform = self.context.get_platform_inst(str(self.config.get("platform_id", "")))
        if platform is None or platform.meta().name != "aiocqhttp":
            raise ValueError("QQ 平台未就绪或平台 ID 不正确；仍可手动输入 QQ 和昵称。")
        rows = await platform.get_client().call_action(
            "get_group_member_list", group_id=int(self.config.get("group_id", "0"))
        )
        return [
            {
                "qq": str(row["user_id"]),
                "nickname": str(row.get("card") or row.get("nickname") or row["user_id"]),
            }
            for row in rows
        ]

    @filter.command("语音手记")
    async def journal_panel(self, event: AstrMessageEvent):
        """Expose the local panel only to the configured owner in a private chat."""
        if (
            event.get_platform_name() != "aiocqhttp"
            or event.get_group_id()
            or event.get_sender_id() != str(self.config.get("owner_qq", ""))
        ):
            yield event.plain_result("此入口仅供配置的管理者私聊使用。")
            return
        yield event.plain_result(
            f"群语音手记：http://127.0.0.1:{self.panel.port}/#token={self.panel.token}\n请在运行 AstrBot 的 A 电脑上打开。可在此连接 B 端采集器或审核录音。此链接包含管理口令，请勿转发。"
        )

    async def terminate(self) -> None:
        await self.panel.close()
        await self.remote.close()
        await self.service.close()

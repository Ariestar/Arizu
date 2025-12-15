import asyncio

from aiocqhttp import CQHttp
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    AtAll,
    BaseMessageComponent,
    Face,
    Forward,
    Image,
    Plain,
    Reply,
    Video,
)
from astrbot.api import logger

from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


@register(
    "astrbot_plugin_recall",
    "Zhalslar",
    "智能撤回插件，可自动判断各场景下消息是否需要撤回",
    "v1.0.1",
    "https://github.com/Zhalslar/astrbot_plugin_recall",
)
class RecallPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.forward_threshold: int = self.context.get_config()["platform_settings"][
            "forward_threshold"
        ]
        self.recall_tasks: list[asyncio.Task] = []
        self.last_msg = None

    def _remove_task(self, task: asyncio.Task):
        try:
            self.recall_tasks.remove(task)
        except ValueError:
            pass

    def _is_recall(self, chain: list[BaseMessageComponent]) -> bool:
        """判断消息是否需撤回"""
        # 判断复读
        if self.last_msg and chain == self.last_msg:
            return True
        self.last_msg = chain
        for seg in chain:
            if isinstance(seg, Plain):
                # 判断长文本
                if self.conf["max_plain_len"] < len(seg.text) < self.forward_threshold:
                    logger.debug(f"文本长度：{len(seg.text)}， 需要撤回")
                    return True
                # 判断关键词
                for word in self.conf["recall_words"]:
                    if word in seg.text:
                        return True
            elif isinstance(seg, Image):
                # TODO: 判断色图
               return False
        return False

    async def _recall_msg(self, client: CQHttp, message_id: int = 1):
        """撤回消息"""
        await asyncio.sleep(self.conf["recall_time"])
        try:
            if message_id:
                await client.delete_msg(message_id=message_id)
                logger.debug(f"已自动撤回消息: {message_id}")
        except Exception as e:
            logger.error(f"撤回消息失败: {e}")

    @filter.on_decorating_result(priority=10)
    async def on_recall(self, event: AiocqhttpMessageEvent):
        """监听消息并自动撤回"""
        # 白名单群
        group_id = event.get_group_id()
        if (
            self.conf["group_whitelist"]
            and group_id not in self.conf["group_whitelist"]
        ):
            return
        chain = event.get_result().chain
        # 无有效消息段直接退出
        if not any(
            isinstance(seg, (Plain, Image, Video, Face, At, AtAll, Forward, Reply))
            for seg in chain
        ):
            return

        # 判断消息是否需要撤回
        if not self._is_recall(chain):
            return

        obmsg = await event._parse_onebot_json(MessageChain(chain=chain))
        client = event.bot

        send_result = None
        if group_id := event.get_group_id():
            send_result = await client.send_group_msg(
                group_id=int(group_id), message=obmsg
            )
        elif user_id := event.get_sender_id():
            send_result = await client.send_private_msg(
                user_id=int(user_id), message=obmsg
            )

        # 启动撤回任务
        if (
            send_result
            and (message_id := send_result.get("message_id"))
            and self._is_recall(chain)
        ):
            task = asyncio.create_task(self._recall_msg(client, int(message_id)))  # type: ignore
            task.add_done_callback(self._remove_task)
            self.recall_tasks.append(task)

        # 清空原消息链
        chain.clear()
        event.stop_event()

    async def terminate(self):
        """插件卸载时取消所有撤回任务"""
        for task in self.recall_tasks:
            task.cancel()
        await asyncio.gather(*self.recall_tasks, return_exceptions=True)
        self.recall_tasks.clear()
        logger.info("自动撤回插件已卸载")

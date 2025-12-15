from astrbot.core.message.components import At
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


def get_ats(event: AiocqhttpMessageEvent) -> list[str]:
    """获取被at者们的id列表"""
    return [
        str(seg.qq)
        for seg in event.get_messages()
        if (isinstance(seg, At) and str(seg.qq) != event.get_self_id())
    ]

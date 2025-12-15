import asyncio
import json
import re
import time
import uuid
from typing import Any
from fastapi import Response
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig


class ResponseManager:
    """
    响应管理类
    """

    def __init__(self, config: AstrBotConfig):
        self.conf = config
        self.channels: dict[str, asyncio.Queue] = {}
        self.callback: Any = None

        # 预编译正则
        self._pat_text = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)"')
        self._pat_image = re.compile(r"[ab]2:(\[.*?\])")
        self._pat_finish = re.compile(r'[ab]d:(\{.*?"finishReason".*?\})')
        self._pat_error = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)

        # Cloudflare 识别片段
        self._cf_patterns = [
            r"<title>Just a moment...</title>",
            r"Enable JavaScript and cookies to continue",
        ]

    # ---------------- OpenAI 格式化 ----------------
    def _make_non_stream(
        self, content: str, model: str, request_id: str, reason: str = "stop"
    ) -> dict:
        """非流式响应体"""
        return {
            "id": request_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": reason,
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": len(content) // 4,
                "total_tokens": len(content) // 4,
            },
        }

    # ---------------- 错误辅助 ----------------
    def _is_cloudflare_error(self, text: str) -> bool:
        return any(re.search(p, text, re.IGNORECASE) for p in self._cf_patterns)

    def _handle_error(self, error_msg: str, request_id: str) -> str:
        """统一错误文案；可触发 Cloudflare 刷新"""
        if not isinstance(error_msg, str):
            return "未知错误"

        lower = error_msg.lower()
        match lower:
            case msg if "413" in error_msg or "too large" in msg:
                logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: 附件过大 (413)。")
                return "上传失败：附件大小超过了 LMArena 服务器的限制 (通常约 5MB)。请压缩或更换更小的文件。"

            case msg if self._is_cloudflare_error(error_msg) or "cloudflare" in msg:
                if self.callback:
                    asyncio.create_task(self.callback())
                return "检测到 Cloudflare 错误。已尝试刷新人机验证，请稍后再试。"

            case _:
                return error_msg

    # ---------------- 内部事件流 ----------------
    async def _process_lmarena_stream(self, request_id: str):
        """
        处理来自浏览器的原始数据流，产出:
          ('content', str) / ('finish', str) / ('error', str)
        """
        queue = self.channels.get(request_id)
        if not queue:
            logger.error(f"PROCESSOR [ID: {request_id[:8]}]: 无法找到响应通道。")
            yield "error", "Internal server error: response channel not found."
            return

        buffer: Any = ""
        has_yielded_content = False

        try:
            while True:
                try:
                    raw_data = await asyncio.wait_for(queue.get(), self.conf["timeout"])
                except asyncio.TimeoutError:
                    logger.warning(
                        f"PROCESSOR [ID: {request_id[:8]}]: 等待浏览器数据超时。"
                    )
                    yield (
                        "error",
                        f"Response timed out after {self.conf['timeout']} seconds.",
                    )
                    return
                match raw_data:
                    case {"error": err}:  # WebSocket 直接错误
                        yield "error", self._handle_error(err, request_id)
                        return
                    case "[DONE]":  # 结束信号
                        if has_yielded_content and getattr(
                            self, "IS_REFRESHING_FOR_VERIFICATION", False
                        ):
                            logger.info(
                                f"PROCESSOR [ID: {request_id[:8]}]: 请求成功完成，重置人机验证状态。"
                            )
                            self.IS_REFRESHING_FOR_VERIFICATION = False
                        break
                    case list() as lst:
                        buffer += "".join(str(item) for item in lst)
                    case _:
                        buffer += str(raw_data)

                # Cloudflare 检测（页面片段）
                if self._is_cloudflare_error(buffer):
                    yield "error", self._handle_error(buffer, request_id)

                # 错误 JSON
                if error_match := self._pat_error.search(buffer):
                    try:
                        error_json = json.loads(error_match.group(1))
                        yield (
                            "error",
                            error_json.get("error", "来自 LMArena 的未知错误"),
                        )
                        return
                    except json.JSONDecodeError:
                        pass

                # 文本内容
                while match_text := self._pat_text.search(buffer):
                    try:
                        text_content = json.loads(f'"{match_text.group(1)}"')
                        if text_content:
                            has_yielded_content = True
                            yield "content", text_content
                    except (ValueError, json.JSONDecodeError):
                        pass
                    buffer = buffer[match_text.end() :]

                # 图片内容
                while match_img := self._pat_image.search(buffer):
                    try:
                        image_data_list = json.loads(match_img.group(1))
                        if isinstance(image_data_list, list) and image_data_list:
                            image_info = image_data_list[0]
                            if (
                                image_info.get("type") == "image"
                                and "image" in image_info
                            ):
                                yield "content", f"![Image]({image_info['image']})"
                    except (json.JSONDecodeError, IndexError) as e:
                        logger.warning(
                            f"解析图片URL时出错: {e}, buffer: {buffer[:150]}"
                        )
                    buffer = buffer[match_img.end() :]

                # 结束原因
                if match_fin := self._pat_finish.search(buffer):
                    try:
                        finish_data = json.loads(match_fin.group(1))
                        yield "finish", finish_data.get("finishReason", "stop")
                    except (json.JSONDecodeError, IndexError):
                        pass
                    buffer = buffer[match_fin.end() :]

        except asyncio.CancelledError:
            logger.debug(f"PROCESSOR [ID: {request_id[:8]}]: 任务被取消。")
        finally:
            if request_id in self.channels:
                del self.channels[request_id]

    # ---------------- 对外接口 ----------------
    async def non_stream_response(self, request_id: str, model: str):
        """聚合内部事件流并返回单个 OpenAI JSON 响应。"""
        response_id = f"chatcmpl-{uuid.uuid4()}"
        full_content: list[str] = []
        finish_reason = "stop"

        async for event_type, data in self._process_lmarena_stream(request_id):
            match event_type:
                case "content":
                    full_content.append(data)
                case "finish":
                    finish_reason = data
                    if data == "content-filter":
                        full_content.append(
                            "\n\n响应被终止，可能是上下文超限或者模型内部审查（大概率）的原因"
                        )
                    # 不 break，等待 [DONE]，避免竞态
                case "error":
                    logger.error(
                        f"NON-STREAM [ID: {request_id[:8]}]: 处理时发生错误: {data}"
                    )
                    status_code = 413 if "附件大小超过了" in str(data) else 500
                    error_response = {
                        "error": {
                            "message": f"[LMArena Bridge Error]: {data}",
                            "type": "bridge_error",
                            "code": "attachment_too_large"
                            if status_code == 413
                            else "processing_error",
                        }
                    }
                    return Response(
                        content=json.dumps(error_response, ensure_ascii=False),
                        status_code=status_code,
                        media_type="application/json",
                    )

        final_content = "".join(full_content)
        response_data = self._make_non_stream(
            final_content, model, response_id, finish_reason
        )
        return Response(
            content=json.dumps(response_data, ensure_ascii=False),
            media_type="application/json",
        )

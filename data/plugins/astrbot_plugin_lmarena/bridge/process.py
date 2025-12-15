import mimetypes
import uuid
from astrbot import logger
from astrbot.core.config.astrbot_config import AstrBotConfig


class Process:
    """
    处理消息
    """

    def __init__(self, config: AstrBotConfig):
        self.conf = config

    @staticmethod
    def _make_file_name(content_type: str, original_name: str | None) -> str:
        if isinstance(original_name, str) and original_name.strip():
            logger.debug(f"成功处理附件 (原始文件名): {original_name}")
            return original_name

        main, sub = (content_type.split("/") + ["octet-stream"])[:2]
        prefix = {"image": "image", "audio": "audio"}.get(main, "file")
        guessed_ext = mimetypes.guess_extension(content_type)
        ext = (guessed_ext.lstrip(".") if guessed_ext else "png")[:20]
        file_name = f"{prefix}_{uuid.uuid4()}.{ext}"
        logger.debug(f"成功处理附件 (生成文件名): {file_name}")
        return file_name

    def process_openai_message(self, message: dict) -> dict:
        """
        处理OpenAI消息，分离文本和附件。
        - 将多模态内容列表分解为纯文本和附件列表。
        - 确保 user 角色的空内容被替换为空格，以避免 LMArena 出错。
        - 为附件生成基础结构。
        """
        content = message.get("content")
        role = message.get("role")
        attachments = []
        text_parts = []
        text_content = ""

        if isinstance(content, str):
            text_content = content

        elif isinstance(content, list):
            for part in content:
                match part.get("type"):
                    # 文本片段
                    case "text":
                        text_parts.append(part.get("text", ""))

                    # 图片 / 文件附件
                    case "image_url":
                        img = part.get("image_url", {})
                        url, original_name = img.get("url"), img.get("detail")

                        if not url:
                            continue

                        try:
                            if url.startswith("data:"):  # base64
                                content_type = url.split(";")[0].split(":")[1]
                            else:  # 普通 URL，直接猜类型
                                content_type = (
                                    mimetypes.guess_type(url)[0]
                                    or "application/octet-stream"
                                )

                            file_name = self._make_file_name(
                                content_type, original_name
                            )
                            # file_name = "file_de04bac6-cd82-476a-8474-cb3871386fe5.png"
                            attachments.append(
                                {
                                    "name": file_name,
                                    "contentType": content_type,
                                    "url": url,
                                }
                            )
                        except Exception as e:
                            logger.warning(f"无法处理图片 URL: {url[:60]}... 错误: {e}")

        text_content = "\n\n".join(text_parts)

        # user 角色必须保证有非空内容
        if role == "user" and not text_content.strip():
            text_content = " "

        return {"role": role, "content": text_content, "attachments": attachments}

    def openai_to_lmarena(self, openai_req: dict) -> list[dict]:
        """
        将 OpenAI 请求体转换为油猴脚本所需的简化载荷，并应用酒馆模式、绕过模式以及对战模式。
        新增了模式覆盖参数，以支持模型特定的会话模式。
        """
        messages = openai_req.get("messages", [])

        # 规范角色:  developer -> system
        for msg in messages:
            if msg.get("role") == "developer":
                msg["role"] = "system"

        # 分离文本和附件
        processed_messages = [
            self.process_openai_message(msg.copy()) for msg in messages
        ]

        # 应用酒馆模式 (合并所有 system 消息为一个整体，并保证 system 消息没有附件)
        if self.conf["tavern_mode_enabled"]:
            system_contents = [
                msg["content"] for msg in processed_messages if msg["role"] == "system"
            ]
            non_system_messages = [
                msg for msg in processed_messages if msg["role"] != "system"
            ]
            final_messages = []
            if system_contents:
                final_messages.append(
                    {
                        "role": "system",
                        "content": "\n\n".join(system_contents),
                        "attachments": [],
                    }
                )
            final_messages.extend(non_system_messages)
            processed_messages = final_messages

        # 构建消息模板
        templates = []
        for msg in processed_messages:
            templates.append(
                {
                    "role": msg["role"],
                    "content": msg.get("content", ""),
                    "attachments": msg.get("attachments", []),
                }
            )

        # 伪造助手消息，绕过图片外审
        if self.conf["image_bypass"] and templates:
            if (att := templates[-1].get("attachments")) and any(
                a.get("contentType", "").startswith("image/") for a in att
            ):
                templates[-1:-1] = [
                    {"role": "assistant", "content": "", "attachments": att.copy()}
                ]
                if templates[0]["role"] == "assistant":
                    logger.debug("首位是助手消息，补虚假用户消息")
                    templates.insert(
                        0, {"role": "user", "content": "Hi", "attachments": []}
                    )

        # 绕过敏感词: 对文本模型添加一个 position 'a' 的用户消息
        if self.conf["text_bypass"]:
            templates.append(
                {
                    "role": "user",
                    "content": " ",
                    "participantPosition": "a",
                    "attachments": [],
                }
            )

        # 应用参与者位置
        for msg in templates:
            match msg["role"]:
                case "system":
                    msg["participantPosition"] = "b"
                case _:
                    msg["participantPosition"] = self.conf["battle_target"].lower()
        return templates

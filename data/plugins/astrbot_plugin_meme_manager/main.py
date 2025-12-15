import asyncio
import copy
import io
import logging
import os
import random
import re
import ssl
import time
from multiprocessing import Process

import aiohttp
from PIL import Image as PILImage

from astrbot.api.all import *
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import *
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

from .backend.category_manager import CategoryManager
from .config import DEFAULT_CATEGORY_DESCRIPTIONS, MEMES_DATA_PATH, MEMES_DIR
from .image_host.img_sync import ImageSync
from .init import init_plugin
from .utils import dict_to_string, generate_secret_key, get_public_ip, load_json
from .webui import ServerState, run_server


@register(
    "meme_manager", "anka", "anka - 表情包管理器 - 支持表情包发送及表情包上传", "3.18"
)
class MemeSender(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 初始化插件
        if not init_plugin():
            raise RuntimeError("插件初始化失败")

        # 初始化类别管理器
        self.category_manager = CategoryManager()

        # 初始化图床同步客户端
        self.img_sync = None
        image_host_type = self.config.get("image_host", "stardots")

        if image_host_type == "stardots":
            stardots_config = self.config.get("image_host_config", {}).get(
                "stardots", {}
            )
            if stardots_config.get("key") and stardots_config.get("secret"):
                # 添加提供商信息到配置中
                stardots_config["provider"] = "stardots"
                self.img_sync = ImageSync(
                    config={
                        "key": stardots_config["key"],
                        "secret": stardots_config["secret"],
                        "space": stardots_config.get("space", "memes"),
                        "provider": "stardots",
                    },
                    local_dir=MEMES_DIR,
                    provider_type="stardots",
                )
        elif image_host_type == "cloudflare_r2":
            r2_config = self.config.get("image_host_config", {}).get(
                "cloudflare_r2", {}
            )
            required_fields = [
                "account_id",
                "access_key_id",
                "secret_access_key",
                "bucket_name",
            ]
            if all(r2_config.get(field) for field in required_fields):
                # 确保 public_url 不以斜杠结尾
                if r2_config.get("public_url"):
                    r2_config["public_url"] = r2_config["public_url"].rstrip("/")
                # 添加提供商信息到配置中
                r2_config["provider"] = "cloudflare_r2"
                self.img_sync = ImageSync(
                    config=r2_config, local_dir=MEMES_DIR, provider_type="cloudflare_r2"
                )
                # 延迟日志记录，避免 logger 未初始化
                self._r2_bucket_name = r2_config.get("bucket_name")

        # 用于管理服务器
        self.webui_process = None

        self.server_key = None
        self.server_port = self.config.get("webui_port", 5000)

        # 初始化表情状态
        self.found_emotions = []  # 存储找到的表情
        self.upload_states = {}  # 存储上传状态：{user_session: {"category": str, "expire_time": float}}
        self.pending_images = {}  # 存储待发送的图片

        # 读取表情包分隔符
        self.fault_tolerant_symbols = self.config.get("fault_tolerant_symbols", ["⬡"])

        # 初始化 logger
        self.logger = logging.getLogger(__name__)

        # 记录 R2 初始化日志（如果已初始化）
        if hasattr(self, "_r2_bucket_name"):
            self.logger.info(f"Cloudflare R2 图床已初始化: {self._r2_bucket_name}")
            delattr(self, "_r2_bucket_name")

        # 处理人格
        self.prompt_head = self.config.get("prompt").get("prompt_head")
        self.prompt_tail_1 = self.config.get("prompt").get("prompt_tail_1")
        self.prompt_tail_2 = self.config.get("prompt").get("prompt_tail_2")
        self.max_emotions_per_message = self.config.get("max_emotions_per_message")
        self.emotions_probability = self.config.get("emotions_probability")
        self.strict_max_emotions_per_message = self.config.get(
            "strict_max_emotions_per_message"
        )

        # 内容清理规则
        self.content_cleanup_rule = self.config.get(
            "content_cleanup_rule", "&&[a-zA-Z]*&&"
        )

        # 更新人格
        personas = self.context.provider_manager.personas
        self.persona_backup = copy.deepcopy(personas)
        self._reload_personas()

    @filter.command_group("表情管理")
    def meme_manager(self):
        """表情包管理命令组:
        开启管理后台
        关闭管理后台
        查看图库
        添加表情
        同步状态
        同步到云端
        从云端同步
        """
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("开启管理后台")
    async def start_webui(self, event: AstrMessageEvent):
        """启动表情包管理服务器"""
        yield event.plain_result("🚀 正在启动管理后台，请稍等片刻～")

        try:
            state = ServerState()
            state.ready.clear()

            # 生成秘钥
            self.server_key = generate_secret_key(8)
            self.server_port = self.config.get("webui_port", 5000)

            # 检查端口占用情况
            if await self._check_port_active():
                yield event.plain_result("🔧 检测到端口占用，正在尝试自动释放...")
                await self._shutdown()
                await asyncio.sleep(1)  # 等待系统释放端口

            config_for_server = {
                "img_sync": self.img_sync,
                "category_manager": self.category_manager,
                "webui_port": self.server_port,
                "server_key": self.server_key,
            }
            self.webui_process = Process(target=run_server, args=(config_for_server,))
            self.webui_process.start()

            # 等待服务器就绪（轮询检测端口激活）
            for i in range(10):
                if await self._check_port_active():
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError("⌛ 启动超时，请检查防火墙设置")

            # 获取公网IP并返回结果
            public_ip = await get_public_ip()
            yield event.plain_result(
                f"✨ 管理后台已就绪！\n"
                f"━━━━━━━━━━━━━━\n"
                f"表情包管理服务器已启动！\n"
                f"⚠️ 如果地址错误或未发出, 请使用 [服务器公网ip]:{self.server_port} 访问\n"
                f"🔑 临时密钥：{self.server_key} （本次有效）\n"
                f"⚠️ 请勿分享给未授权用户"
            )
            yield event.plain_result(
                f"🔗 访问地址：http://{public_ip}:{self.server_port}\n"
            )

        except Exception as e:
            self.logger.error(f"启动失败: {str(e)}")
            yield event.plain_result(
                f"⚠️ 后台启动失败，请稍后重试\n（错误代码：{str(e)}）"
            )
            await self._cleanup_resources()

    async def _check_port_active(self):
        """验证端口是否实际已激活"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.server_port), timeout=1
            )
            writer.close()
            return True
        except Exception:
            return False

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("关闭管理后台")
    async def stop_server(self, event: AstrMessageEvent):
        """关闭表情包管理服务器的指令"""
        yield event.plain_result("🚪 管理后台正在关闭，稍后见~ ✨")

        try:
            await self._shutdown()
            yield event.plain_result("✅ 服务器已关闭")
        except Exception as e:
            yield event.plain_result(f"❌ 安全关闭失败: {str(e)}")
        finally:
            await self._cleanup_resources()

    async def _shutdown(self):
        if self.webui_process:
            self.webui_process.terminate()
            self.webui_process.join()

    async def _cleanup_resources(self):
        if self.img_sync:
            self.img_sync.stop_sync()
        self.server_key = None
        self.server_port = None
        if self.webui_process:
            if self.webui_process.is_alive():
                self.webui_process.terminate()
                self.webui_process.join()
        self.webui_process = None
        self.logger.info("资源清理完成")

    def _reload_personas(self):
        """重新注入人格"""
        self.category_mapping = load_json(
            MEMES_DATA_PATH, DEFAULT_CATEGORY_DESCRIPTIONS
        )
        self.category_mapping_string = dict_to_string(self.category_mapping)
        self.sys_prompt_add = (
            self.prompt_head
            + self.category_mapping_string
            + self.prompt_tail_1
            + str(self.max_emotions_per_message)
            + self.prompt_tail_2
        )
        personas = self.context.provider_manager.personas
        for persona, persona_backup in zip(personas, self.persona_backup):
            persona["prompt"] = persona_backup["prompt"] + self.sys_prompt_add

    @meme_manager.command("查看图库")
    async def list_emotions(self, event: AstrMessageEvent):
        """查看所有可用表情包类别"""
        descriptions = self.category_mapping
        categories = "\n".join(
            [f"- {tag}: {desc}" for tag, desc in descriptions.items()]
        )
        yield event.plain_result(f"🖼️ 当前图库：\n{categories}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("添加表情")
    async def upload_meme(self, event: AstrMessageEvent, category: str = None):
        """上传表情包到指定类别"""
        if not category:
            yield event.plain_result(
                "📌 若要添加表情，请按照此格式操作：\n/表情管理 添加表情 [类别名称]\n（输入/查看图库 可获取类别列表）"
            )
            return

        if category not in self.category_manager.get_descriptions():
            yield event.plain_result(
                f"您输入的表情包类别「{category}」是无效的哦。\n可以使用/查看表情包来查看可用的类别。"
            )
            return

        user_key = f"{event.session_id}_{event.get_sender_id()}"
        self.upload_states[user_key] = {
            "category": category,
            "expire_time": time.time() + 30,
        }
        yield event.plain_result(
            f"请在30秒内发送要添加到【{category}】类别的图片（可发送多张图片）。"
        )

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_upload_image(self, event: AstrMessageEvent):
        """处理用户上传的图片"""
        user_key = f"{event.session_id}_{event.get_sender_id()}"
        upload_state = self.upload_states.get(user_key)

        if not upload_state or time.time() > upload_state["expire_time"]:
            if user_key in self.upload_states:
                del self.upload_states[user_key]
            return

        images = [c for c in event.message_obj.message if isinstance(c, Image)]

        if not images:
            yield event.plain_result("请发送图片文件来进行上传哦。")
            return

        category = upload_state["category"]
        save_dir = os.path.join(MEMES_DIR, category)

        try:
            os.makedirs(save_dir, exist_ok=True)
            saved_files = []

            # 创建忽略 SSL 验证的上下文
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            for idx, img in enumerate(images, 1):
                timestamp = int(time.time())

                try:
                    # 特殊处理腾讯多媒体域名
                    if "multimedia.nt.qq.com.cn" in img.url:
                        insecure_url = img.url.replace("https://", "http://", 1)
                        self.logger.warning(
                            f"检测到腾讯多媒体域名，使用 HTTP 协议下载: {insecure_url}"
                        )
                        async with aiohttp.ClientSession() as session:
                            async with session.get(insecure_url) as resp:
                                content = await resp.read()
                    else:
                        async with aiohttp.ClientSession(
                            connector=aiohttp.TCPConnector(ssl=ssl_context)
                        ) as session:
                            async with session.get(img.url) as resp:
                                content = await resp.read()

                    try:
                        with PILImage.open(io.BytesIO(content)) as img:
                            file_type = img.format.lower()
                    except Exception as e:
                        self.logger.error(f"图片格式检测失败: {str(e)}")
                        file_type = "unknown"

                    ext_mapping = {
                        "jpeg": ".jpg",
                        "png": ".png",
                        "gif": ".gif",
                        "webp": ".webp",
                    }
                    ext = ext_mapping.get(file_type, ".bin")
                    filename = f"{timestamp}_{idx}{ext}"
                    save_path = os.path.join(save_dir, filename)

                    with open(save_path, "wb") as f:
                        f.write(content)
                    saved_files.append(filename)

                except Exception as e:
                    self.logger.error(f"下载图片失败: {str(e)}")
                    yield event.plain_result(f"文件 {img.url} 下载失败啦: {str(e)}")
                    continue

            del self.upload_states[user_key]

            # 基础成功消息
            result_msg = [
                Plain(
                    f"✅ 已经成功收录了 {len(saved_files)} 张新表情到「{category}」图库！"
                )
            ]

            # 如果配置了图床，提示用户需要手动同步
            if self.img_sync:
                result_msg.append(Plain("\n"))
                result_msg.append(
                    Plain("☁️ 检测到已配置图床，如需同步到云端请使用命令：同步到云端")
                )

            yield event.chain_result(result_msg)
            await self.reload_emotions()

        except Exception as e:
            yield event.plain_result(f"保存失败了：{str(e)}")

    async def reload_emotions(self):
        """动态重新加载表情配置"""
        try:
            self.category_manager.sync_with_filesystem()

        except Exception as e:
            self.logger.error(f"重新加载表情配置失败: {str(e)}")

    def _is_position_in_thinking_tags(self, text: str, position: int) -> bool:
        """检查指定位置是否在thinking标签内

        Args:
            text: 原始文本
            position: 要检查的位置

        Returns:
            True如果位置在thinking标签内，False否则
        """
        import re
        # 找到所有thinking标签的开始和结束位置
        thinking_pattern = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)

        for match in thinking_pattern.finditer(text):
            if match.start() <= position < match.end():
                return True
        return False

    def _check_meme_directories(self):
        """检查表情包目录是否存在并且包含图片"""
        self.logger.info(f"开始检查表情包根目录: {MEMES_DIR}")
        if not os.path.exists(MEMES_DIR):
            self.logger.error(f"表情包根目录不存在，请检查: {MEMES_DIR}")
            return

        for emotion in self.category_manager.get_descriptions().values():
            emotion_path = os.path.join(MEMES_DIR, emotion)
            if not os.path.exists(emotion_path):
                self.logger.error(
                    f"表情分类 {emotion} 对应的目录不存在，请查看: {emotion_path}"
                )
                continue

            memes = [
                f
                for f in os.listdir(emotion_path)
                if f.endswith((".jpg", ".png", ".gif"))
            ]
            if not memes:
                self.logger.error(f"表情分类 {emotion} 对应的目录为空: {emotion_path}")
            else:
                self.logger.info(
                    f"表情分类 {emotion} 对应的目录 {emotion_path} 包含 {len(memes)} 个图片"
                )

    @filter.on_llm_response(priority=99999)
    async def resp(self, event: AstrMessageEvent, response: LLMResponse):
        """处理 LLM 响应，识别表情"""

        if not response or not response.completion_text:
            return

        text = response.completion_text
        self.found_emotions = []  # 重置表情列表
        valid_emoticons = set(self.category_mapping.keys())  # 预加载合法表情集合

        clean_text = text

        # 第一阶段：严格匹配符号包裹的表情
        hex_pattern = r"&&([^&&]+)&&"
        matches = re.finditer(hex_pattern, clean_text)

        # 严格模式处理
        temp_replacements = []
        for match in matches:
            original = match.group(0)
            emotion = match.group(1).strip()

            # 合法性验证
            if emotion in valid_emoticons:
                temp_replacements.append((original, emotion))
            else:
                temp_replacements.append((original, ""))  # 非法表情静默移除

        # 保持原始顺序替换
        for original, emotion in temp_replacements:
            clean_text = clean_text.replace(original, "", 1)  # 每次替换第一个匹配项
            if emotion:
                self.found_emotions.append(emotion)

        # 第二阶段：替代标记处理（如[emotion]、(emotion)等）
        if self.config.get("enable_alternative_markup", True):
            # 处理[emotion]格式
            bracket_pattern = r"\[([^\[\]]+)\]"
            matches = re.finditer(bracket_pattern, clean_text)
            bracket_replacements = []
            invalid_brackets = []

            for match in matches:
                original = match.group(0)
                emotion = match.group(1).strip()

                if emotion in valid_emoticons:
                    bracket_replacements.append((original, emotion))
                else:
                    # 记录无效标记，稍后删除
                    invalid_brackets.append(original)

            # 删除所有无效标记
            for invalid in invalid_brackets:
                clean_text = clean_text.replace(invalid, "", 1)

            for original, emotion in bracket_replacements:
                clean_text = clean_text.replace(original, "", 1)
                self.found_emotions.append(emotion)

            # 处理(emotion)格式
            paren_pattern = r"\(([^()]+)\)"
            matches = re.finditer(paren_pattern, clean_text)
            paren_replacements = []
            invalid_parens = []

            for match in matches:
                original = match.group(0)
                emotion = match.group(1).strip()

                if emotion in valid_emoticons:
                    # 需要额外验证，确保不是普通句子的一部分
                    if self._is_likely_emotion_markup(
                        original, clean_text, match.start()
                    ):
                        paren_replacements.append((original, emotion))
                else:
                    # 记录无效标记，稍后删除
                    invalid_parens.append(original)

            # 删除所有无效标记
            for invalid in invalid_parens:
                clean_text = clean_text.replace(invalid, "", 1)

            for original, emotion in paren_replacements:
                clean_text = clean_text.replace(original, "", 1)
                self.found_emotions.append(emotion)

        # 第三阶段：处理重复表情模式（如angryangryangry）
        if self.config.get("enable_repeated_emotion_detection", True):
            high_confidence_emotions = self.config.get("high_confidence_emotions", [])

            for emotion in valid_emoticons:
                # 跳过太短的表情词，避免误判
                if len(emotion) < 3:
                    continue

                # 对高置信度表情，重复两次即可识别
                if emotion in high_confidence_emotions:
                    # 检测重复两次的模式，如 happyhappy
                    repeat_pattern = f"({re.escape(emotion)})\\1{{1,}}"
                    matches = re.finditer(repeat_pattern, clean_text)
                    for match in matches:
                        # 跳过thinking标签内的内容
                        if self._is_position_in_thinking_tags(clean_text, match.start()):
                            continue
                        original = match.group(0)
                        clean_text = clean_text.replace(original, "", 1)
                        self.found_emotions.append(emotion)
                else:
                    # 普通表情词需要重复至少3次才识别
                    # 只检查长度>=4的表情，以减少误判
                    if len(emotion) >= 4:
                        # 查找表情词重复3次以上的模式
                        repeat_pattern = f"({re.escape(emotion)})\\1{{2,}}"
                        matches = re.finditer(repeat_pattern, clean_text)
                        for match in matches:
                            # 跳过thinking标签内的内容
                            if self._is_position_in_thinking_tags(clean_text, match.start()):
                                continue
                            original = match.group(0)
                            clean_text = clean_text.replace(original, "", 1)
                            self.found_emotions.append(emotion)

        # 第四阶段：智能识别可能的表情（松散模式）
        if self.config.get("enable_loose_emotion_matching", True):
            # 查找所有可能的表情词
            for emotion in valid_emoticons:
                # 使用单词边界确保不是其他单词的一部分
                pattern = r"\b(" + re.escape(emotion) + r")\b"
                for match in re.finditer(pattern, clean_text):
                    word = match.group(1)
                    position = match.start()

                    # 跳过thinking标签内的内容
                    if self._is_position_in_thinking_tags(clean_text, position):
                        continue

                    # 判断是否可能是表情而非英文单词
                    if self._is_likely_emotion(
                        word, clean_text, position, valid_emoticons
                    ):
                        # 添加到表情列表
                        self.found_emotions.append(word)
                        # 替换文本中的表情词
                        clean_text = (
                            clean_text[:position] + clean_text[position + len(word) :]
                        )

        # 去重并应用数量限制
        seen = set()
        filtered_emotions = []
        for emo in self.found_emotions:
            if emo not in seen:
                seen.add(emo)
                filtered_emotions.append(emo)
            if len(filtered_emotions) >= self.max_emotions_per_message:
                break

        self.found_emotions = filtered_emotions

        # 防御性清理残留符号
        clean_text = re.sub(r"&&+", "", clean_text)  # 清除未成对的&&符号
        response.completion_text = clean_text.strip()

    def _is_likely_emotion_markup(self, markup, text, position):
        """判断一个标记是否可能是表情而非普通文本的一部分"""
        # 获取标记前后的文本
        before_text = text[:position].strip()
        after_text = text[position + len(markup) :].strip()

        # 如果是在中文上下文中，更可能是表情
        has_chinese_before = bool(
            re.search(r"[\u4e00-\u9fff]", before_text[-1:] if before_text else "")
        )
        has_chinese_after = bool(
            re.search(r"[\u4e00-\u9fff]", after_text[:1] if after_text else "")
        )
        if has_chinese_before or has_chinese_after:
            return True

        # 如果在数字标记中，可能是引用标记如[1]，不是表情
        if re.match(r"\[\d+\]", markup):
            return False

        # 如果标记内有空格，可能是普通句子，不是表情
        if " " in markup[1:-1]:
            return False

        # 如果标记前后是完整的英文句子，可能不是表情
        english_context_before = bool(re.search(r"[a-zA-Z]\s+$", before_text))
        english_context_after = bool(re.search(r"^\s+[a-zA-Z]", after_text))
        if english_context_before and english_context_after:
            return False

        # 默认情况下认为可能是表情
        return True

    def _is_likely_emotion(self, word, text, position, valid_emotions):
        """判断一个单词是否可能是表情而非普通英文单词"""

        # 先获取上下文
        before_text = text[:position].strip()
        after_text = text[position + len(word) :].strip()

        # 规则1：检查是否在英文上下文中
        # 如果前面有英文单词+空格，或后面有空格+英文单词，可能是英文上下文
        english_context_before = bool(re.search(r"[a-zA-Z]\s+$", before_text))
        english_context_after = bool(re.search(r"^\s+[a-zA-Z]", after_text))

        # 在英文上下文中，不太可能是表情
        if english_context_before or english_context_after:
            return False

        # 规则2：前后有中文字符，更可能是表情
        has_chinese_before = bool(
            re.search(r"[\u4e00-\u9fff]", before_text[-1:] if before_text else "")
        )
        has_chinese_after = bool(
            re.search(r"[\u4e00-\u9fff]", after_text[:1] if after_text else "")
        )

        if has_chinese_before or has_chinese_after:
            return True

        # 规则3：如果是句子开头或结尾，可能是表情
        if not before_text or before_text.endswith(
            ("。", "，", "！", "？", ".", ",", ":", ";", "!", "?", "\n")
        ):
            return True

        # 规则4：如果前后都是标点或空格，可能是表情
        if (not before_text or before_text[-1] in " \t\n.,!?;:'\"()[]{}") and (
            not after_text or after_text[0] in " \t\n.,!?;:'\"()[]{}"
        ):
            return True

        # 规则5：如果是已知的表情占比很高(>=70%)的单词，即使在英文上下文中也可能是表情
        if word in self.config.get("high_confidence_emotions", []):
            return True

        return False

    @filter.on_decorating_result(priority=99999)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在消息发送前清理文本中的表情标签，并添加表情图片"""
        result = event.get_result()
        if not result or result.result_content_type == ResultContentType.STREAMING_FINISH:
            return

        try:
            # 第一步：获取并清理原始消息链中的文本
            original_chain = result.chain
            cleaned_components = []

            if original_chain:
                # 处理不同类型的消息链
                if isinstance(original_chain, str):
                    # 字符串类型：清理后转为 Plain 组件
                    cleaned = re.sub(self.content_cleanup_rule, "", original_chain) if self.content_cleanup_rule else original_chain
                    if cleaned.strip():
                        cleaned_components.append(Plain(cleaned.strip()))

                elif isinstance(original_chain, MessageChain):
                    # MessageChain 类型：遍历清理 Plain 组件
                    for component in original_chain.chain:
                        if isinstance(component, Plain):
                            cleaned = re.sub(self.content_cleanup_rule, "", component.text) if self.content_cleanup_rule else component.text
                            if cleaned.strip():
                                cleaned_components.append(Plain(cleaned.strip()))
                        else:
                            # 保留非文本组件（如已有的图片等）
                            cleaned_components.append(component)

                elif isinstance(original_chain, list):
                    # 列表类型：遍历清理 Plain 组件
                    for component in original_chain:
                        if isinstance(component, Plain):
                            cleaned = re.sub(self.content_cleanup_rule, "", component.text) if self.content_cleanup_rule else component.text
                            if cleaned.strip():
                                cleaned_components.append(Plain(cleaned.strip()))
                        else:
                            cleaned_components.append(component)

            # 第二步：添加表情图片（如果有找到的表情）
            if self.found_emotions:
                # 检查概率（注意：概率判断是"小于等于"才发送）
                if random.randint(1, 100) <= self.emotions_probability:
                    # 创建表情图片列表
                    emotion_images = []
                    for emotion in self.found_emotions:
                        if not emotion:
                            continue

                        emotion_path = os.path.join(MEMES_DIR, emotion)
                        if not os.path.exists(emotion_path):
                            continue

                        memes = [
                            f
                            for f in os.listdir(emotion_path)
                            if f.endswith((".jpg", ".png", ".gif"))
                        ]
                        if not memes:
                            continue

                        meme = random.choice(memes)
                        meme_file = os.path.join(emotion_path, meme)

                        try:
                            emotion_images.append(Image.fromFileSystem(meme_file))
                        except Exception as e:
                            self.logger.error(f"添加表情图片失败: {e}")

                    # 将图片与文本组件智能配对，支持分段回复
                    if emotion_images:
                        self.logger.info(f"找到 {len(emotion_images)} 个表情图片，开始与文本配对")
                        self.logger.info(f"配对前的组件数量: {len(cleaned_components)}")
                        cleaned_components = self._merge_components_with_images(cleaned_components, emotion_images)
                        self.logger.info(f"配对后的组件数量: {len(cleaned_components)}")
                        # 打印配对后的组件类型
                        for i, comp in enumerate(cleaned_components):
                            comp_type = type(comp).__name__
                            if isinstance(comp, Plain):
                                self.logger.info(f"组件 {i}: {comp_type} - {comp.text[:20]}...")
                            else:
                                self.logger.info(f"组件 {i}: {comp_type}")
                    else:
                        self.logger.info("没有找到表情图片")

                # 清空已处理的表情列表
                self.found_emotions = []

            # 第三步：更新消息链
            if cleaned_components:
                # 直接使用组件列表，不要包装在 MessageChain 中
                result.chain = cleaned_components
            elif original_chain:
                # 如果原本有内容但清理后为空，也要更新（避免发送带标签的空消息）
                # 进行最后的防御性清理
                if isinstance(original_chain, str):
                    final_cleaned = re.sub(r"&&+", "", original_chain)  # 清除残留的&&符号
                    if final_cleaned.strip():
                        result.chain = [Plain(final_cleaned.strip())]
                elif isinstance(original_chain, MessageChain):
                    # 对 MessageChain 中的每个 Plain 组件进行最后清理
                    final_components = []
                    for component in original_chain.chain:
                        if isinstance(component, Plain):
                            final_cleaned = re.sub(r"&&+", "", component.text)
                            if final_cleaned.strip():
                                final_components.append(Plain(final_cleaned.strip()))
                        else:
                            final_components.append(component)
                    if final_components:
                        result.chain = final_components

        except Exception as e:
            self.logger.error(f"处理消息装饰失败: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """消息发送后处理。目前无需操作，逻辑已前置到 decorate_message。"""
        # 此处的逻辑已移至 on_decorating_result 钩子，以兼容不支持主动消息的平台。
        # 保留此空函数用于可能的调试或未来扩展。
        pass

    @meme_manager.command("同步状态")
    async def check_sync_status(self, event: AstrMessageEvent, detail: str = None):
        """检查表情包与图床的同步状态"""
        if not self.img_sync:
            yield event.plain_result(
                "图床服务尚未配置，请先在插件页面的配置中完成图床配置哦。"
            )
            return

        try:
            # 获取图床配置信息
            provider_name = self.img_sync.provider.__class__.__name__
            if hasattr(self.img_sync.provider, "bucket_name"):
                storage_info = f"存储桶: {self.img_sync.provider.bucket_name}"
            elif hasattr(self.img_sync.provider, "album_id"):
                storage_info = f"相册ID: {self.img_sync.provider.album_id}"
            else:
                storage_info = "未知存储类型"

            # 获取同步状态
            status = self.img_sync.check_status()
            to_upload = status.get("to_upload", [])
            to_download = status.get("to_download", [])

            # 统计信息
            result = [
                "📊 图床同步状态报告",
                "",
                f"🔧 图床服务: {provider_name}",
                f"📁 {storage_info}",
                "",
                "📈 文件统计:",
                f"  • 需要上传: {len(to_upload)} 个文件",
                f"  • 需要下载: {len(to_download)} 个文件",
                ""
            ]

            # 分类统计
            upload_categories = {}
            download_categories = {}

            for file in to_upload:
                cat = file.get("category", "未分类")
                upload_categories[cat] = upload_categories.get(cat, 0) + 1

            for file in to_download:
                cat = file.get("category", "未分类")
                download_categories[cat] = download_categories.get(cat, 0) + 1

            # 显示上传分类统计
            if upload_categories:
                result.append("📤 待上传文件分类:")
                for cat, count in sorted(upload_categories.items(), key=lambda x: x[1], reverse=True):
                    result.append(f"  • {cat}: {count} 个")
                result.append("")

            # 显示下载分类统计
            if download_categories:
                result.append("📥 待下载文件分类:")
                for cat, count in sorted(download_categories.items(), key=lambda x: x[1], reverse=True):
                    result.append(f"  • {cat}: {count} 个")
                result.append("")

            # 显示文件详情（最多各显示5个）
            if to_upload:
                result.append("📤 待上传文件示例（前5个）:")
                for file in to_upload[:5]:
                    result.append(f"  • {file.get('category', '未分类')}/{file['filename']}")
                if len(to_upload) > 5:
                    result.append(f"  • ...还有 {len(to_upload) - 5} 个文件")
                result.append("")

            if to_download:
                result.append("📥 待下载文件示例（前5个）:")
                for file in to_download[:5]:
                    result.append(f"  • {file.get('category', '未分类')}/{file['filename']}")
                if len(to_download) > 5:
                    result.append(f"  • ...还有 {len(to_download) - 5} 个文件")
                result.append("")

            # 同步状态总结
            if not to_upload and not to_download:
                result.append("✅ 云端与本地图库已经完全同步啦！")

                # 如果用户要求详细信息，显示更多内容
                if detail and detail.strip() == "详细":
                    result.append("")
                    result.append("📋 详细信息:")

                    # 显示所有文件类别的统计
                    try:
                        if hasattr(self.img_sync.provider, "get_image_list"):
                            remote_images = self.img_sync.provider.get_image_list()
                            remote_stats = {}
                            for img in remote_images:
                                cat = img.get("category", "未分类")
                                remote_stats[cat] = remote_stats.get(cat, 0) + 1

                            if remote_stats:
                                result.append("📂 云端文件分类详情:")
                                for cat, count in sorted(remote_stats.items(), key=lambda x: x[1], reverse=True):
                                    result.append(f"  • {cat}: {count} 个")

                                # 显示文件总数
                                result.append(f"📊 云端总计: {len(remote_images)} 个文件")
                            else:
                                result.append("📂 云端无文件")
                    except Exception as e:
                        result.append(f"⚠️ 获取云端详情失败: {str(e)}")

                    # 显示本地图库统计
                    local_stats = {}
                    local_total = 0
                    if os.path.exists(MEMES_DIR):
                        for category in os.listdir(MEMES_DIR):
                            category_path = os.path.join(MEMES_DIR, category)
                            if os.path.isdir(category_path):
                                files = [f for f in os.listdir(category_path)
                                       if f.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))]
                                count = len(files)
                                local_stats[category] = count
                                local_total += count

                    if local_stats:
                        result.append("")
                        result.append("📂 本地文件分类详情:")
                        for cat, count in sorted(local_stats.items(), key=lambda x: x[1], reverse=True):
                            result.append(f"  • {cat}: {count} 个")
                        result.append(f"📊 本地总计: {local_total} 个文件")
                    else:
                        result.append("")
                        result.append("📂 本地无文件")
            else:
                result.append("⏳ 需要同步以保持云端与本地图库一致")
                result.append("💡 使用 '/表情管理 同步到云端' 或 '/表情管理 从云端同步' 进行同步")

            # 上传记录统计（如果有的话）
            if hasattr(self.img_sync.sync_manager, "upload_tracker") and self.img_sync.sync_manager.upload_tracker:
                try:
                    # 获取上传记录总数
                    if hasattr(self.img_sync.sync_manager.upload_tracker, "get_uploaded_files"):
                        uploaded_files = self.img_sync.sync_manager.upload_tracker.get_uploaded_files()
                        result.append("")
                        result.append(f"📝 上传记录: 已记录 {len(uploaded_files)} 个文件")
                except Exception:
                    pass  # 忽略获取上传记录时的错误

            yield event.plain_result("\n".join(result))
        except Exception as e:
            self.logger.error(f"检查同步状态失败: {str(e)}")
            yield event.plain_result(f"检查同步状态失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("同步到云端")
    async def sync_to_remote(self, event: AstrMessageEvent):
        """将本地表情包同步到云端"""
        if not self.img_sync:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        try:
            yield event.plain_result("⚡ 正在开启云端同步任务...")
            success = await self.img_sync.start_sync("upload")
            if success:
                yield event.plain_result("云端同步已完成！")
            else:
                yield event.plain_result("云端同步失败，请查看日志哦。")
        except Exception as e:
            self.logger.error(f"同步到云端失败: {str(e)}")
            yield event.plain_result(f"同步到云端失败: {str(e)}")

    @meme_manager.command("图库统计")
    async def show_library_stats(self, event: AstrMessageEvent):
        """显示图库详细统计信息"""
        try:
            result = [
                "📊 表情包图库统计报告",
                "",
                "📁 本地图库统计:"
            ]

            # 统计本地文件
            local_stats = {}
            local_total = 0

            if os.path.exists(MEMES_DIR):
                for category in os.listdir(MEMES_DIR):
                    category_path = os.path.join(MEMES_DIR, category)
                    if os.path.isdir(category_path):
                        files = [f for f in os.listdir(category_path)
                               if f.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))]
                        count = len(files)
                        local_stats[category] = count
                        local_total += count

            # 显示本地统计
            if local_stats:
                result.append(f"  • 总文件数: {local_total} 个")
                result.append(f"  • 分类数: {len(local_stats)} 个")
                result.append("")
                result.append("📂 本地分类详情:")
                for cat, count in sorted(local_stats.items(), key=lambda x: x[1], reverse=True):
                    result.append(f"  • {cat}: {count} 个")
            else:
                result.append("  • 本地图库为空")

            # 云端统计（如果配置了图床）
            if self.img_sync:
                result.append("")
                result.append("☁️ 云端图库统计:")

                try:
                    remote_images = self.img_sync.provider.get_image_list()
                    remote_stats = {}
                    remote_total = len(remote_images)

                    for img in remote_images:
                        cat = img.get("category", "未分类")
                        remote_stats[cat] = remote_stats.get(cat, 0) + 1

                    result.append(f"  • 总文件数: {remote_total} 个")
                    result.append(f"  • 分类数: {len(remote_stats)} 个")
                    result.append("")
                    result.append("📂 云端分类详情:")
                    for cat, count in sorted(remote_stats.items(), key=lambda x: x[1], reverse=True):
                        result.append(f"  • {cat}: {count} 个")

                    # 对比统计
                    result.append("")
                    result.append("📈 本地与云端对比:")
                    result.append(f"  • 本地文件: {local_total} 个")
                    result.append(f"  • 云端文件: {remote_total} 个")

                    if local_total > remote_total:
                        result.append(f"  • 本地比云端多 {local_total - remote_total} 个文件")
                    elif remote_total > local_total:
                        result.append(f"  • 云端比本地多 {remote_total - local_total} 个文件")
                    else:
                        result.append("  • 本地与云端文件数相同")

                    # 分类对比
                    local_categories = set(local_stats.keys())
                    remote_categories = set(remote_stats.keys())

                    only_local = local_categories - remote_categories
                    only_remote = remote_categories - local_categories
                    common_categories = local_categories & remote_categories

                    if only_local:
                        result.append(f"  • 仅本地有的分类: {', '.join(sorted(only_local))}")
                    if only_remote:
                        result.append(f"  • 仅云端有的分类: {', '.join(sorted(only_remote))}")
                    if common_categories:
                        result.append(f"  • 共同分类: {len(common_categories)} 个")

                except Exception as e:
                    result.append(f"  • 获取云端统计失败: {str(e)}")
            else:
                result.append("")
                result.append("☁️ 云端图库: 未配置")

            # 存储空间估算
            result.append("")
            result.append("💾 存储空间估算:")
            if local_total > 0:
                # 假设平均每个文件 500KB
                estimated_size = local_total * 500 / 1024  # 转换为MB
                result.append(f"  • 本地图库约: {estimated_size:.1f} MB")

            if self.img_sync and "remote_total" in locals():
                estimated_remote_size = remote_total * 500 / 1024
                result.append(f"  • 云端图库约: {estimated_remote_size:.1f} MB")

            yield event.plain_result("\n".join(result))

        except Exception as e:
            self.logger.error(f"获取图库统计失败: {str(e)}")
            yield event.plain_result(f"获取图库统计失败: {str(e)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @meme_manager.command("从云端同步")
    async def sync_from_remote(self, event: AstrMessageEvent):
        """从云端同步表情包到本地"""
        if not self.img_sync:
            yield event.plain_result(
                "图床服务尚未配置，请先在配置文件中完成图床配置哦。"
            )
            return

        try:
            yield event.plain_result("开始从云端进行同步...")
            success = await self.img_sync.start_sync("download")
            if success:
                yield event.plain_result("从云端同步已完成！")
                # 重新加载表情配置
                await self.reload_emotions()
            else:
                yield event.plain_result("从云端同步失败，请查看日志哦。")
        except Exception as e:
            self.logger.error(f"从云端同步失败: {str(e)}")
            yield event.plain_result(f"从云端同步失败: {str(e)}")

    async def terminate(self):
        """清理资源"""
        # 恢复人格
        personas = self.context.provider_manager.personas
        for persona, persona_backup in zip(personas, self.persona_backup):
            persona["prompt"] = persona_backup["prompt"]

        # 停止图床同步
        if self.img_sync:
            self.img_sync.stop_sync()

        await self._shutdown()
        await self._cleanup_resources()

    def _merge_components_with_images(self, components, images):
        """将表情图片与文本组件智能配对，支持分段回复

        Args:
            components: 清理后的消息组件列表
            images: 表情图片列表

        Returns:
            合并后的消息组件列表，图片会合理地分布在文本中
        """
        if not images:
            return components

        if not components:
            # 没有文本组件，只发送图片
            return images

        # 找到所有 Plain 组件的索引
        plain_indices = [i for i, comp in enumerate(components) if isinstance(comp, Plain)]

        if not plain_indices:
            # 没有 Plain 组件，直接添加图片到末尾
            return components + images

        # 策略：将图片均匀分布在文本组件中，优先在文本后添加图片
        # 这样在分段回复时，图片更容易和对应的文本一起发送
        merged_components = components.copy()
        images_per_text = max(1, len(images) // len(plain_indices))  # 每个文本至少配一张图片
        image_index = 0
        images_inserted_so_far = 0  # 跟踪已插入的图片数量

        for idx, plain_idx in enumerate(plain_indices):
            if image_index >= len(images):
                break

            # 计算这个文本应该配多少张图片
            if idx == len(plain_indices) - 1:
                # 最后一个文本组件，分配所有剩余图片
                images_for_this_text = len(images) - image_index
            else:
                images_for_this_text = min(images_per_text, len(images) - image_index)

            # 在这个文本组件后插入图片
            # 注意：plain_idx 是在原始 components 中的位置，但由于我们已经插入了一些图片，
            # 需要考虑已插入图片对当前位置的影响
            insert_pos = plain_idx + 1 + images_inserted_so_far

            for _ in range(images_for_this_text):
                if image_index < len(images):
                    merged_components.insert(insert_pos, images[image_index])
                    image_index += 1
                    insert_pos += 1
                    images_inserted_so_far += 1

        return merged_components

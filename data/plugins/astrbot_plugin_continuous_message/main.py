import asyncio
import json
from typing import List, Tuple, Dict, Optional
from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import AstrBotConfig, logger
import astrbot.api.message_components as Comp

# 检查是否为 aiocqhttp 平台，因为合并转发是其特性
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    IS_AIOCQHTTP = True
except ImportError:
    IS_AIOCQHTTP = False

@register(
    "continuous_message",
    "aliveriver",
    "将用户短时间内发送的多条私聊消息合并成一条发送给LLM（仅私聊模式，支持合并转发消息）",
    "2.1.0"
)
class ContinuousMessagePlugin(Star):
    """
    消息防抖动插件 v2.1.0
    消息防抖动插件（仅私聊模式）
    
    功能：
    1. 拦截用户短时间内发送的多条私聊消息
    2. 在防抖时间结束后，将这些消息合并成一条发送给LLM
    3. 过滤指令消息，不参与合并
    4. 保持人格设定和对话历史
    5. 支持图片识别和传递
    6. 支持QQ合并转发消息的提取和合并（aiocqhttp平台）

    安全设计：
    - 强制仅在私聊启用，避免群聊中不同用户的消息被误合并
    """
    
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        
        self.debounce_time = float(self.config.get('debounce_time', 2.0))
        self.command_prefixes = self.config.get('command_prefixes', ['/'])
        self.enable_plugin = self.config.get('enable', True)
        self.merge_separator = self.config.get('merge_separator', '\n')
        self.enable_forward_analysis = self.config.get('enable_forward_analysis', True)
        self.forward_prefix = self.config.get('forward_prefix', '【合并转发内容】\n')
        
        # 会话存储结构:
        # {
        #   uid: {
        #     'buffer': [],
        #     'images': [],
        #     'flush_event': asyncio.Event,  # 用于唤醒 Msg 1
        #     'timer_task': asyncio.Task     # 当前正在倒计时的任务
        #   }
        # }
        self.sessions: Dict[str, Dict] = {}
        
        self._ImageComponent = None
        self._PlainComponent = None
        
        try:
            from astrbot.api.message_components import Image, Plain
            self._ImageComponent = Image
            self._PlainComponent = Plain
        except ImportError:
            try:
                from astrbot.api.message import Image, Plain
                self._ImageComponent = Image
                self._PlainComponent = Plain
            except ImportError:
                logger.error("[消息防抖动] 严重: 组件导入失败")

        logger.info(f"[消息防抖动] v2.1.0 加载 | 事件驱动模式 | 防抖: {self.debounce_time}s | 合并消息: {self.enable_forward_analysis}")

    def is_command(self, message: str) -> bool:
        message = message.strip()
        if not message: return False
        for prefix in self.command_prefixes:
            if message.startswith(prefix): return True
        return False

    def _parse_message(self, message_obj) -> Tuple[str, bool, List[str]]:
        """
        解析消息对象，提取文本、图片和合并转发信息
        
        Returns:
            (文本内容, 是否包含图片, 图片URL列表)
        """
        text = ""
        has_image = False
        image_urls = []
        try:
            if not hasattr(message_obj, "message"): return "", False, []
            
            # 遍历消息组件，提取文本、图片和合并转发
            for component in message_obj.message:
                # 提取文本内容（支持多种属性名）
                if hasattr(component, 'text') and component.text:
                    text += component.text
                elif hasattr(component, 'content') and component.content:
                    text += component.content
                
                # 识别图片组件（优先使用 isinstance，后备使用类名检查）
                is_img = False
                if self._ImageComponent and isinstance(component, self._ImageComponent): is_img = True
                elif component.__class__.__name__ == 'Image': is_img = True
                
                # 提取图片URL（支持 url 或 file 属性）
                if is_img:
                    has_image = True
                    if hasattr(component, 'url') and component.url: image_urls.append(component.url)
                    elif hasattr(component, 'file') and component.file: image_urls.append(component.file)
        except Exception:
            pass
        return text, has_image, image_urls

    def _reconstruct_event(self, event: AstrMessageEvent, text: str, image_urls: List[str]):
        """
        重构消息事件，将合并后的文本和图片重新组装到事件对象中
        这样事件可以继续传播给后续的插件/框架处理
        """
        event.message_str = text
        if not self._PlainComponent: return

        # 构建消息组件链：文本 + 图片
        chain = []
        if text:
            chain.append(self._PlainComponent(text=text))
        
        # 添加图片组件（兼容不同的 Image 构造函数参数）
        if image_urls and self._ImageComponent:
            for url in image_urls:
                try:
                    chain.append(self._ImageComponent(file=url))
                except TypeError:
                    # 如果 file 参数不支持，尝试 url 参数
                    chain.append(self._ImageComponent(url=url))
                except Exception: pass
        
        # 更新事件的消息对象
        if hasattr(event.message_obj, "message"):
            try:
                event.message_obj.message = chain
            except Exception: pass

    async def _timer_coroutine(self, uid: str, duration: float):
        """
        计时器协程：等待指定时间后触发结算
        
        当有新消息到来时，旧计时器会被取消（CancelledError），
        新消息会创建新的计时器重新开始倒计时。
        """
        try:
            await asyncio.sleep(duration)
            # 时间到且未被取消，触发结算事件（唤醒等待的主协程）
            if uid in self.sessions:
                self.sessions[uid]['flush_event'].set()
        except asyncio.CancelledError:
            # 任务被取消（说明有新消息到来，计时器需要重置）
            # 直接退出即可，新消息会创建新的计时器
            pass

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=50)
    async def handle_private_msg(self, event: AstrMessageEvent):
        if not self.enable_plugin or self.debounce_time <= 0: return

        # 0. 检测并处理合并转发消息（仅aiocqhttp平台）
        forward_text = ""
        forward_images = []
        if self.enable_forward_analysis and IS_AIOCQHTTP and isinstance(event, AiocqhttpMessageEvent):
            forward_id = await self._detect_forward_message(event)
            if forward_id:
                try:
                    forward_text, forward_images = await self._extract_forward_content(event, forward_id)
                    if forward_text or forward_images:
                        logger.info(f"[消息防抖动] 检测到合并转发 | 文本: {len(forward_text)}字 | 图片: {len(forward_images)}张")
                except Exception as e:
                    logger.error(f"[消息防抖动] 提取合并转发失败: {e}")

        # 1. 解析消息内容
        raw_text, has_image, current_urls = self._parse_message(event.message_obj)
        if not raw_text: raw_text = (event.message_str or "").strip()
        
        # 合并转发内容处理：如果有合并转发内容，添加到文本和图片中
        if forward_text:
            prefix_text = self.forward_prefix + forward_text
            raw_text = prefix_text + ("\n" + raw_text if raw_text else "")
        if forward_images:
            current_urls.extend(forward_images)
            has_image = True
        
        uid = event.unified_msg_origin

        # 2. 处理指令消息：立即中断当前防抖会话并结算
        # 指令消息本身不会参与防抖，会正常传播执行
        if self.is_command(raw_text):
            if uid in self.sessions:
                # 取消计时器，立即触发结算（已收集的消息会先发送）
                if self.sessions[uid].get('timer_task'):
                    self.sessions[uid]['timer_task'].cancel()
                self.sessions[uid]['flush_event'].set()
            return

        # 3. 忽略空消息（既无文本也无图片）
        if not raw_text and not has_image: return

        # ================== 核心防抖逻辑 ==================

        # 场景 A: 追加到现有会话 (Msg 2, 3...)
        # 用户已有活跃的防抖会话，将新消息追加到缓冲区
        if uid in self.sessions:
            session = self.sessions[uid]
            
            # 1. 追加数据到缓冲区
            if raw_text: session['buffer'].append(raw_text)
            if current_urls: session['images'].extend(current_urls)
            
            # 2. 重置计时器：取消旧任务，创建新任务（重新开始倒计时）
            if session.get('timer_task'):
                session['timer_task'].cancel()
            
            session['timer_task'] = asyncio.create_task(
                self._timer_coroutine(uid, self.debounce_time)
            )
            
            # 3. 阻止当前事件继续传播（消息内容已加入缓冲区，无需单独处理）
            event.stop_event()
            return

        # 场景 B: 启动新会话 (Msg 1)
        # 这是用户的第一条消息，需要创建新的防抖会话并等待
        
        # 1. 初始化会话数据
        flush_event = asyncio.Event()  # 用于唤醒等待的协程
        timer_task = asyncio.create_task(
            self._timer_coroutine(uid, self.debounce_time)
        )
        
        self.sessions[uid] = {
            'buffer': [raw_text] if raw_text else [],  # 文本消息缓冲区
            'images': current_urls,                    # 图片URL列表
            'flush_event': flush_event,                # 结算触发事件
            'timer_task': timer_task                   # 当前计时器任务
        }
        
        logger.info(f"[消息防抖动] 开始收集 - 用户: {uid}")

        # 2. 挂起主协程，等待结算触发
        # 当计时器超时或收到指令时，_timer_coroutine 会调用 flush_event.set() 唤醒这里
        await flush_event.wait()
        
        # ================== 结算阶段 ==================
        # 计时器超时或被指令中断，开始合并消息并重构事件
        
        # 3. 从会话存储中取出所有缓冲数据
        if uid not in self.sessions: return  # 防御性检查（理论上不应发生）
        session_data = self.sessions.pop(uid)  # 取出并删除会话（避免重复处理）
        
        buffer = session_data['buffer']
        all_images = session_data['images']
        merged_text = self.merge_separator.join(buffer).strip()
        
        # 4. 合并消息并重构事件对象
        if not merged_text and not all_images: return  # 空数据直接返回

        img_info = f" + {len(all_images)}图" if all_images else ""
        logger.info(f"[消息防抖动] 结算触发 - 共 {len(buffer)} 条{img_info} -> 发送")
        
        # 重构事件：将合并后的文本和图片重新组装到事件中
        # 重构后的事件会继续传播给后续插件/框架，由它们处理 LLM 调用
        self._reconstruct_event(event, merged_text, all_images)
        return

    async def _detect_forward_message(self, event: AiocqhttpMessageEvent) -> Optional[str]:
        """
        检测消息中是否包含合并转发消息
        
        支持两种场景：
        1. 用户直接发送合并转发消息
        2. 用户回复了一条合并转发消息
        
        Returns:
            合并转发消息的ID，如果没有则返回None
        """
        # 场景1: 直接发送的合并转发
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Forward):
                return seg.id
        
        # 场景2: 回复的合并转发
        reply_seg = None
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.Reply):
                reply_seg = seg
                break
        
        if reply_seg:
            try:
                client = event.bot
                original_msg = await client.api.call_action('get_msg', message_id=reply_seg.id)
                
                if original_msg and 'message' in original_msg:
                    original_message_chain = original_msg['message']
                    if isinstance(original_message_chain, list):
                        for segment in original_message_chain:
                            if isinstance(segment, dict) and segment.get("type") == "forward":
                                return segment.get("data", {}).get("id")
            except Exception as e:
                logger.debug(f"[消息防抖动] 获取被回复消息失败: {e}")
        
        return None

    async def _extract_forward_content(
        self, 
        event: AiocqhttpMessageEvent, 
        forward_id: str
    ) -> Tuple[str, List[str]]:
        """
        从合并转发消息中提取文本和图片URL
        
        Args:
            event: aiocqhttp消息事件对象
            forward_id: 合并转发消息的ID
            
        Returns:
            (格式化的文本内容, 图片URL列表)
        """
        client = event.bot
        
        try:
            forward_data = await client.api.call_action('get_forward_msg', id=forward_id)
        except Exception as e:
            logger.error(f"[消息防抖动] 调用 get_forward_msg API 失败: {e}")
            raise ValueError("获取合并转发内容失败，可能是消息已过期或API问题")

        if not forward_data or "messages" not in forward_data:
            raise ValueError("获取到的合并转发内容为空")

        extracted_texts = []
        image_urls = []

        for message_node in forward_data["messages"]:
            sender_name = message_node.get("sender", {}).get("nickname", "未知用户")
            # 兼容 'message' 和 'content' 两个可能的键
            raw_content = message_node.get("message") or message_node.get("content", [])

            # 解析消息内容（可能是字符串或列表）
            content_chain = self._parse_raw_content(raw_content)
            
            # 提取文本和图片
            node_text_parts = []
            for segment in content_chain:
                if not isinstance(segment, dict):
                    continue
                    
                seg_type = segment.get("type")
                seg_data = segment.get("data", {})
                
                if seg_type == "text":
                    text = seg_data.get("text", "")
                    if text:
                        node_text_parts.append(text)
                
                elif seg_type == "image":
                    url = seg_data.get("url")
                    if url:
                        image_urls.append(url)
                        node_text_parts.append("[图片]")
            
            full_node_text = "".join(node_text_parts).strip()
            if full_node_text:
                extracted_texts.append(f"{sender_name}: {full_node_text}")

        # 返回格式化的文本内容和图片列表
        return "\n".join(extracted_texts), image_urls

    def _parse_raw_content(self, raw_content) -> List[dict]:
        """
        解析原始消息内容
        
        支持的格式：
        1. 列表形式: [{"type": "text", "data": {...}}, ...]
        2. JSON字符串: '[{"type": "text", ...}]'
        3. 纯文本字符串: "hello world"
        
        Args:
            raw_content: 原始消息内容（字符串或列表）
            
        Returns:
            标准化的消息链列表
        """
        # 如果已经是列表，直接返回
        if isinstance(raw_content, list):
            return raw_content
        
        # 如果是字符串，尝试解析为JSON
        if isinstance(raw_content, str):
            try:
                parsed_content = json.loads(raw_content)
                if isinstance(parsed_content, list):
                    return parsed_content
            except (json.JSONDecodeError, TypeError):
                pass
            
            # 解析失败，当作纯文本处理
            return [{"type": "text", "data": {"text": raw_content}}]
        
        # 其他情况返回空列表
        return []
# 文件名: main.py (位于 data/plugins/astrbot_plugin_proactive_chat/ 目录下)
# 版本: 1.0.0-beta.7 (多会话架构版)

# 导入标准库
import asyncio
import json
import random
import time
import traceback
import zoneinfo
from datetime import datetime

# v0.9.8 修复 (持久化会话): 导入 aiofiles 及其异步os模块，彻底解决事件循环阻塞问题
import aiofiles
import aiofiles.os as aio_os

# 导入第三方库
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# 导入 AstrBot 的核心 API 和组件
import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

# v0.9.9 优化 (API 适配): 导入官方定义的消息对象，以使用 add_message_pair
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    UserMessageSegment,
)
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain

# --- 插件主类 ---


# v0.9.95 优化: 将插件主类命名为更具描述性的 ProactiveChatPlugin
class ProactiveChatPlugin(star.Star):
    """
    插件的主类，继承自 astrbot.api.star.Star。
    负责插件的生命周期管理、事件监听和核心逻辑执行。
    """

    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        """
        插件的构造函数。
        当 AstrBot 加载插件时被调用。
        config (AstrBotConfig): 插件专属的配置对象，由 AstrBot 框架通过依赖注入自动传入。
        """
        super().__init__(context)

        # v0.9.95 优化: 增加 config 参数的类型注解
        self.config: AstrBotConfig = config
        self.scheduler = None
        self.timezone = None

        # v0.9.8 修复 (持久化会话): 使用 StarTools 获取插件专属数据目录，确保数据隔离
        self.data_dir = star.StarTools.get_data_dir("astrbot_plugin_proactive_chat")
        # v0.9.9 优化 (代码质量): 使用 pathlib 的 / 操作符拼接路径，更现代化、更具可读性
        self.session_data_file = self.data_dir / "session_data.json"

        self.data_lock = None
        self.session_data = {}

        # v1.0.0-beta.1 新增: 用于群聊消息流监听的定时器字典
        # 键为会话ID，值为asyncio.TimerHandle对象，用于管理每个群聊的沉默倒计时
        # 当群聊中有人说话时，会重置对应的计时器；当计时器到期时，触发群聊的主动消息任务创建
        self.group_timers: dict[str, asyncio.TimerHandle] = {}

        # v1.0.0-beta.1 修复: 用于辅助检测Bot消息的时间戳
        # 记录最后一次检测到Bot消息的时间，用于时间窗口检测算法
        # 用于辅助在Bot发送消息时正确重置监听器的倒计时
        self.last_bot_message_time = 0

        # v1.0.0-beta.4 修复: 使用会话隔离的状态管理，避免竞态条件
        # 将全局状态改为以session_id为键的字典，确保每个群聊会话状态独立
        self.session_temp_state: dict[str, dict] = {}  # 存储每个会话的临时状态

        # v1.0.0-beta.1 架构重构: 不再在初始化时缓存配置，改为按需、按会话获取

        # v1.0.0-beta.2 新增: 自动主动消息功能的数据结构
        self.last_message_times: dict[str, float] = {}  # 记录每个会话的最后消息时间
        self.auto_trigger_timers: dict[str, asyncio.TimerHandle] = {}  # 自动触发计时器
        self.plugin_start_time = time.time()  # 插件启动时间

        # v1.0.0-beta.2 新增: 用于控制相关日志只打印一次
        self.first_message_logged: set[str] = set()  # 记录已经打印过首次消息日志的会话

        logger.info("[主动消息] 插件实例已创建喵。")

    # --- 数据持久化核心函数 ---

    async def _load_data_internal(self):
        """
        从文件中加载会话数据（异步无锁内部实现）。

        这是数据持久化的核心函数之一，负责：
        1. 检查会话数据文件是否存在
        2. 异步读取文件内容
        3. 解析JSON格式的会话数据
        4. 处理可能的异常情况（文件不存在、JSON解析错误等）

        注意：此函数必须在持有data_lock的情况下被调用，确保线程安全。
        使用asyncio.to_thread将同步的json.loads操作放入独立线程，避免阻塞事件循环。
        """
        if await aio_os.path.exists(self.session_data_file):
            try:
                async with aiofiles.open(self.session_data_file, encoding="utf-8") as f:
                    content = await f.read()
                    self.session_data = await asyncio.to_thread(json.loads, content)
            except (OSError, json.JSONDecodeError) as e:
                logger.error(
                    f"[主动消息] 加载会话数据失败喵: {e}，将使用空数据启动喵。"
                )
                self.session_data = {}
        else:
            self.session_data = {}

    async def _save_data_internal(self):
        """
        将会话数据保存到文件（异步无锁内部实现）。

        这是数据持久化的核心函数之一，负责：
        1. 确保数据目录存在（如果不存在则创建）
        2. 异步打开会话数据文件
        3. 将session_data字典转换为JSON格式
        4. 异步写入文件内容
        5. 处理可能的IO异常

        使用ensure_ascii=False保证中文字符正常保存，indent=4提高可读性。
        此函数必须在持有data_lock的情况下被调用，确保数据一致性。
        """
        try:
            await aio_os.makedirs(self.data_dir, exist_ok=True)
            async with aiofiles.open(
                self.session_data_file, "w", encoding="utf-8"
            ) as f:
                content_to_write = await asyncio.to_thread(
                    json.dumps, self.session_data, indent=4, ensure_ascii=False
                )
                await f.write(content_to_write)
        except OSError as e:
            logger.error(f"[主动消息] 保存会话数据失败喵: {e}")

    # --- 插件生命周期函数 ---

    async def initialize(self):
        """插件的异步初始化函数。"""
        self.data_lock = asyncio.Lock()

        # v1.0.0-beta.2 优化: 添加配置验证
        try:
            await self._validate_config()
        except Exception as e:
            logger.warning(
                f"[主动消息] 配置验证发现问题喵: {e}，将继续使用默认设置喵。"
            )

        async with self.data_lock:
            await self._load_data_internal()
        logger.info("[主动消息] 已成功从文件加载会话数据喵。")

        # v1.0.0-beta.7 修复: 从持久化数据中恢复最后消息时间
        # 注意：只恢复插件启动后的消息时间，插件启动前的历史消息不影响自动触发功能
        restored_count = 0
        for session_id, session_info in self.session_data.items():
            if isinstance(session_info, dict) and "last_message_time" in session_info:
                last_time = session_info["last_message_time"]
                if isinstance(last_time, (int, float)) and last_time > 0:
                    # 只恢复插件启动后的消息时间
                    if last_time >= self.plugin_start_time:
                        self.last_message_times[session_id] = last_time
                        restored_count += 1
                        logger.debug(f"[主动消息] 已恢复会话在插件启动后的消息时间喵: {session_id} -> {datetime.fromtimestamp(last_time)}")
                    else:
                        logger.debug(f"[主动消息] 忽略插件启动前的历史消息时间用于自动主动消息任务喵: {session_id} -> {datetime.fromtimestamp(last_time)}")
          
        if restored_count > 0:
            logger.info(f"[主动消息] 已从持久化数据恢复 {restored_count} 个会话在插件启动后的消息时间喵。")

        try:
            self.timezone = zoneinfo.ZoneInfo(self.context.get_config().get("timezone"))
        except (zoneinfo.ZoneInfoNotFoundError, TypeError, KeyError, ValueError) as e:
            logger.warning(
                f"[主动消息] 时区配置无效或未配置喵 ({e})，将使用服务器系统时区作为备用喵。"
            )
            self.timezone = None

        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        await self._init_jobs_from_data()
        logger.info("[主动消息] 调度器已初始化喵。")

        # v1.0.0-beta.2 新增: 为启用的会话设置自动主动消息触发器
        await self._setup_auto_triggers_for_enabled_sessions()
        logger.info("[主动消息] 自动主动消息触发器初始化完成喵。")

    async def terminate(self):
        """插件被卸载或停用时调用的清理函数。"""
        # v1.0.0-beta.1 修复: 终止所有正在运行的 asyncio 计时器，防止插件停用后依然有逻辑在运转
        timer_count = len(self.group_timers)
        for session_id, timer in self.group_timers.items():
            try:
                timer.cancel()
                logger.debug(f"[主动消息] 已取消会话 {session_id} 的沉默计时器喵。")
            except Exception as e:
                logger.warning(f"[主动消息] 取消计时器时出错喵: {e}")

        self.group_timers.clear()
        logger.info(f"[主动消息] 已取消 {timer_count} 个正在运行的群聊沉默计时器喵。")

        # v1.0.0-beta.7 修复: 清理自动触发计时器，防止插件停用后仍有自动触发任务运行
        # 这是一个历史遗留的bug，从1.0.0-beta.2版本就存在
        auto_trigger_count = len(self.auto_trigger_timers)
        for session_id, timer in list(
            self.auto_trigger_timers.items()
        ):  # 使用list避免字典在迭代时修改
            try:
                timer.cancel()
                logger.debug(f"[主动消息] 已取消会话 {session_id} 的自动触发计时器喵。")
            except Exception as e:
                logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")

        self.auto_trigger_timers.clear()
        logger.info(f"[主动消息] 已取消 {auto_trigger_count} 个自动触发计时器喵。")

        # 清理调度器中的所有任务
        if self.scheduler and self.scheduler.running:
            try:
                # 获取所有任务并移除
                jobs = self.scheduler.get_jobs()
                for job in jobs:
                    try:
                        self.scheduler.remove_job(job.id)
                        logger.debug(f"[主动消息] 已移除调度器任务喵: {job.id}")
                    except Exception as e:
                        logger.warning(f"[主动消息] 移除调度器任务时出错喵: {e}")

                self.scheduler.shutdown()
                logger.info("[主动消息] 调度器已关闭喵。")
            except Exception as e:
                logger.error(f"[主动消息] 关闭调度器时出错喵: {e}")

        # 保存数据
        if self.data_lock:
            try:
                async with self.data_lock:
                    await self._save_data_internal()
                logger.info("[主动消息] 会话数据已保存喵。")
            except Exception as e:
                logger.error(f"[主动消息] 保存数据时出错喵: {e}")

        logger.info("[主动消息] 主动消息插件已终止喵。")

    # --- v1.0.0-beta.2 新增: 配置验证 ---
    async def _validate_config(self):
        """验证插件配置的完整性和有效性"""
        try:
            private_settings = self.config.get("private_settings", {})
            group_settings = self.config.get("group_settings", {})

            # 验证私聊配置 - 检查新的多会话架构
            if private_settings.get("enable", False):
                # 检查个性化配置槽位
                private_sessions = self.config.get("private_sessions", {})
                has_personal_config = False
                for session_key in [
                    "session_1",
                    "session_2",
                    "session_3",
                    "session_4",
                    "session_5",
                ]:
                    session_config = private_sessions.get(session_key, {})
                    if session_config.get("enable", False) and session_config.get(
                        "session_id"
                    ):
                        has_personal_config = True
                        break

                # 检查全局session_list
                session_list = private_settings.get("session_list", [])

                if not has_personal_config and not session_list:
                    logger.warning(
                        "[主动消息] 私聊主动消息已启用但未配置任何会话喵（既无个性化配置也无session_list）。"
                    )

                schedule_settings = private_settings.get("schedule_settings", {})
                min_interval = schedule_settings.get("min_interval_minutes", 30)
                max_interval = schedule_settings.get("max_interval_minutes", 900)

                if min_interval > max_interval:
                    logger.warning(
                        "[主动消息] 私聊配置中最小间隔大于最大间隔喵，将自动调整喵。"
                    )

                # v1.0.0-beta.2 移除: 开发阶段不需要最小间隔警告
                # if min_interval < 5:
                #     logger.warning("[主动消息] 私聊最小间隔设置过小（<5分钟），建议增加间隔时间喵。")

            # 验证群聊配置 - 检查新的多会话架构
            if group_settings.get("enable", False):
                # 检查个性化配置槽位
                group_sessions = self.config.get("group_sessions", {})
                has_personal_config = False
                for session_key in [
                    "session_1",
                    "session_2",
                    "session_3",
                    "session_4",
                    "session_5",
                ]:
                    session_config = group_sessions.get(session_key, {})
                    if session_config.get("enable", False) and session_config.get(
                        "session_id"
                    ):
                        has_personal_config = True
                        break

                # 检查全局session_list
                session_list = group_settings.get("session_list", [])

                if not has_personal_config and not session_list:
                    logger.warning(
                        "[主动消息] 群聊主动消息已启用但未配置任何会话喵（既无个性化配置也无session_list）。"
                    )

                # v1.0.0-beta.2 移除: 开发阶段不需要沉默时间警告
                # idle_minutes = group_settings.get("group_idle_trigger_minutes", 10)
                # if idle_minutes < 5:
                #     logger.warning("[主动消息] 群聊沉默触发时间设置过小（<5分钟），建议增加时间喵。")

            logger.info("[主动消息] 配置验证完成喵。")

        except Exception as e:
            logger.error(f"[主动消息] 配置验证过程出错喵: {e}")
            raise

    # --- v1.0.0-beta.2 新增: 自动主动消息功能 ---

    async def _setup_auto_trigger(self, session_id: str, silent: bool = False):
        """
        为指定会话设置自动主动消息触发器（核心实现）。

        这是自动主动消息功能的核心方法，负责：
        1. 检查会话是否启用了自动触发功能
        2. 设置自动触发计时器
        3. 当计时器到期时，创建主动消息任务（不是直接发送消息）

        参数：
        - session_id: 目标会话ID
        - silent: 是否静默执行（不打印日志）

        注意：这个功能只在插件启动后的一段时间内有效，一旦收到消息就会取消自动触发。
        """
        session_config = self._get_session_config(session_id)
        if not session_config:
            return

        auto_trigger_settings = session_config.get("auto_trigger_settings", {})

        # 检查是否启用了自动触发功能
        if not auto_trigger_settings.get("enable_auto_trigger", False):
            logger.debug(f"[主动消息] 会话 {session_id} 未启用自动主动消息功能喵。")
            return

        auto_trigger_minutes = auto_trigger_settings.get(
            "auto_trigger_after_minutes", 5
        )
        if auto_trigger_minutes <= 0:
            logger.debug(
                f"[主动消息] 会话 {session_id} 的自动触发时间设置为0，禁用自动触发喵。"
            )
            return

        # 取消现有的自动触发计时器
        if session_id in self.auto_trigger_timers:
            try:
                self.auto_trigger_timers[session_id].cancel()
                logger.debug(
                    f"[主动消息] 已取消会话 {session_id} 的现有自动触发计时器喵。"
                )
            except Exception as e:
                logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")
            finally:
                del self.auto_trigger_timers[session_id]

        # 定义自动触发回调函数 - 修复闭包变量捕获问题
        def _auto_trigger_callback(captured_session_id=session_id):
            try:
                # 检查是否仍然需要自动触发（避免重复触发）
                if captured_session_id not in self.auto_trigger_timers:
                    logger.debug(
                        f"[主动消息] 会话 {captured_session_id} 的自动触发已被取消，跳过喵。"
                    )
                    return

                # 检查配置是否仍然有效
                current_config = self._get_session_config(captured_session_id)
                if not current_config or not current_config.get("enable", False):
                    logger.info(
                        f"[主动消息] 会话 {captured_session_id} 的配置已禁用，取消自动触发喵。"
                    )
                    return

                # 检查是否已经有活动（收到过消息）- 修复：只检查插件启动后的消息
                last_message_time = self.last_message_times.get(captured_session_id, 0)
                current_time = time.time()
                time_since_plugin_start = current_time - self.plugin_start_time

                # 调试信息：帮助理解自动触发条件判断
                logger.debug(
                    f"[主动消息] 自动触发检查 - 会话: {captured_session_id}, "
                    f"插件启动后最后消息时间: {last_message_time}, "
                    f"插件启动时间: {self.plugin_start_time}, "
                    f"当前时间: {current_time}, "
                    f"插件运行时间: {time_since_plugin_start:.0f}秒, "
                    f"需要等待时间: {auto_trigger_minutes * 60}秒, "
                    f"插件启动后是否收到消息: {last_message_time > 0}"
                )

                # v1.0.0-beta.7 修复: 只检查插件启动后是否收到消息
                # 插件启动前的历史消息不影响自动触发功能
                if last_message_time == 0 and time_since_plugin_start >= (
                    auto_trigger_minutes * 60
                ):
                    # 重要：创建任务而不是直接发送消息，但避免持久化
                    # 自动触发的任务不应该被持久化，避免与正常任务冲突
                    try:
                        current_session_config = self._get_session_config(captured_session_id)
                        if not current_session_config:
                            logger.warning(
                                f"[主动消息] 无法获取会话配置，取消自动触发喵: {captured_session_id}"
                            )
                            return

                        schedule_conf = current_session_config.get("schedule_settings", {})
                        min_interval = (
                            int(schedule_conf.get("min_interval_minutes", 30)) * 60
                        )
                        max_interval = max(
                            min_interval,
                            int(schedule_conf.get("max_interval_minutes", 900)) * 60,
                        )
                        random_interval = random.randint(min_interval, max_interval)
                        next_trigger_time = time.time() + random_interval
                        run_date = datetime.fromtimestamp(
                            next_trigger_time, tz=self.timezone
                        )

                        # 直接添加到调度器，但不保存到session_data
                        self.scheduler.add_job(
                            self.check_and_chat,
                            "date",
                            run_date=run_date,
                            args=[captured_session_id],
                            id=captured_session_id,
                            replace_existing=True,
                            misfire_grace_time=60,
                        )

                        # 合并日志：一行包含所有关键信息
                        logger.info(
                            f"[主动消息] 🚀 会话 {captured_session_id} 满足条件，自动触发任务已创建喵！执行时间 (非持久化): {run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵"
                        )

                    except Exception as e:
                        logger.error(f"[主动消息] 自动触发任务创建失败喵: {e}")

                    # 清理自动触发计时器（只触发一次）
                    if captured_session_id in self.auto_trigger_timers:
                        del self.auto_trigger_timers[captured_session_id]

                else:
                    logger.debug(
                        f"[主动消息] 会话 {captured_session_id} 不满足自动触发条件喵："
                        f"最后消息时间={last_message_time}, 插件启动时间={self.plugin_start_time}"
                    )

            except Exception as e:
                logger.error(f"[主动消息] 自动触发回调函数执行失败喵: {e}")

        # 设置自动触发计时器
        try:
            loop = asyncio.get_running_loop()
            # 转换分钟为秒，设置延迟调用
            delay_seconds = auto_trigger_minutes * 60

            self.auto_trigger_timers[session_id] = loop.call_later(
                delay_seconds, _auto_trigger_callback
            )

            # 根据silent参数决定是否打印日志，避免重复
            if not silent:
                logger.info(
                    f"[主动消息] 已为会话 {session_id} 设置自动主动消息触发器喵，"
                    f"将在 {auto_trigger_minutes} 分钟后检查是否需要自动触发喵。"
                )

        except Exception as e:
            logger.error(f"[主动消息] 设置自动触发计时器失败喵: {e}")

    async def _cancel_auto_trigger(self, session_id: str):
        """
        取消指定会话的自动主动消息触发器。
        当收到消息时调用，确保不会重复触发。
        """
        if session_id in self.auto_trigger_timers:
            try:
                self.auto_trigger_timers[session_id].cancel()
                logger.info(f"[主动消息] 已取消会话 {session_id} 的自动触发计时器喵。")
            except Exception as e:
                logger.warning(f"[主动消息] 取消自动触发计时器时出错喵: {e}")
            finally:
                del self.auto_trigger_timers[session_id]

    def _cleanup_invalid_session_data(self):
        """
        清理无效的会话数据，包括：
        1. 删除通用格式的会话ID（如 private_message:xxx, group_message:xxx）
        2. 这些是由早期版本的自动触发功能错误创建的

        返回清理的条目数量
        """
        cleaned_count = 0
        invalid_sessions = []

        for session_id in list(self.session_data.keys()):
            # 检查是否是通用格式的错误会话ID
            if session_id.startswith("private_message:") or session_id.startswith(
                "group_message:"
            ):
                invalid_sessions.append(session_id)
                cleaned_count += 1

        # 删除无效的会话数据
        for session_id in invalid_sessions:
            del self.session_data[session_id]
            logger.info(f"[主动消息] 清理了无效的会话数据: {session_id}")

        return cleaned_count

    async def _setup_auto_triggers_for_enabled_sessions(self):
        """
        为所有启用了自动触发功能的会话设置自动主动消息触发器。
        在插件初始化时调用。
        """
        logger.info("[主动消息] 开始检查并设置自动主动消息触发器喵...")

        auto_trigger_count = 0
        processed_sessions = set()  # 记录已处理的会话，避免重复

        # 1. 检查私聊个性化配置槽位（优先级最高）
        private_sessions = self.config.get("private_sessions", {})
        for session_key in [
            "session_1",
            "session_2",
            "session_3",
            "session_4",
            "session_5",
        ]:
            session_config = private_sessions.get(session_key, {})
            if session_config.get("enable", False) and session_config.get("session_id"):
                target_id = session_config["session_id"]
                if target_id not in processed_sessions:
                    session_name = session_config.get("session_name", "")
                    auto_trigger_count += (
                        await self._setup_auto_trigger_for_session_config(
                            session_config, "FriendMessage", target_id, session_name
                        )
                    )
                    processed_sessions.add(target_id)
                    logger.debug(f"[主动消息] 已处理私聊个性化配置: {target_id}")

        # 2. 检查群聊个性化配置槽位（优先级最高）
        group_sessions = self.config.get("group_sessions", {})
        for session_key in [
            "session_1",
            "session_2",
            "session_3",
            "session_4",
            "session_5",
        ]:
            session_config = group_sessions.get(session_key, {})
            if session_config.get("enable", False) and session_config.get("session_id"):
                target_id = session_config["session_id"]
                if target_id not in processed_sessions:
                    session_name = session_config.get("session_name", "")
                    auto_trigger_count += (
                        await self._setup_auto_trigger_for_session_config(
                            session_config, "GroupMessage", target_id, session_name
                        )
                    )
                    processed_sessions.add(target_id)
                    logger.debug(f"[主动消息] 已处理群聊个性化配置: {target_id}")

        # 3. 检查全局配置的session_list（只处理未在个性化配置中的会话）
        private_settings = self.config.get("private_settings", {})
        session_list = private_settings.get("session_list", [])
        if private_settings.get("enable", False) and session_list:
            for target_id in session_list:
                if target_id not in processed_sessions:
                    # 尝试从个性化配置中获取备注名，如果没有就为空
                    session_name = ""
                    for session_key in [
                        "session_1",
                        "session_2",
                        "session_3",
                        "session_4",
                        "session_5",
                    ]:
                        session_config = private_sessions.get(session_key, {})
                        if session_config.get("session_id") == target_id:
                            session_name = session_config.get("session_name", "")
                            break

                    auto_trigger_count += (
                        await self._setup_auto_trigger_for_session_config(
                            private_settings, "FriendMessage", target_id, session_name
                        )
                    )
                    processed_sessions.add(target_id)
                    logger.debug(f"[主动消息] 已处理私聊全局配置: {target_id}")

        group_settings = self.config.get("group_settings", {})
        session_list = group_settings.get("session_list", [])
        if group_settings.get("enable", False) and session_list:
            for target_id in session_list:
                if target_id not in processed_sessions:
                    # 尝试从个性化配置中获取备注名，如果没有就为空
                    session_name = ""
                    for session_key in [
                        "session_1",
                        "session_2",
                        "session_3",
                        "session_4",
                        "session_5",
                    ]:
                        session_config = group_sessions.get(session_key, {})
                        if session_config.get("session_id") == target_id:
                            session_name = session_config.get("session_name", "")
                            break

                    auto_trigger_count += (
                        await self._setup_auto_trigger_for_session_config(
                            group_settings, "GroupMessage", target_id, session_name
                        )
                    )
                    processed_sessions.add(target_id)
                    logger.debug(f"[主动消息] 已处理群聊全局配置: {target_id}")

        # 4. 检查全局配置的默认设置（没有session_list的情况）
        if private_settings.get("enable", False) and not private_settings.get(
            "session_list", []
        ):
            logger.warning(
                "[主动消息] 私聊全局配置已启用但未配置session_list，无法设置自动触发器喵。"
            )

        if group_settings.get("enable", False) and not group_settings.get(
            "session_list", []
        ):
            logger.debug("[主动消息] 群聊全局配置已启用但未配置session_list喵。")

        if auto_trigger_count == 0:
            # 更精确地分析为什么没有设置触发器
            has_auto_trigger_config = False

            # 检查私聊个性化配置
            private_sessions = self.config.get("private_sessions", {})
            for session_key in [
                "session_1",
                "session_2",
                "session_3",
                "session_4",
                "session_5",
            ]:
                session_config = private_sessions.get(session_key, {})
                if session_config.get("auto_trigger_settings", {}).get(
                    "enable_auto_trigger", False
                ):
                    has_auto_trigger_config = True
                    break

            # 检查群聊个性化配置
            if not has_auto_trigger_config:
                group_sessions = self.config.get("group_sessions", {})
                for session_key in [
                    "session_1",
                    "session_2",
                    "session_3",
                    "session_4",
                    "session_5",
                ]:
                    session_config = group_sessions.get(session_key, {})
                    if session_config.get("auto_trigger_settings", {}).get(
                        "enable_auto_trigger", False
                    ):
                        has_auto_trigger_config = True
                        break

            # 检查全局配置
            if not has_auto_trigger_config:
                private_settings = self.config.get("private_settings", {})
                group_settings = self.config.get("group_settings", {})
                if private_settings.get("auto_trigger_settings", {}).get(
                    "enable_auto_trigger", False
                ) or group_settings.get("auto_trigger_settings", {}).get(
                    "enable_auto_trigger", False
                ):
                    has_auto_trigger_config = True

            if has_auto_trigger_config:
                logger.info(
                    "[主动消息] 检测到自动主动消息配置，但会话ID无效或未配置，无法设置触发器喵。"
                )
            else:
                logger.info("[主动消息] 没有会话启用自动主动消息功能喵。")
        else:
            logger.info(
                f"[主动消息] 已为 {auto_trigger_count} 个会话设置自动主动消息触发器喵。"
            )

    # v1.0.0-beta.7 新增: 支持多会话的自动触发器设置
    async def _setup_auto_trigger_for_session_config(
        self, settings: dict, message_type: str, target_id: str, session_name: str = ""
    ) -> int:
        """
        为指定会话配置设置自动触发器（支持多会话架构）。

        参数：
        - settings: 会话配置设置
        - message_type: 消息类型（FriendMessage 或 GroupMessage）
        - target_id: 目标ID（用户QQ号或群号）
        - session_name: 会话备注名，用于日志显示

        返回：设置的触发器数量
        """
        type_description = "私聊" if message_type == "FriendMessage" else "群聊"
        # 修复会话名称显示：如果有备注名就显示"备注名(ID)"，没有就显示ID
        if session_name and session_name.strip():
            display_name = f"{session_name}({target_id})"
        else:
            display_name = target_id

        auto_trigger_settings = settings.get("auto_trigger_settings", {})
        if not auto_trigger_settings.get("enable_auto_trigger", False):
            logger.debug(
                f"[主动消息] {type_description}{display_name} 未启用自动主动消息功能喵。"
            )
            return 0

        # 检查是否已经有持久化的主动消息任务
        has_existing_task = False
        current_time = time.time()
        for session_id, session_info in self.session_data.items():
            if (
                session_info.get("next_trigger_time")
                and session_id.endswith(f":{message_type}:{target_id}")
            ):
                next_trigger = session_info.get("next_trigger_time")
                trigger_time_with_grace = next_trigger + 60
                is_not_expired = current_time < trigger_time_with_grace

                if is_not_expired:
                    logger.debug(
                        f"[主动消息] 找到有效的{type_description}持久化任务喵: {session_id}"
                    )
                    has_existing_task = True
                    break

        if has_existing_task:
            logger.info(
                f"[主动消息] {type_description} {display_name} 已存在持久化的主动消息任务喵，"
                f"跳过自动触发器设置以避免冲突喵。"
            )
            return 0

        # 使用指定格式的会话ID，但需要先确定平台名称
        platform_name = "default"
        for existing_session_id in self.session_data.keys():
            # 修复：精确匹配，避免错误匹配到其他会话
            if existing_session_id.endswith(f":{message_type}:{target_id}"):
                platform_name = existing_session_id.split(":")[0]
                break

        session_id = f"{platform_name}:{message_type}:{target_id}"
        logger.debug(
            f"[主动消息] 为{type_description} {display_name} 设置自动触发器喵: {session_id}"
        )
        # 优化：在外层函数统一打印完整的日志信息，内层函数静默执行，避免重复
        auto_trigger_minutes = auto_trigger_settings.get("auto_trigger_after_minutes", 5)
        logger.info(
            f"[主动消息] 已为 {type_description} {display_name} 设置自动触发器喵，"
            f"将在 {auto_trigger_minutes} 分钟后检查是否需要自动触发喵。"
        )
        await self._setup_auto_trigger(session_id, silent=True)
        return 1

    # --- v1.0.0-beta.1 架构重构: 配置获取 ---

    def _get_session_config(self, umo: str) -> dict | None:
        """
        根据统一消息来源(umo)获取对应的会话配置。

        v1.0.0-beta.7 重构: 支持多会话配置，先检查个性化配置，再检查全局配置。

        配置优先级：
        1. 个性化会话配置（私聊/群聊槽位）
        2. 全局配置中的session_list匹配
        3. 全局配置的默认设置

        返回值：配置字典（如果找到且启用）或None（如果未找到或禁用）
        """
        # 解析umo字符串格式
        parts = umo.split(":")
        if len(parts) < 3:
            return None

        message_type = parts[1]  # 第二部分是消息类型
        target_id = parts[-1]  # 最后一部分是用户ID或群ID

        # 根据消息类型分别处理
        if message_type == "FriendMessage":
            return self._get_private_session_config(umo, target_id)
        elif message_type == "GroupMessage":
            return self._get_group_session_config(umo, target_id)

        return None

    def _get_private_session_config(self, umo: str, target_id: str) -> dict | None:
        """获取私聊会话配置"""
        # 1. 检查个性化配置槽位
        private_sessions = self.config.get("private_sessions", {})
        for session_key in [
            "session_1",
            "session_2",
            "session_3",
            "session_4",
            "session_5",
        ]:
            session_config = private_sessions.get(session_key, {})
            if (
                session_config.get("enable", False)
                and session_config.get("session_id", "") == target_id
            ):
                # 添加会话名称到配置中，用于日志显示
                config_copy = session_config.copy()
                config_copy["_session_name"] = session_config.get("session_name", "")
                config_copy["_session_type"] = "private"
                return config_copy

        # 2. 检查全局配置的session_list（严格模式：必须明确在list中）
        private_settings = self.config.get("private_settings", {})
        if not private_settings.get("enable", False):
            return None

        session_list = private_settings.get("session_list", [])
        if target_id in session_list:
            # 只有明确在list中的才提供服务
            config_copy = private_settings.copy()
            config_copy["_session_type"] = "private"
            config_copy["_from_session_list"] = True
            return config_copy

        return None

    def _get_group_session_config(self, umo: str, target_id: str) -> dict | None:
        """获取群聊会话配置"""
        # 1. 检查个性化配置槽位
        group_sessions = self.config.get("group_sessions", {})
        for session_key in [
            "session_1",
            "session_2",
            "session_3",
            "session_4",
            "session_5",
        ]:
            session_config = group_sessions.get(session_key, {})
            if (
                session_config.get("enable", False)
                and session_config.get("session_id", "") == target_id
            ):
                # 添加会话名称到配置中，用于日志显示
                config_copy = session_config.copy()
                config_copy["_session_name"] = session_config.get("session_name", "")
                config_copy["_session_type"] = "group"
                return config_copy

        # 2. 检查全局配置的session_list（严格模式：必须明确在list中）
        group_settings = self.config.get("group_settings", {})
        if not group_settings.get("enable", False):
            return None

        session_list = group_settings.get("session_list", [])
        if target_id in session_list:
            # 只有明确在list中的才提供服务
            config_copy = group_settings.copy()
            config_copy["_session_type"] = "group"
            config_copy["_from_session_list"] = True
            return config_copy

        return None

    # --- 核心调度逻辑 ---

    async def _init_jobs_from_data(self):
        """
        从已加载的 session_data 中恢复定时任务。

        这是插件重启恢复机制的核心函数，负责：
        1. 遍历所有保存的会话数据
        2. 检查每个会话的配置是否有效且启用
        3. 验证定时任务是否过期（给予1分钟宽限期）
        4. 恢复未过期的定时任务到调度器
        5. 记录恢复统计信息

        特别处理：区分私聊和群聊的不同恢复逻辑
        - 私聊：使用APScheduler定时任务
        - 群聊：使用沉默倒计时机制（不在这里恢复）

        这个函数确保了插件重启后能够无缝继续之前的工作状态。
        """
        restored_count = 0
        current_time = time.time()

        # 增强调试信息
        logger.info(
            f"[主动消息] 开始从数据恢复定时任务喵，当前时间: {datetime.fromtimestamp(current_time)}"
        )
        # 首先清理无效的会话数据
        cleaned_count = self._cleanup_invalid_session_data()
        if cleaned_count > 0:
            logger.info(f"[主动消息] 清理了 {cleaned_count} 个无效的会话数据条目喵。")
            # 立即保存清理后的数据
            async with self.data_lock:
                self._save_data_internal()

        logger.info(f"[主动消息] 会话数据条目数: {len(self.session_data)}")

        for session_id, session_info in self.session_data.items():
            # v1.0.0-beta.1 架构重构: 检查此会话是否有对应的配置
            session_config = self._get_session_config(session_id)

            # 增强调试信息，包含会话名称
            session_name = (
                session_config.get("_session_name", "") if session_config else ""
            )
            session_desc = f"({session_name})" if session_name else ""
            logger.debug(f"[主动消息] 检查会话喵 {session_id}{session_desc}:")
            logger.debug(f"[主动消息] 会话信息喵: {session_info}")
            logger.debug(f"[主动消息] 配置有效性喵: {session_config is not None}")
            if session_config:
                logger.debug(
                    f"[主动消息] 配置启用状态: {session_config.get('enable', False)}"
                )
                # 显示配置来源
                if session_config.get("_from_session_list"):
                    logger.debug("[主动消息] 配置来源: 全局session_list匹配")
                elif session_config.get("_from_global"):
                    logger.debug("[主动消息] 配置来源: 全局默认配置")
                else:
                    logger.debug("[主动消息] 配置来源: 个性化配置槽位")

            if not session_config or not session_config.get("enable", False):
                logger.debug(
                    f"[主动消息]   会话 {session_id}{session_desc} 配置无效或未启用，跳过喵"
                )
                continue

            next_trigger = session_info.get("next_trigger_time")
            logger.debug(f"[主动消息] next_trigger_time: {next_trigger} 喵")

            # v1.0.0-beta.1 修复: 修正任务恢复逻辑，避免过早清理数据导致无法恢复
            if next_trigger:
                # 检查任务是否过期（给1分钟的宽限期）
                trigger_time_with_grace = next_trigger + 60
                is_not_expired = current_time < trigger_time_with_grace

                logger.debug("[主动消息] 任务时间检查喵:")
                logger.debug(
                    f"[主动消息] 原始触发时间: {datetime.fromtimestamp(next_trigger)} 喵"
                )
                logger.debug(
                    f"[主动消息] 宽限期后时间: {datetime.fromtimestamp(trigger_time_with_grace)} 喵"
                )
                logger.debug(f"[主动消息] 是否未过期喵: {is_not_expired}")

                if is_not_expired:
                    try:
                        run_date = datetime.fromtimestamp(
                            next_trigger, tz=self.timezone
                        )

                        # 检查是否已存在相同任务，避免重复
                        existing_job = self.scheduler.get_job(session_id)
                        if existing_job:
                            logger.debug(
                                f"[主动消息] 会话 {session_id} 的任务已存在，跳过恢复喵。"
                            )
                            continue

                        self.scheduler.add_job(
                            self.check_and_chat,
                            "date",
                            run_date=run_date,
                            args=[session_id],
                            id=session_id,
                            replace_existing=True,
                            misfire_grace_time=60,
                        )
                        # v1.0.0-beta.7 新增: 显示会话名称
                        session_name = session_config.get("_session_name", "")
                        session_desc = f"({session_name})" if session_name else ""
                        logger.info(
                            f"[主动消息] 已成功从文件恢复任务喵: {session_id} {session_desc}, 执行时间: {run_date} 喵"
                        )
                        restored_count += 1
                    except Exception as e:
                        logger.error(
                            f"[主动消息] 添加恢复任务 '{session_id}' 到调度器时失败喵: {e}"
                        )
                else:
                    # 任务已过期，记录日志但不清理数据
                    # v1.0.0-beta.7 新增: 显示会话名称
                    session_name = session_config.get("_session_name", "")
                    session_desc = f"({session_name})" if session_name else ""
                    logger.info(
                        f"[主动消息] 会话 {session_id} {session_desc} 的任务已过期，跳过恢复喵。"
                    )
                    logger.debug(
                        f"[主动消息] 触发时间: {datetime.fromtimestamp(next_trigger)} 喵"
                    )
                    logger.debug(
                        f"[主动消息] 当前时间: {datetime.fromtimestamp(current_time)} 喵"
                    )
                    logger.debug("[主动消息] 宽限期: 60秒喵")
                    # 不要清理数据，让正常流程处理过期的任务
            else:
                logger.debug(
                    f"[主动消息] 会话 {session_id} 没有next_trigger_time，跳过喵"
                )

        logger.info(
            f"[主动消息] 任务恢复检查完成，共恢复 {restored_count} 个定时任务喵。"
        )
        if restored_count == 0:
            logger.info("[主动消息] 没有需要恢复的定时任务喵。")

    async def _schedule_next_chat_and_save(
        self, session_id: str, reset_counter: bool = False
    ):
        """
        安排下一次主动聊天并立即将状态持久化到文件。

        这是调度逻辑的核心函数，负责：
        1. 根据配置计算下一次触发时间（在最小和最大间隔之间随机选择）
        2. 创建新的定时任务并添加到调度器
        3. 更新会话数据中的触发时间
        4. 立即保存数据到文件（确保重启后任务不丢失）
        5. 处理计数器重置（当用户回复时）

        参数：
        - session_id: 目标会话ID
        - reset_counter: 是否重置未回复计数器（用户回复时设为True）

        这个函数是"原子操作"，确保调度决策能够持久化保存。
        """
        session_config = self._get_session_config(session_id)
        if not session_config:
            return

        schedule_conf = session_config.get("schedule_settings", {})

        async with self.data_lock:
            if reset_counter:
                self.session_data.setdefault(session_id, {})["unanswered_count"] = 0

            min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
            max_interval = max(
                min_interval, int(schedule_conf.get("max_interval_minutes", 900)) * 60
            )
            random_interval = random.randint(min_interval, max_interval)
            next_trigger_time = time.time() + random_interval
            run_date = datetime.fromtimestamp(next_trigger_time, tz=self.timezone)

            self.scheduler.add_job(
                self.check_and_chat,
                "date",
                run_date=run_date,
                args=[session_id],
                id=session_id,
                replace_existing=True,
                misfire_grace_time=60,
            )

            self.session_data.setdefault(session_id, {})["next_trigger_time"] = (
                next_trigger_time
            )
            logger.info(
                f"[主动消息] 已为会话 {session_id} 安排下一次主动聊天喵，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵。"
            )

            await self._save_data_internal()

    # --- 事件监听 ---

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=999)
    async def on_private_message(self, event: AstrMessageEvent):
        """监听私聊消息，取消旧任务，并重置计时器和计数器。"""
        # v1.0.0-beta.1 修复: 不再只检查 message_str，而是检查整个消息链，以正确响应图片等富媒体消息
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin
        logger.debug(f"[主动消息] 收到私聊消息喵，会话ID: {session_id}")

        # v1.0.0-beta.2 新增: 记录消息时间并取消自动触发
        # v1.0.0-beta.7 修复: 只记录插件启动后的消息时间，用于自动触发功能
        current_time = time.time()
        self.last_message_times[session_id] = current_time
        
        # 持久化插件启动后的消息时间，确保插件重载后能恢复状态
        async with self.data_lock:
            # 只保存插件启动后的消息时间
            if current_time >= self.plugin_start_time:
                self.session_data.setdefault(session_id, {})["last_message_time"] = current_time
                logger.debug(f"[主动消息] 已记录私聊消息时间喵（插件启动后）: {session_id} -> {current_time}")
            else:
                logger.debug(f"[主动消息] 忽略插件启动前的私聊旧消息喵: {session_id} -> {current_time}")

        # 尝试取消自动触发 - 支持多种会话ID格式
        await self._cancel_auto_trigger(session_id)

        # 同时尝试取消基于FriendMessage格式的触发器（为了兼容初始化时的设置）
        try:
            # 从会话ID中提取平台名称和用户ID部分
            if ":" in session_id:
                parts = session_id.split(":")
                if len(parts) >= 3:  # platform:type:id 格式
                    platform_name = parts[0]  # 第一部分是平台名称
                    user_id = parts[-1]  # 最后一部分是用户ID
                    friend_message_session_id = (
                        f"{platform_name}:FriendMessage:{user_id}"
                    )
                    await self._cancel_auto_trigger(friend_message_session_id)
        except Exception as e:
            logger.debug(
                f"[主动消息] 尝试取消FriendMessage格式触发器时出错喵（可忽略）: {e}"
            )

        # 只打印一次日志，避免刷屏，且只针对配置的会话
        session_config = self._get_session_config(session_id)
        if session_config and session_config.get("enable", False):
            if session_id not in self.first_message_logged:
                self.first_message_logged.add(session_id)
                # v1.0.0-beta.7 新增: 显示会话名称
                session_name = session_config.get("_session_name", "")
                session_desc = f"({session_name})" if session_name else ""
                logger.info(
                    f"[主动消息] 已记录私聊消息时间并取消自动触发喵，会话ID: {session_id} {session_desc}"
                )
        # 后续消息不再打印日志，保持简洁

        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            logger.info(f"[主动消息] 会话 {session_id} 未启用或配置无效，跳过处理喵。")
            return

        # v1.0.0-beta.1 修复: 在重新调度前，先尝试取消任何已存在的、由 APScheduler 设置的定时任务
        try:
            self.scheduler.remove_job(session_id)
            # v1.0.0-beta.7 新增: 显示会话名称
            session_name = session_config.get("_session_name", "")
            session_desc = f"({session_name})" if session_name else ""
            logger.info(
                f"[主动消息] 用户已回复喵，已取消会话 {session_id} {session_desc} 的预定主动消息任务喵。"
            )
        except Exception:  # JobLookupError
            pass  # 如果任务不存在，说明是正常情况，无需处理

        # 重要：只重置当前会话的计数器，不影响其他会话
        # v1.0.0-beta.7 新增: 显示会话名称
        session_name = session_config.get("_session_name", "")
        session_desc = f"({session_name})" if session_name else ""
        logger.info(
            f"[主动消息] 重置会话 {session_id} {session_desc} 的未回复计数器为0喵。"
        )
        await self._schedule_next_chat_and_save(session_id, reset_counter=True)

    # v1.0.0-beta.1 新增: 群聊消息监听与智能触发
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=998)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群聊消息流，重置沉默倒计时，并取消已计划的主动消息任务。"""
        # v1.0.0-beta.1 修复: 响应所有类型的消息，而不仅仅是文本消息
        if not event.get_messages():
            return

        session_id = event.unified_msg_origin

        # v1.0.0-beta.4 修复: 使用会话隔离的状态管理，避免竞态条件
        current_time = time.time()
        self.session_temp_state[session_id] = {"last_user_time": current_time}
        logger.debug(
            f"[主动消息] 记录用户消息时间戳喵: {current_time}, 会话ID: {session_id}"
        )

        # v1.0.0-beta.2 新增: 记录消息时间并取消自动触发
        self.last_message_times[session_id] = current_time
        
        # v1.0.0-beta.7 修复: 只持久化插件启动后的消息时间，用于自动触发功能
        async with self.data_lock:
            # 只保存插件启动后的消息时间
            if current_time >= self.plugin_start_time:
                self.session_data.setdefault(session_id, {})["last_message_time"] = current_time
                logger.debug(f"[主动消息] 已记录群聊消息时间喵（插件启动后）: {session_id} -> {current_time}")
            else:
                logger.debug(f"[主动消息] 忽略插件启动前的群聊旧消息用于自动主动消息任务喵: {session_id} -> {current_time}")
        await self._cancel_auto_trigger(session_id)

        # 同时尝试取消基于GroupMessage格式的触发器（为了兼容初始化时的设置）
        try:
            # 从会话ID中提取平台名称和群ID部分
            if ":" in session_id:
                parts = session_id.split(":")
                if len(parts) >= 3:  # platform:type:id 格式
                    platform_name = parts[0]  # 第一部分是平台名称
                    group_id = parts[-1]  # 最后一部分是群ID
                    group_message_session_id = (
                        f"{platform_name}:GroupMessage:{group_id}"
                    )
                    await self._cancel_auto_trigger(group_message_session_id)
        except Exception as e:
            logger.debug(
                f"[主动消息] 尝试取消GroupMessage格式触发器时出错喵（可忽略）: {e}"
            )

        # 只打印一次日志，避免刷屏，且只针对配置的会话
        session_config = self._get_session_config(session_id)
        if session_config and session_config.get("enable", False):
            if session_id not in self.first_message_logged:
                self.first_message_logged.add(session_id)
                # v1.0.0-beta.7 新增: 显示会话名称
                session_name = session_config.get("_session_name", "")
                session_desc = f"({session_name})" if session_name else ""
                logger.info(
                    f"[主动消息] 已记录群聊消息时间并取消自动触发喵，会话ID: {session_id}{session_desc}"
                )
        # 后续消息不再打印日志，保持简洁

        # v1.0.0-beta.1 注释: Bot消息检测已迁移到after_message_sent事件
        # 旧的on_group_message中的Bot检测逻辑已被移除，避免重复检测
        # 现在通过on_after_message_sent专门处理Bot发送的消息
        sender_id = None
        try:
            # 只获取发送者ID用于日志记录，不再进行Bot检测
            if hasattr(event, "message_obj") and event.message_obj:
                if hasattr(event.message_obj, "sender") and event.message_obj.sender:
                    sender_id = getattr(
                        event.message_obj.sender, "id", None
                    ) or getattr(event.message_obj.sender, "user_id", None)

            if not sender_id:
                sender_id = getattr(event, "user_id", None) or getattr(
                    event, "sender_id", None
                )

        except Exception as e:
            logger.debug(f"[主动消息] 获取发送者ID失败喵: {e}")

        # 简化日志：只记录用户消息检测，Bot检测由after_message_sent处理
        logger.debug(
            f"[主动消息] 收到用户消息喵，会话ID: {session_id}, 发送者ID: {sender_id}"
        )

        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            logger.debug(f"[主动消息] 会话 {session_id} 未启用或配置无效，跳过处理喵。")
            return

        # v1.0.0-beta.1 修复: 群聊活跃时取消已预定的 APScheduler 任务
        # 注意：这里不再区分Bot消息和用户消息，因为Bot消息检测已迁移到after_message_sent
        try:
            self.scheduler.remove_job(session_id)
            logger.info(
                f"[主动消息] 群聊活跃喵，已取消会话 {session_id} 的预定主动消息任务喵。"
            )
        except Exception as e:  # JobLookupError
            logger.debug(f"[主动消息] 会话 {session_id} 没有待取消的调度任务喵: {e}")

        # v1.0.0-beta.1 架构重构: 将重置沉默倒计时的逻辑，提取到一个可复用的函数中
        # 无论是用户消息还是Bot消息，都应该重置沉默倒计时
        # v1.0.0-beta.1 修复: 群聊用户发言时也应该重置未回复计数器
        logger.debug(f"[主动消息] 准备重置群聊沉默倒计时喵，会话ID: {session_id}")
        await self._reset_group_silence_timer(session_id)

        # 重要修复：群聊用户发言时也应该重置未回复计数器，与私聊保持一致
        # 每个会话(私聊/群聊)有独立的session_id和数据，不会相互影响
        # v1.0.0-beta.1 修复: 现在只处理用户消息，Bot消息检测已迁移到after_message_sent
        async with self.data_lock:
            if session_id in self.session_data:
                current_unanswered = self.session_data[session_id].get(
                    "unanswered_count", 0
                )
                self.session_data[session_id]["unanswered_count"] = 0
                if current_unanswered > 0:
                    logger.debug(
                        f"[主动消息] 群聊用户已回复，会话 {session_id} 未回复计数器已重置喵。"
                    )

                # v1.0.0-beta.1 修复: 清理已作废的定时任务数据，避免重复恢复
                # 重要：只清理群聊的定时任务数据，因为群聊使用沉默倒计时机制
                # 私聊使用APScheduler，不应该在这里清理
                if (
                    "group" in session_id.lower()
                    and "next_trigger_time" in self.session_data[session_id]
                ):
                    del self.session_data[session_id]["next_trigger_time"]
                    logger.debug(
                        f"[主动消息] 因群聊活跃，清理会话 {session_id} 中已作废的定时任务数据喵。"
                    )

    # v1.0.0-beta.1 新增: 监听Bot消息发送事件
    # 这个监听器专门用于检测Bot自己发送的消息，解决了之前Bot消息无法被正确识别的问题
    # 通过after_message_sent事件，我们可以在Bot发送消息后立即得到通知
    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """
        监听消息发送后事件，检测Bot自己发送的消息。
        这是v1.0.0-beta.1版本的核心改进之一，通过多重检测机制准确识别Bot消息：
        1. 时间窗口检测：Bot回复通常在用户消息5秒内
        2. source属性检测：检查消息来源标识
        3. ID匹配检测：对比self_id和user_id

        检测到Bot消息后，会重置群聊沉默倒计时，确保时序正确性。
        """
        session_id = event.unified_msg_origin

        # 只关注群聊消息
        if "group" not in session_id.lower():
            return

        # 简化但有效的Bot消息检测 - 基于之前成功的经验
        logger.debug(f"[主动消息] after_message_sent事件触发喵，会话ID: {session_id}")

        is_bot_message = False
        current_time = time.time()

        # v1.0.0-beta.6 修复 (内存泄漏): 定期清理过期的会话状态，防止内存泄漏
        # 每10次调用清理一次
        if hasattr(self, "_cleanup_counter"):
            self._cleanup_counter += 1
        else:
            self._cleanup_counter = 1

        if self._cleanup_counter % 10 == 0:
            self._cleanup_expired_session_states(current_time)

        try:
            # 核心检测逻辑1: 时间窗口检测（最可靠）- v1.0.0-beta.4修复：使用会话隔离状态
            session_state = self.session_temp_state.get(session_id, {})
            last_user_time = session_state.get("last_user_time", 0)
            time_since_user = current_time - last_user_time
            if (
                last_user_time > 0 and time_since_user < 5.0  # 5秒时间窗口
            ):
                is_bot_message = True
                logger.debug(
                    f"[主动消息] 🎯 检测到Bot消息喵！时间窗口: {time_since_user:.2f}秒，会话ID: {session_id}"
                )

            # 核心检测逻辑2: source属性检测
            elif hasattr(event, "source") and event.source:
                source = str(event.source).lower()
                if source in ["self", "bot", "assistant"]:
                    is_bot_message = True
                    logger.info(
                        f"[主动消息] ✅ 检测到Bot消息喵！source: {source}，会话ID: {session_id}"
                    )

            # 核心检测逻辑3: 简单的ID匹配
            elif hasattr(event, "self_id") and hasattr(event, "user_id"):
                if str(event.self_id) == str(event.user_id):
                    is_bot_message = True
                    logger.info(
                        f"[主动消息] ✅ 检测到Bot消息喵！self_id == user_id: {event.self_id}，会话ID: {session_id}"
                    )

            if is_bot_message:
                # 重置沉默倒计时
                await self._reset_group_silence_timer(session_id)
                # 清理会话状态 - v1.0.0-beta.4修复：使用会话隔离状态管理
                if session_id in self.session_temp_state:
                    del self.session_temp_state[session_id]
            else:
                logger.debug(f"[主动消息] 未检测到Bot消息喵，会话ID: {session_id}")

        except Exception as e:
            logger.error(
                f"[主动消息] after_message_sent 检测异常喵: {e}, 会话ID: {session_id}"
            )

    # v1.0.0-beta.1 新增: 群聊沉默倒计时重置器
    async def _reset_group_silence_timer(self, session_id: str):
        """
        重置指定群聊的"沉默倒计时"。
        这是群聊主动消息机制的核心函数，工作原理：
        1. 取消该群聊现有的沉默计时器（如果有）
        2. 根据配置创建新的计时器，设置指定的沉默时间
        3. 当计时器到期时，会触发主动消息任务创建

        无论是用户发言还是Bot自己发言，都会调用此函数重置倒计时，
        确保只有在群聊真正沉默时才发送主动消息。
        """
        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            logger.debug(f"[主动消息] 会话 {session_id} 未启用，跳过重置沉默倒计时喵。")
            return

        # 取消上一个为该群聊设置的"沉默倒计时"
        if session_id in self.group_timers:
            try:
                self.group_timers[session_id].cancel()
                logger.debug(f"[主动消息] 已取消 {session_id} 的上一个沉默倒计时喵。")
            except Exception as e:
                logger.warning(f"[主动消息] 取消 {session_id} 的旧计时器时出错喵: {e}")
            finally:
                del self.group_timers[session_id]

        # 获取沉默触发时间
        idle_minutes = session_config.get("group_idle_trigger_minutes", 10)
        logger.debug(
            f"[主动消息] 将为 {session_id} 设置 {idle_minutes} 分钟的沉默倒计时喵。"
        )

        # 定义倒计时结束后的回调函数 - 修复闭包变量捕获问题
        def _schedule_callback(captured_session_id=session_id):
            try:
                # v1.0.0-beta.1 修复: 在创建任务前，验证群聊是否仍然处于沉默状态
                # 重要：倒计时结束时，需要检查群聊是否仍然值得发送主动消息

                # 检查1: 验证当前是否还有活跃的计时器（如果群聊活跃，计时器应该被重置）
                if captured_session_id not in self.group_timers:
                    logger.info(
                        f"[主动消息] 群聊 {captured_session_id} 的计时器已被重置，跳过主动消息创建喵。"
                    )
                    return

                # v1.0.0-beta.3 修复：
                # 检查2: 验证会话数据是否存在，如果不存在则创建初始数据
                if captured_session_id not in self.session_data:
                    logger.info(
                        f"[主动消息] 群聊 {captured_session_id} 的会话数据不存在，创建初始会话数据喵。"
                    )
                    # 为新会话创建初始数据
                    self.session_data[captured_session_id] = {"unanswered_count": 0}

                # 检查3: 验证配置是否仍然启用
                current_config = self._get_session_config(captured_session_id)
                if not current_config or not current_config.get("enable", False):
                    logger.info(
                        f"[主动消息] 群聊 {captured_session_id} 的配置已禁用或不存在，跳过主动消息创建喵。"
                    )
                    return

                # v1.0.0-beta.1 修复: 当群聊沉默时，不应该重置计数器。reset_counter 必须为 False。
                # 这个回调是在主事件循环中被调用的，所以我们可以安全地创建异步任务
                # 获取当前的未回复次数，用于显示更准确的日志
                current_unanswered = self.session_data.get(captured_session_id, {}).get(
                    "unanswered_count", 0
                )
                asyncio.create_task(
                    self._schedule_next_chat_and_save(captured_session_id, reset_counter=False)
                )
                logger.info(
                    f"[主动消息] 群聊 {captured_session_id} 已沉默 {idle_minutes} 分钟，开始计划主动消息喵。(当前未回复次数: {current_unanswered})"
                )
            except Exception as e:
                logger.error(f"[主动消息] 沉默倒计时回调函数执行失败喵: {e}")

        # 设置一个新的"沉默倒计时"
        try:
            loop = asyncio.get_running_loop()
            self.group_timers[session_id] = loop.call_later(
                idle_minutes * 60, _schedule_callback
            )
            logger.debug(
                f"[主动消息] 已重置 {session_id} 的沉默倒计时 ({idle_minutes}分钟) 喵。当前计时器数量: {len(self.group_timers)}"
            )
        except Exception as e:
            logger.error(f"[主动消息] 设置沉默倒计时失败喵: {e}")

    # --- v0.9.95 优化: `check_and_chat` 函数重构 ---

    async def _is_chat_allowed(self, session_id: str) -> bool:
        """
        检查是否允许进行主动聊天（条件检查）。

        这是主动消息发送前的必要检查，包括：
        1. 检查插件是否在该会话中启用
        2. 检查当前时间是否在免打扰时段内

        返回值：
        - True: 允许进行主动聊天
        - False: 不允许进行主动聊天（插件禁用或免打扰时段）

        这个函数是纯查询函数，不包含任何副作用。
        重新调度的逻辑由调用方根据返回值决定。

        v1.0.0-beta.6 修复 (函数副作用): 重新调度逻辑移至调用方处理
        """
        session_config = self._get_session_config(session_id)
        if not session_config or not session_config.get("enable", False):
            return False

        schedule_conf = session_config.get("schedule_settings", {})
        if is_quiet_time(schedule_conf.get("quiet_hours", "1-7"), self.timezone):
            logger.info("[主动消息] 当前为免打扰时段喵。")
            return False

        return True

    async def _prepare_llm_request(self, session_id: str) -> dict | None:
        """
        准备 LLM 请求所需的上下文、人格和最终 Prompt。

        基于最新文档优化，简化人格和上下文获取逻辑。

        返回值：
        - dict: 包含conv_id、history、system_prompt的请求包
        - None: 如果准备失败
        """
        try:
            # 获取当前会话的对话ID
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(
                session_id
            )
            if not conv_id:
                logger.warning(
                    f"[主动消息] 无法找到会话 {session_id} 的当前对话ID，可能是新会话，跳过本次任务喵。"
                )
                return None

            # 获取对话对象
            conversation = await self.context.conversation_manager.get_conversation(
                session_id, conv_id
            )

            # 获取对话历史（简化处理，直接使用字符串历史）
            pure_history_messages = []
            if conversation and conversation.history:
                try:
                    # 尝试解析JSON格式的历史记录
                    if isinstance(conversation.history, str):
                        pure_history_messages = await asyncio.to_thread(
                            json.loads, conversation.history
                        )
                    else:
                        pure_history_messages = conversation.history
                except (json.JSONDecodeError, TypeError):
                    logger.warning("[主动消息] 解析历史记录失败，使用空历史喵。")

            # 获取人格设定（使用新的人格管理器API）
            original_system_prompt = ""

            # 优先使用会话绑定的persona
            if conversation and conversation.persona_id:
                persona = await self.context.persona_manager.get_persona(
                    conversation.persona_id
                )
                if persona:
                    original_system_prompt = persona.system_prompt
                    logger.info(
                        f"[主动消息] 使用会话人格: '{conversation.persona_id}' 喵"
                    )

            # 如果没有会话persona，使用默认persona
            if not original_system_prompt:
                default_persona = (
                    await self.context.persona_manager.get_default_persona_v3(
                        umo=session_id
                    )
                )
                if default_persona:
                    original_system_prompt = default_persona["prompt"]
                    logger.info("[主动消息] 使用默认人格设定喵")

            if not original_system_prompt:
                logger.error(
                    "[主动消息] 呜喵？！关键错误喵：无法加载任何人格设定，放弃喵。"
                )
                return None

            logger.info(
                f"[主动消息] 成功加载上下文喵: 共 {len(pure_history_messages)} 条历史消息喵。"
            )

            return {
                "conv_id": conv_id,
                "history": pure_history_messages,
                "system_prompt": original_system_prompt,
            }

        except Exception as e:
            logger.warning(f"[主动消息] 获取上下文或人格失败喵: {e}")
            return None

    async def _send_proactive_message(self, session_id: str, text: str):
        """
        负责处理主动消息的发送逻辑，包括TTS语音和文本消息。

        发送流程：
        1. 检查TTS配置，如果启用则尝试生成语音
        2. 如果TTS成功，发送语音消息
        3. 根据配置决定是否同时发送文本原文
        4. 如果TTS失败或禁用，直接发送文本消息

        特别处理：如果是群聊消息，发送后会立即重置沉默倒计时，
        因为Bot发送消息也意味着群聊有活动。
        """
        session_config = self._get_session_config(session_id)
        if not session_config:
            logger.info(f"[主动消息] 无法获取会话配置，跳过消息发送喵: {session_id}")
            return

        logger.info(f"[主动消息] 开始发送主动消息喵，会话ID: {session_id}")

        tts_conf = session_config.get("tts_settings", {})
        is_tts_sent = False
        if tts_conf.get("enable_tts", True):
            try:
                logger.info("[主动消息] 尝试进行手动 TTS 喵。")
                tts_provider = self.context.get_using_tts_provider(umo=session_id)
                if tts_provider:
                    audio_path = await tts_provider.get_audio(text)
                    if audio_path:
                        await self.context.send_message(
                            session_id, MessageChain([Record(file=audio_path)])
                        )
                        is_tts_sent = True
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[主动消息] 手动 TTS 流程发生异常喵: {e}")

        if not is_tts_sent or tts_conf.get("always_send_text", True):
            await self.context.send_message(
                session_id, MessageChain([Plain(text=text)])
            )

        # v1.0.0-beta.1 修复: Bot 自己发送的消息，也应该被视为一次"活动"，重置群聊的沉默倒计时
        # 注意：这里需要立即重置沉默倒计时，因为Bot发送消息意味着群聊有活动
        if "group" in session_id.lower():
            # 立即重置，不要等待，确保时序正确
            await self._reset_group_silence_timer(session_id)
            logger.info(
                f"[主动消息] Bot主动消息已发送，已重置群聊 {session_id} 的沉默倒计时喵。"
            )

            # v1.0.0-beta.1 修复: 记录Bot发送消息的时间，用于辅助检测Bot消息
            # 有些平台下self_id可能获取不到，我们可以通过发送时间来辅助判断
            self.last_bot_message_time = time.time()
            # 注意：群聊的next_trigger_time在任务成功完成后会被清理，这是正确的行为
            # 因为群聊使用监控沉默倒计时与APScheduler结合的机制，而不是固定的APScheduler任务

    async def _finalize_and_reschedule(
        self,
        session_id: str,
        conv_id: str,
        user_prompt: str,
        assistant_response: str,
        unanswered_count: int,
    ):
        """
        负责主动消息任务完成后的收尾工作，包括：
        1. 存档对话历史（使用add_message_pair）
        2. 更新未回复计数器
        3. 重新调度下一个任务（仅私聊）
        4. 保存所有状态到持久化存储

        v1.0.0-beta.1 重要更新：此函数现在区分私聊和群聊的处理逻辑：
        - 私聊：立即重新调度下一个主动消息任务
        - 群聊：清理定时任务数据，使用沉默倒计时机制
        """
        # 1. 使用新的对话管理API存档对话历史
        try:
            # v1.0.0-beta.2 优化: 使用预导入的TextPart，避免重复导入
            user_msg_obj = UserMessageSegment(content=[TextPart(text=user_prompt)])
            assistant_msg_obj = AssistantMessageSegment(
                content=[TextPart(text=assistant_response)]
            )

            await self.context.conversation_manager.add_message_pair(
                cid=conv_id,
                user_message=user_msg_obj,
                assistant_message=assistant_msg_obj,
            )
            logger.info(
                "[主动消息] 已成功将本次主动对话存档至对话历史喵 (使用新的add_message_pair API)。"
            )
        except Exception as e:
            logger.error(f"[主动消息] 存档对话历史失败喵: {e}")
            # v1.0.0-beta.2 优化: 存档失败时不中断主流程，只记录错误
            logger.warning("[主动消息] 对话存档失败喵，但会继续执行后续步骤喵。")

        # 2. 然后再获取锁，执行关键区代码（AI审查建议：优化锁的使用范围）
        async with self.data_lock:
            # 更新计数器 (对私聊和群聊都适用)
            # v1.0.0-beta.1 修复: 计数器逻辑
            # 只有在Bot成功发送消息给用户后，才增加未回复计数器
            # 每个会话(私聊/群聊)都有独立的计数器，不会相互影响
            new_unanswered_count = unanswered_count + 1
            self.session_data.setdefault(session_id, {})["unanswered_count"] = (
                new_unanswered_count
            )
            logger.info(
                f"[主动消息] 会话 {session_id} 的第 {new_unanswered_count} 次主动消息已发送完成，当前未回复次数: {new_unanswered_count} 次喵。"
            )

            # 重新调度 (v1.0.0-beta.1 修复: 只对私聊进行立即的、连续的重新调度)
            if "private" in session_id.lower() or "friendmessage" in session_id.lower():
                session_config = self._get_session_config(session_id)
                if not session_config:
                    return
                schedule_conf = session_config.get("schedule_settings", {})

                min_interval = int(schedule_conf.get("min_interval_minutes", 30)) * 60
                max_interval = max(
                    min_interval,
                    int(schedule_conf.get("max_interval_minutes", 900)) * 60,
                )
                random_interval = random.randint(min_interval, max_interval)
                next_trigger_time = time.time() + random_interval
                run_date = datetime.fromtimestamp(next_trigger_time, tz=self.timezone)

                self.scheduler.add_job(
                    self.check_and_chat,
                    "date",
                    run_date=run_date,
                    args=[session_id],
                    id=session_id,
                    replace_existing=True,
                    misfire_grace_time=60,
                )

                self.session_data.setdefault(session_id, {})["next_trigger_time"] = (
                    next_trigger_time
                )
                logger.info(
                    f"[主动消息] 已为私聊会话 {session_id} 安排下一次主动聊天喵，时间：{run_date.strftime('%Y-%m-%d %H:%M:%S')} 喵。"
                )

            # 保存所有状态
            await self._save_data_internal()

    async def check_and_chat(self, session_id: str):
        """
        由定时任务触发的核心函数（车间主任），负责完成一次完整的主动消息流程。

        这是整个插件的核心调度函数，工作流程：
        1. 检查是否允许聊天（插件启用、非免打扰时段）
        2. 检查未回复次数是否达到上限
        3. 准备LLM请求（获取上下文、人格、构造Prompt）
        4. 调用LLM生成回复内容
        5. 发送生成的消息（支持TTS）
        6. 收尾工作（存档、更新计数器、重新调度）

        支持AstrBot 4.5.7+新API，同时保持向后兼容性。
        具备完善的错误处理机制，确保任务链不会中断。
        """
        try:
            if not await self._is_chat_allowed(session_id):
                # v1.0.0-beta.6 修复 (函数副作用): 将重新调度逻辑移至调用方，由调用方根据返回值决定操作
                logger.info("[主动消息] 当前为免打扰时段，跳过并重新调度喵。")
                await self._schedule_next_chat_and_save(session_id)
                return

            session_config = self._get_session_config(session_id)
            if not session_config:
                return

            schedule_conf = session_config.get("schedule_settings", {})

            async with self.data_lock:
                unanswered_count = self.session_data.get(session_id, {}).get(
                    "unanswered_count", 0
                )
                max_unanswered = schedule_conf.get("max_unanswered_times", 3)
                if max_unanswered > 0 and unanswered_count >= max_unanswered:
                    logger.info(
                        f"[主动消息] 会话 {session_id} 的未回复次数 ({unanswered_count}) 已达到上限 ({max_unanswered})，暂停主动消息喵。"
                    )
                    return

            # v1.0.0-beta.1 修复: 计数器逻辑说明
            # unanswered_count 未回复计数器
            # 0 = 还没有发送过主动消息，或用户已经回复
            # 1 = 已经发送了第1次主动消息，未回复次数1
            # 2 = 已经发送了第2次主动消息，未回复次数2，以此类推。
            logger.info(
                f"[主动消息] 开始生成第 {unanswered_count + 1} 次主动消息喵，当前未回复次数: {unanswered_count} 次喵。"
            )

            request_package = await self._prepare_llm_request(session_id)
            if not request_package:
                await self._schedule_next_chat_and_save(session_id)
                return

            conv_id = request_package["conv_id"]
            pure_history_messages = request_package["history"]
            original_system_prompt = request_package["system_prompt"]

            # v1.0.0-beta.6 修复 (并发竞态条件): 记录任务开始时的状态快照
            # 用于检测LLM调用期间是否有新消息到达
            task_start_state = {
                "last_message_time": self.last_message_times.get(session_id, 0),
                "unanswered_count": unanswered_count,
                "timestamp": time.time(),
            }
            logger.debug(
                f"[主动消息] 任务开始状态快照 - 会话: {session_id}, "
                f"最后消息时间: {task_start_state['last_message_time']}, "
                f"未回复计数: {task_start_state['unanswered_count']}"
            )

            # v4.5.7+ 优化: 使用新的统一LLM调用接口
            # 基于最新文档的推荐方式，简化API调用逻辑

            # 准备prompt模板和时间字符串
            motivation_template = session_config.get("proactive_prompt", "")
            now_str = datetime.now(self.timezone).strftime("%Y年%m月%d日 %H:%M")
            final_user_simulation_prompt = motivation_template.replace(
                "{{unanswered_count}}", str(unanswered_count)
            ).replace("{{current_time}}", now_str)

            logger.info("[主动消息] 已生成包含动机和时间的 Prompt 喵。")

            # 使用新的统一LLM调用接口
            llm_response_obj = None
            try:
                # 获取当前会话使用的LLM提供商ID（v4.5.7+新API）
                provider_id = await self.context.get_current_chat_provider_id(
                    session_id
                )

                # 使用统一的llm_generate接口调用LLM
                llm_response_obj = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=final_user_simulation_prompt,
                    contexts=pure_history_messages,
                    system_prompt=original_system_prompt,
                )
                logger.info("[主动消息] 使用统一的context.llm_generate API成功喵！")

            except Exception as llm_error:
                # v1.0.0-beta.2 优化: 改进错误处理，提供更详细的错误信息
                logger.error(f"[主动消息] 新API调用失败喵: {llm_error}")
                logger.info(f"[主动消息] 错误类型喵: {type(llm_error).__name__}")
                logger.info(f"[主动消息] 错误详情喵: {str(llm_error)}")

                # 尝试使用传统方式作为回退
                try:
                    provider = self.context.get_using_provider(umo=session_id)
                    if provider:
                        llm_response_obj = await provider.text_chat(
                            prompt=final_user_simulation_prompt,
                            contexts=pure_history_messages,
                            system_prompt=original_system_prompt,
                        )
                        logger.info("[主动消息] 使用传统API回退成功喵。")
                    else:
                        logger.warning(
                            "[主动消息] 未找到 LLM Provider，放弃并重新调度喵。"
                        )
                        await self._schedule_next_chat_and_save(session_id)
                        return
                except Exception as fallback_error:
                    # v1.0.0-beta.2 优化: 记录更详细的回退失败信息
                    logger.error(f"[主动消息] 传统API回退也失败喵: {fallback_error}")
                    logger.info(
                        f"[主动消息] 回退错误类型喵: {type(fallback_error).__name__}"
                    )
                    logger.error(
                        "[主动消息] 呜喵？！LLM调用完全失败，将重新调度任务喵。"
                    )
                    await self._schedule_next_chat_and_save(session_id)
                    return

            if llm_response_obj and llm_response_obj.completion_text:
                response_text = llm_response_obj.completion_text.strip()
                logger.info(f"[主动消息] LLM 已生成文本喵: '{response_text}'。")

                # v1.0.0-beta.6 修复 (并发竞态条件): 在发送消息前检查状态一致性
                # 如果在LLM调用期间用户发送了新消息，则丢弃本次生成结果
                current_state = {
                    "last_message_time": self.last_message_times.get(session_id, 0),
                    "unanswered_count": self.session_data.get(session_id, {}).get(
                        "unanswered_count", 0
                    ),
                }

                # 检查是否有新消息到达（比较时间戳和计数器）
                has_new_message = (
                    current_state["last_message_time"]
                    > task_start_state["last_message_time"]
                    or current_state["unanswered_count"]
                    < task_start_state["unanswered_count"]
                )

                if has_new_message:
                    logger.info(
                        f"[主动消息] 检测到用户在LLM生成期间发送了新消息，丢弃本次主动消息喵。 "
                        f"任务开始时最后消息时间: {task_start_state['last_message_time']}, "
                        f"当前最后消息时间: {current_state['last_message_time']}, "
                        f"任务开始时未回复计数: {task_start_state['unanswered_count']}, "
                        f"当前未回复计数: {current_state['unanswered_count']}"
                    )
                    # 不重新调度任务，因为用户消息已经触发了新的任务创建
                    # on_private_message 已经处理了任务重新调度
                    return

                await self._send_proactive_message(session_id, response_text)

                # v0.9.96 修复: 调用新的、原子化的收尾函数
                await self._finalize_and_reschedule(
                    session_id,
                    conv_id,
                    final_user_simulation_prompt,
                    response_text,
                    unanswered_count,
                )

                # v1.0.0-beta.1 修复: 任务成功完成后，正确处理定时任务数据
                # 重要：需要区分不同情况来处理数据持久化

                # 情况1: 群聊任务 - 清理数据，因为群聊使用沉默倒计时机制
                if "group" in session_id.lower():
                    async with self.data_lock:
                        if (
                            session_id in self.session_data
                            and "next_trigger_time" in self.session_data[session_id]
                        ):
                            del self.session_data[session_id]["next_trigger_time"]
                            await self._save_data_internal()
                            logger.debug(
                                f"[主动消息] 群聊任务成功完成，清理会话 {session_id} 的定时任务数据喵。"
                            )

                # 情况2: 私聊任务 - 保留数据，因为私聊使用APScheduler定时任务
                # 私聊任务的数据应该保留，直到任务真正执行或过期
                else:
                    # v1.0.0-beta.6 修复 (数据覆盖): 安全更新数据，避免数据丢失
                    # 私聊任务保留next_trigger_time数据，用于程序重启时恢复
                    async with self.data_lock:
                        # 安全地更新数据，而不是完全替换，避免数据丢失
                        if session_id in self.session_data:
                            # 只更新必要的字段，保留其他现有数据
                            self.session_data[session_id]["unanswered_count"] = (
                                self.session_data[session_id].get("unanswered_count", 0)
                            )
                            # 确保next_trigger_time存在
                            if "next_trigger_time" not in self.session_data[session_id]:
                                self.session_data[session_id]["next_trigger_time"] = (
                                    None
                                )
                            await self._save_data_internal()
                            logger.debug(
                                f"[主动消息] 私聊任务成功完成，安全更新会话 {session_id} 的数据用于重启恢复喵。"
                            )

            else:
                logger.warning("[主动消息] LLM 调用失败或返回空内容，重新调度喵。")
                await self._schedule_next_chat_and_save(session_id)

        except Exception as e:
            # v1.0.0-beta.2 优化: 改进错误日志记录，分类处理不同类型的错误
            error_type = type(e).__name__
            error_msg = str(e)

            logger.error("[主动消息] check_and_chat 任务发生致命错误喵:")
            logger.error(f"[主动消息] 错误类型喵: {error_type}")
            logger.error(f"[主动消息] 错误信息喵: {error_msg}")
            logger.debug(f"[主动消息] 详细堆栈信息喵:\n{traceback.format_exc()}")

            # 根据错误类型进行不同的处理
            if "RateLimitError" in error_type or "quota" in error_msg.lower():
                logger.warning("[主动消息] 检测到API限制错误，将延长重试间隔喵。")
                # 可以在这里添加更长的延迟逻辑
            elif "Connection" in error_type or "Timeout" in error_type:
                logger.warning("[主动消息] 检测到连接错误，可能需要检查网络设置喵。")
            elif "Authentication" in error_type or "auth" in error_msg.lower():
                logger.error("[主动消息] 认证错误，请检查API密钥配置喵。")
                # 认证错误通常需要手动干预，可以暂停任务
                return

            # v1.0.0-beta.1 修复: 任务失败后也清理数据，避免残留
            try:
                async with self.data_lock:
                    if (
                        session_id in self.session_data
                        and "next_trigger_time" in self.session_data[session_id]
                    ):
                        del self.session_data[session_id]["next_trigger_time"]
                        await self._save_data_internal()
                        logger.debug(
                            f"[主动消息] 任务失败，清理会话 {session_id} 的定时任务数据喵。"
                        )
            except Exception as clean_e:
                logger.debug(f"[主动消息] 清理失败任务数据时出错喵: {clean_e}")

            # 错误恢复：尝试重新调度
            try:
                logger.info(f"[主动消息] 尝试重新调度会话 {session_id} 的任务喵。")
                await self._schedule_next_chat_and_save(session_id)
                logger.info(f"[主动消息] 会话 {session_id} 的任务重新调度成功喵。")
            except Exception as se:
                logger.error(f"[主动消息] 在错误处理中重新调度失败喵: {se}")
                logger.error(f"[主动消息] 会话 {session_id} 可能需要手动干预喵。")

    # v1.0.0-beta.6 新增 (内存泄漏修复): 清理过期会话状态函数
    def _cleanup_expired_session_states(self, current_time: float):
        """
        清理过期的会话状态，防止内存泄漏。

        删除超过5分钟的旧状态条目，确保内存使用保持合理。
        由于状态只在Bot回复时清理，如果Bot没有回复，状态会永久残留。
        """
        expired_sessions = []
        timeout_seconds = 300  # 5分钟超时

        for session_id, state in self.session_temp_state.items():
            last_user_time = state.get("last_user_time", 0)
            if current_time - last_user_time > timeout_seconds:
                expired_sessions.append(session_id)

        # 删除过期的会话状态
        for session_id in expired_sessions:
            del self.session_temp_state[session_id]
            logger.debug(f"[主动消息] 清理了过期的会话状态: {session_id}")


def is_quiet_time(quiet_hours_str: str, tz: zoneinfo.ZoneInfo) -> bool:
    """检查当前时间是否处于免打扰时段。"""
    try:
        start_str, end_str = quiet_hours_str.split("-")
        start_hour, end_hour = int(start_str), int(end_str)
        now = datetime.now(tz) if tz else datetime.now()
        # 处理跨天的情况 (例如 23-7) (v0.9.7 继承)
        if start_hour <= end_hour:
            return start_hour <= now.hour < end_hour
        else:
            return now.hour >= start_hour or now.hour < end_hour
    # v0.9.8 修复 (精细化异常捕获): 捕获可能发生的多种异常
    except (ValueError, TypeError):
        return False

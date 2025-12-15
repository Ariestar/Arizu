import collections
from typing import Dict, List, Optional


from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import star_handlers_registry, StarHandlerMetadata

from .draw import AstrBotHelpDrawer


@register(
    "astrbot_plugin_help", "tinker", "查看所有命令，包括插件，返回一张帮助图片", "1.1.3"
)
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.drawer = AstrBotHelpDrawer(config)

    @filter.command("helps", alias={"帮助", "菜单", "功能"})
    async def get_help(self, event: AstrMessageEvent):
        """获取插件帮助信息"""
        help_msg = self.get_all_commands()
        if not help_msg:
            yield event.plain_result("没有找到任何插件或命令")
            return
        image = self.drawer.draw_help_image(help_msg)
        yield event.chain_result([Image.fromBytes(image)])

    def get_all_commands(self) -> Dict[str, List[str]]:
        """获取所有其他插件及其命令列表, 格式为 {plugin_name: [command#desc]}"""
        # 使用 defaultdict 可以方便地向列表中添加元素
        plugin_commands: Dict[str, List[str]] = collections.defaultdict(list)
        try:
            # 获取所有插件的元数据，并且去掉未激活的
            all_stars_metadata = self.context.get_all_stars()
            all_stars_metadata = [star for star in all_stars_metadata if star.activated]
        except Exception as e:
            logger.error(f"获取插件列表失败: {e}")
            return {}  # 出错时返回空字典
        if not all_stars_metadata:
            logger.warning("没有找到任何插件")
            return {}  # 没有插件时返回空字典
        for star in all_stars_metadata:
            plugin_name = getattr(star, "name", "未知插件")
            plugin_instance = getattr(star, "star_cls", None)
            module_path = getattr(star, "module_path", None)  # 获取模块路径以供匹配
            if (
                plugin_name == "astrbot"
                or plugin_name == "astrbot_plugin_help"
                or plugin_name == "astrbot-reminder"
            ):
                # 跳过自身和核心插件
                continue
            # 进行必要的检查
            if (
                not plugin_name
                or not module_path
                or not isinstance(plugin_instance, Star)
            ):
                # 如果实例无效或名称/路径缺失，记录警告并跳过
                # 注意：这里检查了 module_path 是否存在，因为后面需要用它来匹配 handler
                logger.warning(
                    f"插件 '{plugin_name}' (模块: {module_path}) 的元数据无效或不完整，已跳过。"
                )
                continue
            # 检查插件实例是否是当前插件的实例 (排除自身)
            if plugin_instance is self:
                continue
            # 遍历所有注册的处理器
            for handler in star_handlers_registry:
                # 确保处理器元数据有效且类型正确 (虽然原始代码有 assert，这里加个检查更安全)
                if not isinstance(handler, StarHandlerMetadata):
                    continue
                # 检查此处理器是否属于当前遍历的插件 (通过模块路径匹配)
                if handler.handler_module_path != module_path:
                    continue
                command_name: Optional[str] = None
                description: Optional[str] = handler.desc  # 获取描述信息
                # 遍历处理器的过滤器，查找命令或命令组
                for filter_ in handler.event_filters:
                    if isinstance(filter_, CommandFilter):
                        command_name = filter_.command_name
                        break  # 找到一个命令即可，跳出过滤器循环
                    elif isinstance(filter_, CommandGroupFilter):
                        command_name = filter_.group_name
                        break  # 找到一个命令组即可
                # 如果找到了命令或命令组名称
                if command_name:
                    # 格式化字符串
                    if description:
                        formatted_command = f"{command_name}#{description}"
                    else:
                        # 如果没有描述，就不加 # 和后面的部分
                        formatted_command = command_name

                    # 将格式化后的命令添加到对应插件的列表中
                    # 使用 set 来避免因别名等原因导致的完全重复项（如 "/cmd1#desc" 多次出现）
                    # 如果允许重复（例如不同handler但命令和描述相同），则直接 append
                    if formatted_command not in plugin_commands[plugin_name]:
                        plugin_commands[plugin_name].append(formatted_command)
        return dict(plugin_commands)

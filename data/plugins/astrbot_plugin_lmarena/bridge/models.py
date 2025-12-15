
import os
import re
import json
from astrbot.api import logger


class ModelsManager:
    """
    负责管理 available_models.json 文件的加载、更新和提取逻辑
    """

    def __init__(self, config):
        self.conf = config
        # available_models.json 文件路径
        self.available_model_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "available_models.json"
        )
        # {modelname: {"id": ..., "type": ...}}
        self.model_map: dict[str, dict[str, str]] = {}
        # 初始化时加载一次
        self.load_model_map()

    def load_model_map(
        self, models: list[dict] | None = None
    ) -> dict[str, dict[str, str]]:
        """
        加载模型映射字典，并更新配置
        """
        if not models:
            try:
                with open(self.available_model_path, "r", encoding="utf-8") as f:
                    models = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                logger.warning("available_models.json加载失败，请检查文件")
                models = []

        self.model_map.clear()
        if models:
            for m in models:
                if not isinstance(m, dict) or "publicName" not in m or "id" not in m:
                    continue
                out_caps = m.get("capabilities", {}).get("outputCapabilities", {})
                model_type = next(
                    (
                        t
                        for t in ("video", "image", "search", "text")
                        if out_caps.get(t)
                    ),
                    "unknown",
                )

                self.model_map[m["publicName"]] = {
                    "id": m["id"],
                    "type": model_type,
                }
        return self.model_map

    def update_from_html(self, html_content: str) -> bool:
        """
        从 HTML 内容提取模型并更新 available_models.json
        """
        new_models_list = self._extract_models_from_html(html_content)
        if not new_models_list:
            logger.error("未能从 HTML 提取模型数据")
            return False

        try:
            with open(self.available_model_path, "w", encoding="utf-8") as f:
                json.dump(new_models_list, f, indent=4, ensure_ascii=False)
            logger.info(f"模型列表文件已更新，共 {len(new_models_list)} 个模型")
            self.load_model_map(new_models_list)
            return True
        except IOError as e:
            logger.error(f"写入 {self.available_model_path} 出错: {e}")
            return False

    def _extract_models_from_html(self, html_content):
        """
        从 HTML 内容中提取完整的模型JSON对象，使用括号匹配确保完整性。
        """
        models = []
        model_names = set()

        # 查找所有可能的模型JSON对象的起始位置
        for start_match in re.finditer(r'\{\\"id\\":\\"[a-f0-9-]+\\"', html_content):
            start_index = start_match.start()

            # 从起始位置开始，进行花括号匹配
            open_braces = 0
            end_index = -1

            # 优化：设置一个合理的搜索上限，避免无限循环
            search_limit = start_index + 10000  # 假设一个模型定义不会超过10000个字符

            for i in range(start_index, min(len(html_content), search_limit)):
                if html_content[i] == "{":
                    open_braces += 1
                elif html_content[i] == "}":
                    open_braces -= 1
                    if open_braces == 0:
                        end_index = i + 1
                        break

            if end_index != -1:
                # 提取完整的、转义的JSON字符串
                json_string_escaped = html_content[start_index:end_index]

                # 反转义
                json_string = json_string_escaped.replace('\\"', '"').replace(
                    "\\\\", "\\"
                )

                try:
                    model_data = json.loads(json_string)
                    model_name = model_data.get("publicName")

                    # 使用publicName去重
                    if model_name and model_name not in model_names:
                        models.append(model_data)
                        model_names.add(model_name)
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"解析提取的JSON对象时出错: {e} - 内容: {json_string[:150]}..."
                    )
                    continue
        return models

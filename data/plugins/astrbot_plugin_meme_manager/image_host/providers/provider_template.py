from ..interfaces.image_host import ImageHostInterface
from pathlib import Path
from typing import List, Dict


class ProviderTemplate(ImageHostInterface):
    """图床提供者模板类"""

    def __init__(self, config: Dict):
        self.config = config

    def upload_image(self, file_path: Path) -> Dict[str, str]:
        # 实现图床的上传逻辑
        raise NotImplementedError

    def delete_image(self, image_hash: str) -> bool:
        # 实现图床的删除逻辑
        raise NotImplementedError

    def get_image_list(self) -> List[Dict[str, str]]:
        # 实现图床的图片列表获取逻辑
        raise NotImplementedError

    def download_image(self, image_info: Dict[str, str], save_path: Path) -> bool:
        # 实现图床的下载逻辑
        raise NotImplementedError

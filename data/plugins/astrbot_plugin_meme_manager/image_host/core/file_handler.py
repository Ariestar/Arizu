from pathlib import Path
from typing import List, Dict


class FileHandler:
    """文件处理类"""

    SUPPORTED_FORMATS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def scan_local_images(self) -> List[Dict[str, str]]:
        """扫描本地图片"""
        images = []
        for file_path in self.base_dir.rglob("*"):
            if (
                file_path.is_file()
                and file_path.suffix.lower() in self.SUPPORTED_FORMATS
            ):
                # 计算相对路径
                rel_path = file_path.relative_to(self.base_dir)
                category = str(rel_path.parent).replace("\\", "/")
                if category == ".":
                    category = ""

                # 构建文件信息
                filename = rel_path.name
                file_id = str(rel_path).replace("\\", "/")

                images.append(
                    {
                        "path": str(file_path),
                        "id": file_id,  # 使用文件名作为标识
                        "filename": filename,
                        "category": category,  # 保留分类信息
                    }
                )
        return images

    def get_file_path(self, category: str, filename: str) -> Path:
        """获取文件完整路径，支持分类目录"""
        path = self.base_dir
        if category:
            path = path / category
            path.mkdir(parents=True, exist_ok=True)
        return path / filename

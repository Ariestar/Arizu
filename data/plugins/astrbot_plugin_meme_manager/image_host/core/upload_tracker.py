import json
from pathlib import Path
from typing import Dict, List, Set
import logging

logger = logging.getLogger(__name__)


class UploadTracker:
    """上传记录追踪器 - 记录已上传的文件，避免重复上传"""
    
    def __init__(self, tracker_file: Path):
        self.tracker_file = Path(tracker_file)
        self.uploaded_files: Dict[str, dict] = {}
        self.load()
    
    def load(self):
        """加载上传记录"""
        if self.tracker_file.exists():
            try:
                with open(self.tracker_file, 'r', encoding='utf-8') as f:
                    self.uploaded_files = json.load(f)
                logger.info(f"加载上传记录: {len(self.uploaded_files)} 个文件")
            except Exception as e:
                logger.error(f"加载上传记录失败: {e}")
                self.uploaded_files = {}
        else:
            self.uploaded_files = {}
    
    def save(self):
        """保存上传记录"""
        try:
            self.tracker_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.tracker_file, 'w', encoding='utf-8') as f:
                json.dump(self.uploaded_files, f, ensure_ascii=False, indent=2)
            logger.info(f"保存上传记录: {len(self.uploaded_files)} 个文件")
        except Exception as e:
            logger.error(f"保存上传记录失败: {e}")
    
    def is_uploaded(self, file_path: Path, category: str = "") -> bool:
        """检查文件是否已上传"""
        rel_path = str(Path(category) / file_path.name) if category else file_path.name
        return rel_path in self.uploaded_files
    
    def mark_uploaded(self, file_path: Path, category: str = "", remote_url: str = ""):
        """标记文件为已上传"""
        import time
        
        rel_path = str(Path(category) / file_path.name) if category else file_path.name
        
        self.uploaded_files[rel_path] = {
            "filename": file_path.name,
            "category": category,
            "remote_url": remote_url,
            "upload_time": time.time(),
            "file_size": file_path.stat().st_size if file_path.exists() else 0
        }
        self.save()
        logger.info(f"标记为已上传: {rel_path}")
    
    def get_uploaded_count(self) -> int:
        """获取已上传文件数量"""
        return len(self.uploaded_files)
    
    def clear_record(self):
        """清空上传记录"""
        self.uploaded_files = {}
        self.save()
        logger.info("上传记录已清空")

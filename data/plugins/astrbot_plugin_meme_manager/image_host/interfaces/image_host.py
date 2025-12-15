from abc import ABC, abstractmethod
from typing import List, Dict
from pathlib import Path

class ImageHostInterface(ABC):
    """图床接口抽象基类"""
    
    @abstractmethod
    def upload_image(self, file_path: Path) -> Dict[str, str]:
        """
        上传图片到图床
        
        Args:
            file_path: 图片文件路径
            
        Returns:
            Dict: {
                'url': '图片URL',
                'hash': '图片哈希值'
            }
        """
        pass
    
    @abstractmethod
    def delete_image(self, image_hash: str) -> bool:
        """
        从图床删除图片
        
        Args:
            image_hash: 图片哈希值
            
        Returns:
            bool: 删除是否成功
        """
        pass
    
    @abstractmethod
    def get_image_list(self) -> List[Dict[str, str]]:
        """
        获取图床上的所有图片信息
        
        Returns:
            List[Dict]: [{
                'url': '图片URL',
                'hash': '图片哈希值',
                'filename': '文件名'
            }]
        """
        pass
    
    @abstractmethod
    def download_image(self, image_info: Dict[str, str], save_path: Path) -> bool:
        """
        下载图片到本地
        
        Args:
            image_info: 图片信息
            save_path: 保存路径
            
        Returns:
            bool: 下载是否成功
        """
        pass 
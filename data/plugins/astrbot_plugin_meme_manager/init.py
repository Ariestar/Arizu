import os
import logging
from .utils import ensure_dir_exists, save_json, copy_memes_if_not_exists
from .config import (
    BASE_DATA_DIR,
    MEMES_DIR,
    MEMES_DATA_PATH,
    DEFAULT_CATEGORY_DESCRIPTIONS
)

logger = logging.getLogger(__name__)

def init_plugin():
    """初始化插件，创建必要的目录和配置文件"""
    try:
        # 创建基础数据目录
        ensure_dir_exists(BASE_DATA_DIR)
        
        # 创建表情包目录
        copy_memes_if_not_exists()
        
        # 初始化 memes_data.json
        if not os.path.exists(MEMES_DATA_PATH):
            save_json(DEFAULT_CATEGORY_DESCRIPTIONS, MEMES_DATA_PATH)
            logger.info(f"创建默认类别描述文件: {MEMES_DATA_PATH}")
            
        return True
    except Exception as e:
        logger.error(f"插件初始化失败: {e}")
        return False 
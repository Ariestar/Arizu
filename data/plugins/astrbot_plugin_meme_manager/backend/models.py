import os
import aiofiles  # 使用 aiofiles 进行异步文件操作
import logging
from werkzeug.utils import secure_filename
from ..config import MEMES_DIR

logger = logging.getLogger(__name__)


async def scan_emoji_folder():
    """扫描表情包文件夹，返回所有类别及其表情包"""
    emoji_data = {}
    if not os.path.exists(MEMES_DIR):
        os.makedirs(MEMES_DIR)
    for category in os.listdir(MEMES_DIR):
        category_path = os.path.join(MEMES_DIR, category)
        if os.path.isdir(category_path):
            emoji_files = [
                f
                for f in os.listdir(category_path)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            ]
            emoji_data[category] = emoji_files
        else:
            emoji_data[category] = []
    return emoji_data


def get_emoji_by_category(category):
    """获取指定类别下的所有表情包"""
    category_path = os.path.join(MEMES_DIR, category)
    if not os.path.isdir(category_path):
        return []
    emoji_files = [
        f
        for f in os.listdir(category_path)
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
    ]
    return emoji_files


def add_emoji_to_category(category, image_file):
    """
    添加表情包到指定类别
    
    Args:
        category: 类别名
        image_file: 上传的文件对象
    
    Returns:
        str: 保存后的文件路径
    """
    if not image_file:
        logger.error("没有接收到文件")
        raise ValueError("没有接收到文件")
        
    if not image_file.filename:
        logger.error("文件名为空")
        raise ValueError("文件名为空")
    
    # 使用 pathlib.Path 处理路径，避免路径问题
    from pathlib import Path
    
    # 确保类别目录存在
    category_path = Path(MEMES_DIR) / category
    category_path.mkdir(parents=True, exist_ok=True)
    
    # 保存文件
    filename = image_file.filename
    # 生成安全的文件名
    safe_filename = secure_filename(filename)
    
    # 如果文件名被修改了，记录日志
    if safe_filename != filename:
        logger.info(f"文件名已从 {filename} 修改为安全的文件名 {safe_filename}")
        filename = safe_filename
    
    # 完整的文件保存路径
    file_path = category_path / filename
    
    # 记录日志，包括绝对路径
    logger.info(f"准备保存文件到: {file_path.absolute()}")
    
    try:
        # 检查目录是否可写
        if not os.access(category_path, os.W_OK):
            logger.error(f"没有权限写入目录: {category_path}")
            raise IOError(f"没有权限写入目录: {category_path}")
            
        # 检查磁盘空间是否足够
        import shutil
        _, _, free = shutil.disk_usage(category_path)
        # 假设文件不会超过10MB，保险起见检查是否至少有10MB
        if free < 10 * 1024 * 1024:
            logger.error(f"磁盘空间不足: 只有 {free / 1024 / 1024:.2f}MB")
            raise IOError("磁盘空间不足")
            
        # 直接以二进制方式读取和写入文件，避免FileStorage.save可能存在的问题
        image_file.stream.seek(0)  # 确保从头开始读取
        content = image_file.stream.read()
        
        # 以二进制写入模式保存文件
        with open(file_path, 'wb') as f:
            f.write(content)
        
        # 验证文件是否成功保存
        if not file_path.exists():
            logger.error(f"文件保存失败，{file_path} 不存在")
            raise IOError(f"文件保存失败，{file_path} 不存在")
            
        file_size = file_path.stat().st_size
        if file_size == 0:
            logger.error(f"文件保存失败，{file_path} 大小为0")
            raise IOError(f"文件保存失败，{file_path} 大小为0")
            
        logger.info(f"文件成功保存到 {file_path}, 大小: {file_size} 字节")
        return str(file_path)
        
    except Exception as e:
        logger.error(f"保存文件时出错: {str(e)}", exc_info=True)
        # 如果文件已部分创建，尝试删除
        if file_path.exists():
            try:
                file_path.unlink()  # 删除文件
                logger.info(f"已删除部分上传的文件: {file_path}")
            except Exception as del_e:
                logger.error(f"无法删除部分上传的文件: {del_e}")
        raise IOError(f"保存文件时出错: {str(e)}")


def delete_emoji_from_category(category, image_file):
    """删除指定类别下的表情包"""
    category_path = os.path.join(MEMES_DIR, category)

    if not os.path.isdir(category_path):
        return False
    image_path = os.path.join(category_path, image_file)
    if os.path.exists(image_path):
        os.remove(image_path)
        return True
    return False


def update_emoji_in_category(category, old_image_file, new_image_file):
    """更新（替换）表情包文件"""
    category_path = os.path.join(MEMES_DIR, category)

    if not os.path.isdir(category_path):
        return False
    old_image_path = os.path.join(category_path, old_image_file)
    if os.path.exists(old_image_path):
        os.remove(old_image_path)
        filename = secure_filename(new_image_file.filename)
        target_path = os.path.join(category_path, filename)
        new_image_file.save(target_path)
        return True
    return False

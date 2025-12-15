import os
import json
import logging
import aiohttp
import random
import string
from typing import Dict, Any
import shutil
from .config import MEMES_DIR, CURRENT_DIR

logger = logging.getLogger(__name__)

def ensure_dir_exists(path: str) -> None:
    """确保目录存在，不存在则创建"""
    if not os.path.exists(path):
        os.makedirs(path)

def copy_memes_if_not_exists():
    """如果 MEMES_DIR 下没有表情包文件，则复制 CURRENT_DIR 下的 memes 文件夹内容"""
    # 确保目录存在
    ensure_dir_exists(MEMES_DIR)
    
    # 检查目录是否为空或只有非常少的文件（可能是残留或系统生成文件）
    meme_files = [f for f in os.listdir(MEMES_DIR) if os.path.isfile(os.path.join(MEMES_DIR, f))]
    
    # 如果目录为空或几乎为空，复制默认表情包
    if len(meme_files) < 3:  # 假设少于3个文件认为是空目录
        source_dir = os.path.join(CURRENT_DIR, "memes")
        if os.path.exists(source_dir):
            # 复制所有文件
            for item in os.listdir(source_dir):
                src_path = os.path.join(source_dir, item)
                dst_path = os.path.join(MEMES_DIR, item)
                if os.path.isdir(src_path):
                    if not os.path.exists(dst_path):
                        shutil.copytree(src_path, dst_path)
                else:
                    shutil.copy2(src_path, dst_path)
            logger.info(f"已将默认表情包复制到 {MEMES_DIR}")
        else:
            logger.warning(f"默认表情包目录不存在: {source_dir}")

def save_json(data: Dict[str, Any], filepath: str) -> bool:
    """保存 JSON 数据到文件"""
    try:
        ensure_dir_exists(os.path.dirname(filepath))
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存 JSON 文件失败 {filepath}: {e}")
        return False

def load_json(filepath: str, default: Dict = None) -> Dict:
    """从文件加载 JSON 数据"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"加载 JSON 文件失败 {filepath}: {e}")
        return default if default is not None else {}

def dict_to_string(dictionary):
    lines = [f"{key} - {value}\n" for key, value in dictionary.items()]
    return "\n".join(lines)

def generate_secret_key(length=8):
    """生成随机秘钥"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

async def get_public_ip():
    """异步获取公网IPv4地址"""
    ipv4_apis = [
        'http://ipv4.ifconfig.me/ip',        # IPv4专用接口
        'http://api-ipv4.ip.sb/ip',          # 樱花云IPv4接口
        'http://v4.ident.me',                # IPv4专用
        'http://ip.qaros.com',               # 备用国内服务
        'http://ipv4.icanhazip.com',         # IPv4专用
        'http://4.icanhazip.com'             # 另一个变种地址
    ]
    
    async with aiohttp.ClientSession() as session:
        for api in ipv4_apis:
            try:
                async with session.get(api, timeout=5) as response:
                    if response.status == 200:
                        ip = (await response.text()).strip()
                        # 添加二次验证确保是IPv4格式
                        if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip):
                            return ip
            except:
                continue
    
    return "[服务器公网ip]"

import hashlib
import time
import random
import string
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pathlib import Path
from typing import List, Dict, TypedDict
from ..interfaces.image_host import ImageHostInterface
import urllib3
import json
import logging
import mimetypes
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from botocore.config import Config

logger = logging.getLogger(__name__)


class CloudflareR2Error(Exception):
    """Cloudflare R2 相关错误的基类"""

    pass


class AuthenticationError(CloudflareR2Error):
    """认证错误"""

    pass


class NetworkError(CloudflareR2Error):
    """网络错误"""

    pass


class InvalidResponseError(CloudflareR2Error):
    """响应格式错误"""

    pass


class ImageInfo(TypedDict):
    url: str
    id: str
    filename: str
    category: str


class CloudflareR2Provider(ImageHostInterface):
    """Cloudflare R2图床提供者实现"""

    def __init__(self, config: Dict[str, str]):
        """
        初始化Cloudflare R2图床

        Args:
            config: {
                'account_id': 'your_account_id',
                'access_key_id': 'your_access_key_id',
                'secret_access_key': 'your_secret_access_key',
                'bucket_name': 'your_bucket_name',
                'public_url': 'https://your-domain.com'  # 可选，CDN域名
            }
        """
        required_fields = {"account_id", "access_key_id", "secret_access_key", "bucket_name"}
        missing_fields = required_fields - set(config.keys())
        if missing_fields:
            raise ValueError(f"Missing required config fields: {missing_fields}")
        
        self.config = config
        self.account_id = config["account_id"]
        self.access_key_id = config["access_key_id"]
        self.secret_access_key = config["secret_access_key"]
        self.bucket_name = config["bucket_name"]
        self.public_url = config.get("public_url")
        
        logger.info(f"初始化 Cloudflare R2 图床: account_id={self.account_id}, bucket={self.bucket_name}")
        if self.public_url:
            logger.info(f"使用自定义公共URL: {self.public_url}")
        else:
            logger.info(f"使用R2.dev默认公共URL: https://{self.bucket_name}.{self.account_id}.r2.dev")
        
        # 初始化S3客户端
        endpoint_url = f"https://{self.account_id}.r2.cloudflarestorage.com"
        logger.info(f"R2 S3端点: {endpoint_url}")
        
        self.s3_client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            config=Config(
                signature_version='s3v4',
                s3={'addressing_style': 'path'}
            )
        )
        
        # 测试连接
        try:
            self.s3_client.head_bucket(Bucket=self.bucket_name)
            logger.info(f"成功连接到R2存储桶: {self.bucket_name}")
        except ClientError as e:
            logger.error(f"无法访问R2存储桶 {self.bucket_name}: {e}")
            raise
        
        # 禁用SSL警告
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def upload_image(self, file_path: Path) -> ImageInfo:
        """上传图片到Cloudflare R2"""
        max_retries = 3
        retry_delay = 2

        for attempt in range(max_retries):
            try:
                # 获取文件信息
                if not file_path.exists():
                    raise FileNotFoundError(f"File not found: {file_path}")
                
                file_stat = file_path.stat()
                mime_type, _ = mimetypes.guess_type(str(file_path))
                if not mime_type:
                    # 默认使用jpeg类型
                    mime_type = "image/jpeg"
                
                logger.debug(f"上传文件: {file_path}")
                logger.info(f"开始上传: {file_path.name}")
                
                # 生成S3键名（保持分类结构）
                s3_key = self._generate_s3_key(file_path)
                logger.info(f"生成的S3键名: {s3_key}")
                
                # 读取文件内容
                with open(file_path, 'rb') as f:
                    file_content = f.read()
                
                logger.info(f"准备上传到存储桶 {self.bucket_name}, 文件大小: {len(file_content)} bytes")
                
                # 上传到R2（Cloudflare R2不支持ACL，默认通过R2.dev或自定义域名公开访问）
                self.s3_client.put_object(
                    Bucket=self.bucket_name,
                    Key=s3_key,
                    Body=file_content,
                    ContentType=mime_type
                )
                
                logger.info(f"上传成功: {s3_key}")
                
                # 获取公共URL
                public_url = self._get_public_url(s3_key)
                
                logger.info(f"上传成功 URL: {public_url}")
                return {
                    "url": public_url,
                    "id": s3_key,
                    "filename": file_path.name,
                    "category": self._get_category_from_path(file_path),
                }
                
            except ClientError as e:
                error_code = e.response['Error']['Code']
                error_message = e.response['Error']['Message']
                logger.error(f"AWS错误 ({error_code}): {error_message}")
                
                if attempt < max_retries - 1:
                    logger.warning(f"AWS错误，重试中: {error_message}")
                    time.sleep(retry_delay)
                    continue
                raise CloudflareR2Error(f"AWS错误: {error_message}")
                
            except Exception as e:
                logger.error(f"上传异常: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                raise CloudflareR2Error(f"上传失败: {str(e)}")

        raise Exception(f"Upload failed after {max_retries} retries")

    def delete_image(self, image_id: str) -> bool:
        """从Cloudflare R2删除图片"""
        try:
            self.s3_client.delete_object(
                Bucket=self.bucket_name,
                Key=image_id
            )
            return True
        except ClientError as e:
            logger.error(f"删除失败: {e}")
            return False
        except Exception as e:
            logger.error(f"删除异常: {str(e)}")
            return False

    def get_image_list(self) -> List[ImageInfo]:
        """获取Cloudflare R2中 memes/ 文件夹内的所有图片"""
        all_images = []
        
        try:
            paginator = self.s3_client.get_paginator('list_objects_v2')
            
            # 只列出 memes/ 前缀的文件
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix="memes/"):
                if 'Contents' in page:
                    for obj in page['Contents']:
                        s3_key = obj['Key']
                        
                        # 跳过目录
                        if s3_key.endswith('/'):
                            continue
                        
                        # 只处理 memes/ 下的文件
                        if not s3_key.startswith('memes/'):
                            continue
                        
                        # 解析分类和文件名
                        category, filename = self._parse_s3_key(s3_key)
                        
                        # 获取公共URL
                        public_url = self._get_public_url(s3_key)
                        
                        all_images.append({
                            "url": public_url,
                            "id": s3_key,
                            "filename": filename,
                            "category": category,
                        })
                        
        except ClientError as e:
            logger.error(f"获取文件列表失败: {e}")
            raise CloudflareR2Error(f"获取文件列表失败: {e}")
            
        return all_images

    def download_image(self, image_info: Dict[str, str], save_path: Path) -> bool:
        """从Cloudflare R2下载图片"""
        max_retries = 3
        retry_delay = 1
        
        s3_key = image_info["id"]
        
        for attempt in range(max_retries):
            try:
                # 确保目标目录存在
                save_path.parent.mkdir(parents=True, exist_ok=True)
                
                # 从R2下载文件
                self.s3_client.download_file(
                    self.bucket_name,
                    s3_key,
                    str(save_path)
                )
                
                # 验证文件是否成功下载
                if save_path.exists() and save_path.stat().st_size > 0:
                    logger.info(f"下载成功: {s3_key}")
                    return True
                else:
                    logger.error(f"下载的文件无效: {s3_key}")
                    
            except ClientError as e:
                logger.error(f"下载失败: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return False
                
            except Exception as e:
                logger.error(f"下载异常: {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                return False
                
        return False

    def _generate_s3_key(self, file_path: Path) -> str:
        """生成S3键名，保持分类结构，所有文件放在memes文件夹中"""
        # 从文件路径中提取分类信息
        category = self._get_category_from_path(file_path)
        filename = file_path.name
        
        if category:
            return f"memes/{category}/{filename}"
        else:
            return f"memes/{filename}"

    def _get_category_from_path(self, file_path: Path) -> str:
        """从文件路径获取分类"""
        # 从文件路径中提取相对于表情包目录的分类
        # 假设 file_path 是完整的本地路径，如 /path/to/memes/category/filename.jpg
        
        # 获取父目录
        parent = file_path.parent
        
        # 如果文件在子目录中，返回子目录名作为分类
        if parent.name and parent.name != "." and parent.name != str(file_path.parents[-2]):
            # 尝试获取相对于memes目录的路径
            # 这里简化处理，返回直接父目录名
            return parent.name
        
        return ""

    def _parse_s3_key(self, s3_key: str) -> tuple:
        """解析S3键名获取分类和文件名"""
        # 移除 memes/ 前缀
        if s3_key.startswith("memes/"):
            s3_key = s3_key[6:]
        
        filename = s3_key.split('/')[-1]
        category = ""
        
        # 如果S3键包含路径，提取分类
        if '/' in s3_key:
            path_parts = s3_key.split('/')
            if len(path_parts) > 1:
                category = '/'.join(path_parts[:-1])
        
        return category, filename

    def _get_public_url(self, s3_key: str) -> str:
        """获取文件的公共URL"""
        if self.public_url:
            # 如果配置了自定义CDN域名（确保不以斜杠结尾）
            base_url = self.public_url.rstrip("/")
            return f"{base_url}/{s3_key}"
        else:
            # 使用R2默认的公共访问URL（R2.dev子域名）
            return f"https://{self.bucket_name}.{self.account_id}.r2.dev/{s3_key}"
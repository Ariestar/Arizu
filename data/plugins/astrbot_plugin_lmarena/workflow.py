import asyncio
import mimetypes
from random import random
import re
import base64
from pathlib import Path
from typing import Optional
import uuid
import aiohttp
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
import astrbot.core.message.components as Comp
import io
from PIL import Image


def extract_first_frame(raw: bytes) -> bytes:
    """把 GIF 的第一帧抽出来，返回 PNG/JPEG 字节流"""
    img_io = io.BytesIO(raw)
    img = Image.open(img_io)
    if img.format != "GIF":
        return raw  # 不是 GIF，原样返回
    first_frame = img.convert("RGBA")
    out_io = io.BytesIO()
    first_frame.save(out_io, format="PNG")
    return out_io.getvalue()


async def compress_image(image_bytes: bytes, max_bytes: int) -> bytes:
    """
    线程池里压缩静态图片到指定大小以内，GIF 不处理
    """
    loop = asyncio.get_running_loop()

    def _inner(image_bytes: bytes, max_bytes: int) -> bytes:
        try:
            img = Image.open(io.BytesIO(image_bytes))

            # GIF 不处理
            if img.format == "GIF":
                return image_bytes

            if len(image_bytes) <= max_bytes:
                return image_bytes

            # 2) 先把长边一次性缩到 1024 以下，质量先压到 70
            img.thumbnail((1024, 1024), Image.LANCZOS)  # type: ignore
            resampled = io.BytesIO()
            img.save(resampled, format=img.format, quality=70, optimize=True)
            resampled.seek(0)
            if resampled.tell() <= max_bytes:
                return resampled.getvalue()

            # 3) 还不够小，再进入原有循环微调
            quality, scale = 50, 0.6
            resample = Image.LANCZOS  # type: ignore

            while True:
                resampled.seek(0)
                resampled.truncate(0)

                if scale < 1:
                    w, h = img.size
                    tmp = img.resize((int(w * scale), int(h * scale)), resample)
                else:
                    tmp = img

                tmp.save(resampled, format=img.format, quality=quality, optimize=True)

                if resampled.tell() <= max_bytes or (quality <= 5 and scale <= 0.2):
                    break

                if quality > 5:
                    quality -= 5
                else:
                    scale *= 0.9

            return resampled.getvalue()

        except Exception as e:
            raise ValueError(f"图片压缩失败: {e}")

    return await loop.run_in_executor(None, _inner, image_bytes, max_bytes)


class Workflow:
    """
    工具类
    """

    headers = {"Content-Type": "application/json"}

    def __init__(
        self, config: AstrBotConfig, bridge_server_url: str, image_server_url: str | None
    ):
        self.conf = config
        self.bridge_server_url = bridge_server_url
        self.image_server_url = image_server_url
        self.session = aiohttp.ClientSession()

    async def upload_to_bed(self, img_bytes: bytes, image_server_url: str) -> str | None:
        """
        转 base64 并上传到图床，返回可访问的 URL。
        """
        try:
            # 自动生成文件名（确保唯一）
            file_name = f"{uuid.uuid4().hex}.jpg"
            mime_type = mimetypes.guess_type(file_name)[0] or "image/jpeg"

            # 转 Base64
            base64_str = base64.b64encode(img_bytes).decode()
            data_uri = f"data:{mime_type};base64,{base64_str}"

            api_key = self.conf["image_server"]["api_key"]
            payload = {
                "file_name": file_name,
                "file_data": data_uri,
                "api_key": api_key,
            }

            async with self.session.post(
                url=image_server_url, json=payload
            ) as response:
                if response.status >= 400:
                    error_text = await response.text()
                    logger.error(
                        f"上传到文件床时发生 HTTP 错误: {response.status} - {error_text}"
                    )
                    return None

                result = await response.json()
                if not (result.get("success") and result.get("filename")):
                    logger.error(f"图床上传失败: {result.get('error', '未知错误')}")
                    return None
                # 拼接 URL
                url_prefix = image_server_url.rsplit("/", 1)[0]
                uploaded_url = f"{url_prefix}/uploads/{result['filename']}"
                logger.info(f"图片成功上传到图床: {uploaded_url}")
                return uploaded_url

        except aiohttp.ClientResponseError as e:
            logger.error(f"上传到文件床时发生 HTTP 响应错误: {e.status} - {e.message}")
        except aiohttp.ClientError as e:
            logger.error(f"连接到文件床服务器时出错: {e}")
        except Exception as e:
            logger.error(f"上传文件时发生未知错误: {e}", exc_info=True)

        return None

    async def _download_image(self, url: str, http: bool = True) -> bytes | None:
        """下载图片"""
        if http:
            url = url.replace("https://", "http://")
        try:
            async with self.session.get(url) as resp:
                return await resp.read()
        except Exception as e:
            logger.error(f"图片下载失败: {e}")
            return None

    async def _get_avatar(self, user_id: str) -> bytes | None:
        """根据 QQ 号下载头像"""
        if not user_id.isdigit():
            user_id = "".join(random.choices("0123456789", k=9))
        avatar_url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
        return await self._download_image(avatar_url)

    async def _load_bytes(self, src: str) -> bytes | None:
        """统一把 src 转成 bytes"""
        raw: Optional[bytes] = None
        # 1. 本地文件
        if Path(src).is_file():
            raw = Path(src).read_bytes()
        # 2. URL
        elif src.startswith("http"):
            raw = await self._download_image(src)
        # 3. Base64（直接返回）
        elif src.startswith("base64://"):
            return base64.b64decode(src[9:])
        if not raw:
            return None
        # 抽 GIF 第一帧
        return extract_first_frame(raw)

    async def _extract_from_segments(
        self, segments: list, event: AstrMessageEvent
    ) -> list[bytes | str]:
        """从消息片段中提取图片或头像，支持图床失败回退到 base64"""
        results: list[bytes | str] = []
        for seg in segments:
            # 处理图片
            if isinstance(seg, Comp.Image):
                if src := seg.url or seg.file:
                    if img_bytes := await self._load_bytes(src):
                        if self.image_server_url:
                            if url := await self.upload_to_bed(
                                img_bytes, self.image_server_url
                            ):
                                results.append(url)
                            else:
                                logger.warning("图床上传失败，回退到 base64")
                                results.append(img_bytes)
                        else:
                            results.append(img_bytes)

            # 处理@用户头像
            elif isinstance(seg, Comp.At) and str(seg.qq) != event.get_self_id():
                avatar = await self._get_avatar(str(seg.qq))
                if isinstance(avatar, bytes):
                    if self.image_server_url:
                        if url := await self.upload_to_bed(
                            avatar, self.image_server_url
                        ):
                            results.append(url)
                        else:
                            logger.warning("头像上传图床失败，回退到 base64")
                            results.append(avatar)
                    else:
                        results.append(avatar)

        return results

    async def get_images(self, event: AstrMessageEvent) -> list[bytes | str]:
        """收集消息和引用里的所有图片/头像"""
        images: list[bytes | str] = []

        # 1. 引用消息
        reply_seg = next(
            (s for s in event.get_messages() if isinstance(s, Comp.Reply)), None
        )
        if reply_seg and reply_seg.chain:
            images.extend(await self._extract_from_segments(reply_seg.chain, event))

        # 2. 当前消息
        images.extend(await self._extract_from_segments(event.get_messages(), event))
        return images

    @staticmethod
    async def make_openai_req(
        text: str, images: list[bytes | str] | None, model: str
    ) -> dict:
        """
        制作 OpenAI 格式数据块，支持多张图片
        - images 可为单个 bytes/str，也可为 list
        """
        content: list[dict] = [{"type": "text", "text": text}]

        if images:
            for img in images:
                if isinstance(img, bytes):
                    compressed = await compress_image(img, 3_500_000)
                    img_url = f"data:image/jpeg;base64,{base64.b64encode(compressed).decode()}"
                elif isinstance(img, str):
                    img_url = img
                else:
                    continue

                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": img_url},
                    }
                )

        return {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "n": 1,
        }

    async def fetch_content(
        self,
        text: str,
        images: list[bytes | str] | None,
        model: str,
        retries: int = 3,
    ) -> bytes | str | None:
        """
        发送请求并返回图片 bytes；
        失败时重试 retries 次，最后一次仍失败则返回错误字符串。
        """
        openai_req = await self.make_openai_req(text, images, model)
        logger.debug(openai_req)
        url = f"{self.bridge_server_url}/v1/chat/completions"
        error_msg = None  # 记录最后一次的错误信息
        for attempt in range(retries + 1):
            logger.info(f"请求{model}(第 {attempt + 1} 次): {text[:50]}...")
            try:
                async with self.session.post(
                    url, headers=self.headers, json=openai_req
                ) as resp:
                    result = await resp.json()
                    logger.debug(result)
                    if resp.status != 200:
                        error_msg = result.get("error", {}).get("message") or str(
                            result
                        )
                        if "422" in error_msg:
                            error_msg = "内容不合规"
                        raise ValueError(error_msg)  # 触发重试

                    # HTTP 200，尝试解析图片 URL
                    content_msg = result["choices"][0]["message"]["content"]
                    if match := re.search(r"!\[.*?\]\((.*?)\)", content_msg):
                        img_url = match.group(1)
                        logger.info(f"返回图片 URL: {img_url}")
                        img = await self._download_image(img_url, http=False)
                        if not img:
                            error_msg = "图片下载失败"
                            raise ValueError("图片下载失败")  # 触发重试
                        return img
                    elif content_msg:
                        return content_msg
                    else:
                        error_msg = "响应为空"
                        raise ValueError(error_msg)

            except Exception as e:
                logger.error(f"第 {attempt + 1} 次失败: {e}")
                if attempt < retries:
                    await asyncio.sleep(2**attempt)
                # 最后一次循环继续，不会提前 return

        # 走到这里说明所有重试机会已用完
        return error_msg or "unknown error"

    async def terminate(self):
        if self.session:
            await self.session.close()

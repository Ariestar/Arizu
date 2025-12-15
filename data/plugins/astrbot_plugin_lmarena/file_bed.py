import asyncio
import base64
import shutil
import threading
from pathlib import Path
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig



class UploadPayload(BaseModel):
    file_name: str
    file_data: str  # data URI
    api_key: str


class ImageServer:
    def __init__(self, config: AstrBotConfig, upload_dir: Path):
        self.clear_cache_interval = config["image_server"]["clear_cache_interval"]
        self.host = config["image_server"]["host"]
        self.port = config["image_server"]["port"]
        self.api_key = config["image_server"]["api_key"]
        self.upload_dir = upload_dir
        self.app = FastAPI()
        self._server = None
        self._thread = None
        self._cleaner_thread = None
        self._stop_cleaner = threading.Event()
        self.app.mount(
            "/uploads", StaticFiles(directory=self.upload_dir), name="uploads"
        )
        self._setup_routes()

    def _setup_routes(self):
        @self.app.post("/upload")
        async def upload(request: Request, payload: UploadPayload = Body(...)):
            # 校验 API key
            if payload.api_key != self.api_key:
                raise HTTPException(403)

            # 解析 base64
            if payload.file_data.startswith("data:"):
                base64_str = payload.file_data.split(",", 1)[1]
            else:
                base64_str = payload.file_data
            file_bytes = base64.b64decode(base64_str)

            # 保存文件
            save_path = self.upload_dir / payload.file_name
            with open(save_path, "wb") as f:
                f.write(file_bytes)

            client_host = request.client.host if request.client else "unknown"
            logger.info(f"[图床] (来自 {client_host})上传完成，已保存到: {save_path}")

            return {"success": True, "filename": payload.file_name}

    def _clear_cache(self):
        try:
            if self.upload_dir.exists():
                shutil.rmtree(self.upload_dir)
            self.upload_dir.mkdir(parents=True, exist_ok=True)
            logger.info("[图床] 定时清理完成，缓存已清空")
        except Exception as e:
            logger.error(f"[图床] 清理缓存失败: {e}")

    def _start_cleaner(self, interval_hours: int = 6):
        async def _loop():
            try:
                while not self._stop_cleaner.is_set():
                    await asyncio.sleep(interval_hours * 3600)
                    if not self._stop_cleaner.is_set():
                        self._clear_cache()
            except asyncio.CancelledError:
                logger.info("[图床] 缓存清理任务已取消")

        self._cleaner_task = asyncio.create_task(_loop())

    def start(self):
        if self._server:
            return
        config = uvicorn.Config(
            app=self.app, host=self.host, port=self.port, loop="asyncio"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        if self.clear_cache_interval:
            self._start_cleaner(interval_hours=self.clear_cache_interval)
        logger.info(f"内置图床已启动: http://{self.host}:{self.port}")

    def stop(self):
        if self._server:
            self._server.should_exit = True
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=5)

        if hasattr(self, "_cleaner_task"):
            self._stop_cleaner.set()
            self._cleaner_task.cancel()
        logger.info("内置图床已优雅关闭")

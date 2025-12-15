import asyncio
import json
from aiohttp import web
import threading
from astrbot.api import logger
import uuid
from fastapi import WebSocket, WebSocketDisconnect, Request, HTTPException, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from astrbot.core.config.astrbot_config import AstrBotConfig
from typing import Optional

from .models import ModelsManager
from .response import ResponseManager
from .process import Process


class FastAPIWrapper:
    def __init__(self, server, config: AstrBotConfig):
        """
        server: LMArenaBridgeServer 实例
        """
        self.server = server
        self.host = config["bridge_server"]["host"]
        self.port = config["bridge_server"]["port"]
        self.app = FastAPI(lifespan=self.lifespan)  # type: ignore
        self._uvicorn_server = None
        self._server_thread: Optional[threading.Thread] = None

        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        self._setup_routes()

    @staticmethod
    async def lifespan(app: FastAPI):
        yield

    def _setup_routes(self):
        app = self.app
        s = self.server

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket):
            await s.websocket_endpoint(ws)

        @app.get("/v1/models")
        async def get_models():
            return await s.get_models()

        @app.post("/v1/chat/completions")
        async def chat_completions(request: Request):
            return await s.chat_completions(request)

        @app.post("/internal/update_available_models")
        async def update_available_models(request: Request):
            return await s.update_available_models_endpoint(request)

    def start(self):
        if self._uvicorn_server and self._uvicorn_server.started:
            return
        import uvicorn

        config = uvicorn.Config(self.app, host=self.host, port=self.port)
        self._uvicorn_server = uvicorn.Server(config)
        self._server_thread = threading.Thread(
            target=self._uvicorn_server.run, daemon=True
        )
        self._server_thread.start()
        logger.info("内置LMArena桥梁已启动!")
        logger.info(f"监听地址: {self.host}:{self.port}")
        logger.info(f"WebSocket 端点: ws://{self.host}:{self.port}/ws")

    def stop(self):
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True

            if self._server_thread and self._server_thread.is_alive():
                self._server_thread.join(timeout=5)

            self._uvicorn_server = None
            self._server_thread = None
            logger.info("LMArena 桥梁服务器已优雅关闭")


class LMArenaBridgeServer:
    """
    LMArena Bridge 后端服务
    """

    browser_ws: WebSocket | None = None  # 与单个油猴脚本的 WebSocket 连接

    def __init__(self, config: AstrBotConfig):
        self.conf = config
        # 消息模版处理器
        self.processor = Process(config)
        # 响应管理器
        self.responser = ResponseManager(config)
        self.responser.callback = self.refresh

        # 模型管理器
        self.model_mgr = ModelsManager(config)

    # ---------------- WS处理 ----------------
    async def websocket_endpoint(self, websocket: WebSocket):
        """处理来自油猴脚本的 WebSocket 连接。"""
        await websocket.accept()
        self.browser_ws = websocket
        logger.info("✅ 油猴脚本已成功连接 WebSocket。")
        # 刷新模型列表
        await self.trigger_model_update()
        try:
            while True:
                # 等待并接收来自油猴脚本的消息
                message_str = await websocket.receive_text()
                logger.debug(f"[油猴->本地]: {message_str[:100]}")
                message = json.loads(message_str)

                request_id = message.get("request_id")
                data = message.get("data")

                if not request_id or data is None:
                    logger.warning(f"[油猴脚本]无效消息: {message}")
                    continue

                # 将收到的数据放入对应的响应通道
                if request_id in self.responser.channels:
                    await self.responser.channels[request_id].put(data)
                else:
                    logger.warning(f"[油猴脚本]未知响应: {request_id}")

        except WebSocketDisconnect:
            logger.warning("❌ 油猴脚本客户端已断开连接。")
        except Exception as e:
            logger.error(f"WebSocket 处理时发生未知错误: {e}", exc_info=True)
        finally:
            self.browser_ws = None
            for queue in self.responser.channels.values():
                await queue.put({"error": "Browser disconnected during operation"})
            self.responser.channels.clear()

    async def ws_send(self, payload: dict):
        if not self.browser_ws:
            raise HTTPException(
                status_code=503,
                detail="油猴脚本客户端未连接。请确保 LMArena 页面已打开并激活脚本。",
            )

        await self.browser_ws.send_text(json.dumps(payload, ensure_ascii=False))
        truncated_payload = json.dumps(payload, ensure_ascii=False)[:200]
        logger.debug(f"[本地->油猴]: {truncated_payload}...")

    # ---------------- main.py调用的接口 ----------------
    async def refresh(self):
        """刷新油猴脚本页面"""
        await self.ws_send({"command": "refresh"})

    async def trigger_model_update(self):
        """让油猴发送页面源代码"""
        await self.ws_send({"command": "send_page_source"})

    def get_model_dict(self) -> dict:
        """获取所有模型列表"""
        return dict(self.model_mgr.model_map)

    async def update_id(
        self,
        host: str = "127.0.0.1",
        port: int = 5103,
        timeout: int = 20,
    ) -> str:
        """
        一次性 aiohttp 监听器，等待 Tampermonkey 推送 {sessionId, messageId}
        """
        await self.ws_send({"command": "activate_id_capture"})
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        async def handler(request: web.Request):
            if request.method == "OPTIONS":
                return web.Response(
                    status=204,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type",
                    },
                )

            data = await request.json()
            sid, mid = data.get("sessionId"), data.get("messageId")
            if sid and mid:
                if not future.done():
                    future.set_result((sid, mid))
                return web.json_response(
                    {"status": "success"}, headers={"Access-Control-Allow-Origin": "*"}
                )
            return web.json_response(
                {"error": "Missing sessionId or messageId"},
                status=400,
                headers={"Access-Control-Allow-Origin": "*"},
            )

        app = web.Application()
        app.router.add_route("*", "/update", handler)
        runner = web.AppRunner(app)

        try:
            await runner.setup()
            await web.TCPSite(runner, host, port).start()
            logger.info(
                f"🚀 捕获监听中: http://{host}:{port}/update (timeout={timeout})"
            )
            try:
                sid, mid = await asyncio.wait_for(future, timeout)
                self.conf.update({"session_id": sid, "message_id": mid})
                self.conf.save_config()
                logger.info(f"✅ 成功捕获并保存: {sid}, {mid}")
                return f"已捕获会话ID: {sid[:8]}..."
            except asyncio.TimeoutError:
                logger.warning("⏳ 捕获超时")
                return "捕获超时"
        except OSError as e:
            logger.error(f"❌ 监听器启动失败 {host}:{port}: {e}")
            return "监听器启动失败"
        finally:
            await runner.cleanup()

    # ---------------- FastAPI调用 ----------------
    async def chat_completions(self, request: Request):
        """
        FastAPI 路由函数
        负责解析请求体、API Key 验证，然后转发给逻辑函数。
        """
        # Json检查
        try:
            openai_req = await request.json()
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

        # API Key 验证
        if self.conf["bridge_server"]["api_key"]:
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                raise HTTPException(
                    status_code=401,
                    detail="未提供 API Key。请在 Authorization 头部中以 'Bearer YOUR_KEY' 格式提供。",
                )
            provided_key = auth_header.split(" ")[1]
            if provided_key != self.conf["bridge_server"]["api_key"]:
                raise HTTPException(status_code=401, detail="提供的 API Key 不正确。")

        # 生成请求ID
        request_id = str(uuid.uuid4())

        # 创建响应通道
        self.responser.channels[request_id] = asyncio.Queue()

        # 发送载荷到油猴脚本
        payload = {
            "request_id": request_id,
            "payload": {
                "message_templates": self.processor.openai_to_lmarena(openai_req),
                "target_model_id": None, # fuck! 原来是个没作用的参数
                "session_id": self.conf["session_id"],
                "message_id": self.conf["message_id"],
            },
        }
        logger.debug(payload)
        await self.ws_send(payload)

        # 返回响应（stream 参数开启流式响应）
        try:
            return await self.responser.non_stream_response(
                request_id, "default_model"
            )
        except Exception as e:
            logger.error(
                f"API CALL [ID: {request_id[:8]}]: 处理请求时发生致命错误: {e}",
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail=str(e))

    async def update_available_models_endpoint(self, request: Request):
        """
        接收来自油猴脚本的页面 HTML，提取并更新 available_models.json。
        """
        html_content = await request.body()
        if not html_content:
            logger.warning("模型更新请求未收到任何 HTML 内容。")
            return
        logger.info("收到来自油猴脚本的页面内容，开始提取可用模型...")
        self.model_mgr.update_from_html(html_content.decode("utf-8"))

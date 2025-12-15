import os
import re
import json
import uuid
import asyncio
import platform
import subprocess
import html
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageColor
from pygments import highlight
from pygments.lexers import get_lexer_by_name, guess_lexer, get_lexer_for_filename
from pygments.formatters import ImageFormatter, HtmlFormatter
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound
from pygments.lexer import RegexLexer, bygroups, include, words
from pygments.token import Text, Comment, Operator, Keyword, Name, String, Number, Punctuation, Token

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.message_components import Image as ImageComponent, Plain, Reply, File
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from playwright.async_api import async_playwright


@register("astrbot_plugin_code_renderer", "Xbodw", "将代码信息或者代码文件渲染为图片", "1.4.7")
class CodeRenderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self.custom_languages = {}  # Store custom language definitions for highlight.js registration
        self.temp_dir = os.path.join(get_astrbot_data_path(), "temp", "code_render")
        self._cached_font = None  # Cached available font
        self._playwright = None   # Global Playwright instance
        self._browser = None      # Shared browser instance

        self.standard_language_map = {
            # Common programming languages
            '.py': 'python',
            '.js': 'javascript',
            '.ts': 'typescript',
            '.jsx': 'javascript',
            '.tsx': 'typescript',
            '.java': 'java',
            '.c': 'c',
            '.cpp': 'cpp',
            '.cc': 'cpp',
            '.cxx': 'cpp',
            '.h': 'c',
            '.hpp': 'cpp',
            '.cs': 'csharp',
            '.php': 'php',
            '.rb': 'ruby',
            '.go': 'go',
            '.rs': 'rust',
            '.swift': 'swift',
            '.kt': 'kotlin',
            '.scala': 'scala',
            '.r': 'r',
            '.m': 'objectivec',
            '.mm': 'objectivec',
            
            # Web technologies
            '.html': 'html',
            '.htm': 'html',
            '.xml': 'xml',
            '.css': 'css',
            '.scss': 'scss',
            '.sass': 'sass',
            '.less': 'less',
            '.json': 'json',
            '.yaml': 'yaml',
            '.yml': 'yaml',
            '.toml': 'toml',
            '.md': 'markdown',
            '.markdown': 'markdown',
            
            # Shell and scripts
            '.sh': 'bash',
            '.bash': 'bash',
            '.zsh': 'bash',
            '.ps1': 'powershell',
            '.bat': 'batch',
            '.cmd': 'batch',
            
            # Database
            '.sql': 'sql',
            
            # Others
            '.lua': 'lua',
            '.vim': 'vim',
            '.diff': 'diff',
            '.patch': 'diff',
            '.ini': 'ini',
            '.cfg': 'ini',
            '.conf': 'nginx',
            '.dockerfile': 'dockerfile',
        }
        
        self._load_custom_languages()

    async def initialize(self):
        """Initialize the plugin"""
        self._load_custom_languages()
        
        # Create temp directory
        os.makedirs(self.temp_dir, exist_ok=True)

        # Ensure Playwright browser is available
        await self._ensure_playwright_browser()

        # Start shared Playwright browser instance
        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            if self._browser is None:
                self._browser = await self._playwright.chromium.launch(headless=True)
                logger.info("CodeRender Playwright 浏览器已启动")
        except Exception as e:
            logger.error(f"启动 Playwright 浏览器失败: {e}")

        # Clean up temp files on startup
        await self._cleanup_temp_files()

        # Start periodic cleanup task
        asyncio.create_task(self._periodic_cleanup())
        
        logger.info(f"{len(self.custom_languages)} 个自定义语言已加载.")

    def _find_cjk_font(self, font_size: int):
        """Find available CJK font across platforms"""
        system = platform.system()
        font_names = []
        font_dirs = []
        
        if system == "Windows":
            font_names = ["msyh.ttc", "simhei.ttf", "simsun.ttc", "msjh.ttc"]
            font_dirs = [os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")]
        elif system == "Darwin": # macOS
            font_names = ["PingFang.ttc", "STHeiti Light.ttc", "STHeiti Medium.ttc"]
            font_dirs = ["/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
        else: # Linux / Other
            # 优先尝试使用 fc-match (fontconfig)
            try:
                # 查找支持中文的字体文件路径
                output = subprocess.check_output(['fc-match', '-f', '%{file}', ':lang=zh'], stderr=subprocess.DEVNULL).decode().strip()
                if output and os.path.exists(output):
                    try:
                        return ImageFont.truetype(output, font_size)
                    except Exception:
                        pass
            except Exception:
                pass

            # 常见 Linux CJK 字体
            font_names = [
                "NotoSansSC-Regular.otf", "NotoSansCJK-Regular.ttc", 
                "wqy-microhei.ttc", "wqy-zenhei.ttc",
                "DroidSansFallback.ttf", "uming.ttc", "ukai.ttc"
            ]
            font_dirs = [
                "/usr/share/fonts", 
                "/usr/local/share/fonts", 
                os.path.expanduser("~/.fonts"),
                os.path.expanduser("~/.local/share/fonts")
            ]

        # Search through directories
        for directory in font_dirs:
            if not os.path.exists(directory):
                continue
            for root, dirs, files in os.walk(directory):
                for filename in files:
                    # Exact match
                    if filename in font_names:
                        try:
                            return ImageFont.truetype(os.path.join(root, filename), font_size)
                        except Exception:
                            continue
                    # Fuzzy match for Noto Sans CJK
                    if "NotoSansSC" in filename and filename.endswith((".otf", ".ttf", ".ttc")):
                        try:
                            return ImageFont.truetype(os.path.join(root, filename), font_size)
                        except Exception:
                            continue

        # Fallback: Try loading by name directly (relies on system path configuration)
        for name in font_names:
            try:
                return ImageFont.truetype(name, font_size)
            except Exception:
                continue
                
        return None

    def _load_custom_languages(self):
        """Load custom language definitions from languages folder for highlight.js registration"""
        plugin_dir = Path(__file__).parent
        languages_dir = plugin_dir / "languages"
        
        if not languages_dir.exists():
            logger.info("languages directory does not exist, skipping custom language loading")
            return
        
        json_files = list(languages_dir.glob("*.json"))
        if not json_files:
            logger.info("No JSON files found in languages directory")
            return
        
        for json_file in json_files:
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    lang_def = json.load(f)
                    
                # Validate required fields
                if "name" not in lang_def:
                    logger.warning(f"Skipping {json_file.name}: missing 'name' field")
                    continue
                
                lang_id = json_file.stem  # Use filename as language identifier
                self.custom_languages[lang_id] = lang_def
                logger.info(f"Loaded custom language: {lang_id} ({lang_def['name']})")
                
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse {json_file.name}: {e}")
            except Exception as e:
                logger.error(f"Error loading {json_file.name}: {e}")

    async def _cleanup_temp_files(self):
        """Clean up temporary files"""
        if not os.path.exists(self.temp_dir):
            return
        
        count = 0
        for filename in os.listdir(self.temp_dir):
            file_path = os.path.join(self.temp_dir, filename)
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                    count += 1
            except Exception as e:
                logger.warning(f"Failed to delete temporary file {filename}: {e}")
        
        if count > 0:
            logger.info(f"Cleaned up {count} temporary files")

    async def _periodic_cleanup(self):
        """Periodically clean up temporary files older than 1 hour"""
        while True:
            try:
                await asyncio.sleep(900)  # Check every 15 minutes
                await self._cleanup_temp_files()
            except Exception as e:
                logger.error(f"Error during periodic temp file cleanup: {e}")

    async def _ensure_playwright_browser(self):
        """Ensure Playwright browser is installed and available"""
        try:
            from playwright.async_api import async_playwright as _ap

            async with _ap() as p:
                try:
                    browser = await p.chromium.launch(headless=True)
                    await browser.close()
                    #logger.info("Playwright Chromium 浏览器已就绪")
                except Exception as e:
                    logger.warning("Playwright browser not installed or unavailable, attempting to install Chromium...")
                    import subprocess as _sub
                    result = _sub.run([
                        "playwright",
                        "install",
                        "chromium",
                    ], capture_output=True, text=True)
                    if result.returncode == 0:
                        logger.info("Successfully installed Playwright Chromium")
                    else:
                        logger.error(f"Failed to automatically install Playwright browser: {result.stderr}")
                        logger.error("Please run manually: playwright install chromium")
        except Exception as e:
            logger.error(f"Error checking Playwright browser: {e}")
            logger.error("If this is the first time using, please run manually: playwright install chromium")

    def _is_group_blocked(self, event: AstrMessageEvent) -> bool:
        """Check if current group is in blacklist"""
        session_id = event.session_id
        if not session_id:
            return False
        
        return session_id in self.config.blacklist

    def _detect_language(self, code: str, hint: str = None, filename: str = None) -> str:
        """Detect code language - now fully relies on highlight.js auto-detection, only handles hints and file extensions"""
        # If language hint is provided, return it directly (let highlight.js handle it)
        if hint:
            return hint.lower().strip()
        
        # 如果提供了文件名，尝试匹配扩展名
        if filename:
            ext = os.path.splitext(filename)[1].lower()
            
            # 优先检查自定义语言的扩展名
            for lang_id, lang_def in self.custom_languages.items():
                if ext in lang_def.get("extensions", []):
                    return lang_id
            
            # 检查标准语言映射
            if ext in self.standard_language_map:
                return self.standard_language_map[ext]
        
        return None

    def _get_lexer(self, language: str, code: str):
        """获取 Pygments 语法高亮器（仅用于旧的图片渲染方式）"""
        # Pygments lexer 仅在不使用 Playwright 时需要
        try:
            return get_lexer_by_name(language, stripall=True)
        except ClassNotFound:
            # 尝试猜测
            try:
                return guess_lexer(code)
            except ClassNotFound:
                return get_lexer_by_name("text", stripall=True)

    async def _render_code_to_image(
        self,
        code: str,
        language: str,
        theme_override: str = None,
        font_size_override: int = None,
        line_numbers_override: bool = None,
    ) -> str:
        """使用 Playwright + 本地 highlight.js 模板渲染代码为图片"""
        theme_name = theme_override or (self.config.get("default_theme", "github-dark") if self.config else "github-dark")
        font_size = font_size_override or (self.config.get("font_size", 14) if self.config else 14)
        plugin_dir = os.path.dirname(__file__)

        # 字体配置：config.font_path > 插件内 JetBrainsMono-Regular.ttf > 浏览器系统字体
        font_path = None
        if self.config and self.config.get("font_path"):
            font_path = self.config.get("font_path")
        else:
            default_font_path = os.path.join(plugin_dir, "fonts", "ttf", "JetBrainsMono-Regular.ttf")
            if os.path.exists(default_font_path):
                font_path = default_font_path

        font_family = (
            self.config.get("font_family") if self.config and self.config.get("font_family")
            else "JetBrains Mono, Consolas, Fira Code, Source Code Pro, monospace"
        )

        font_face_css = ""
        if font_path and os.path.exists(font_path):
            font_url = "file://" + font_path.replace("\\", "/")
            font_face_css = f"""
        @font-face {{
            font-family: 'CodeRenderFont';
            src: url('{font_url}');
            font-weight: normal;
            font-style: normal;
        }}
        """
            font_family = "CodeRenderFont"

        # highlight.js 与主题 CSS 路径（可通过配置覆盖）
        if self.config and self.config.get("highlight_js_path"):
            hljs_path = self.config.get("highlight_js_path")
        else:
            # 默认使用插件目录下解压的 highlight.min.js
            hljs_path = os.path.join(plugin_dir, "assets", "highlight", "highlight.min.js")

        if self.config and self.config.get("highlight_css_path"):
            hljs_css_path = self.config.get("highlight_css_path")
        else:
            # 根据主题名自动匹配 styles 目录下的 CSS 文件，例如 monokai -> monokai.min.css
            css_filename = f"{theme_name}.min.css"
            hljs_css_path = os.path.join(plugin_dir, "assets", "highlight", "styles", css_filename)

        # 读取主题 CSS，如果存在则使用；否则使用内置深色主题作为回退
        hljs_theme_css = ""
        try:
            if os.path.exists(hljs_css_path):
                with open(hljs_css_path, "r", encoding="utf-8") as f:
                    hljs_theme_css = f.read()
        except Exception as e:
            logger.error(f"读取 highlight.js 主题 CSS 失败: {e}")
            hljs_theme_css = ""

        # 读取 highlight.js 源码内联到页面中，避免 file:// 外链脚本不执行
        hljs_source = ""
        try:
            with open(hljs_path, "r", encoding="utf-8") as f:
                hljs_source = f.read()
        except Exception as e:
            logger.error(f"读取 highlight.js 失败: {e}")
            hljs_source = ""

        # 读取行号插件源码
        lnjs_source = ""
        try:
            lnjs_path = os.path.join(plugin_dir, "assets", "line-number", "line-number.js")
            if os.path.exists(lnjs_path):
                with open(lnjs_path, "r", encoding="utf-8") as f:
                    lnjs_source = f.read()
        except Exception as e:
            logger.error(f"读取行号插件失败: {e}")
            lnjs_source = ""

        custom_lang_scripts = self._generate_hljs_language_registrations()

        # 避免内联脚本中出现 </script> 终止标签
        full_hljs_source = (hljs_source or '') + (lnjs_source or '') + custom_lang_scripts
        hljs_inline = full_hljs_source.replace("</script>", "<\\/script>") if full_hljs_source else ""

        # 将代码安全转义后塞进 template
        escaped_code = html.escape(code)
        language_class = language if language else ""

        # 行号配置
        use_line_numbers = (
            line_numbers_override
            if line_numbers_override is not None
            else (self.config.get("line_numbers_enabled", True) if self.config else True)
        )
        start_from = (
            self.config.get("line_numbers_start_from", 1)
            if (self.config and isinstance(self.config.get("line_numbers_start_from", 1), int))
            else 1
        )
        single_line = (
            self.config.get("line_numbers_single_line", False)
            if self.config
            else False
        )

        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <style>
    {font_face_css}
    body {{
        margin: 0;
        padding: 20px;
        background: #1e1e1e;
        min-height: 100vh;
        display: flex;
        align-items: flex-start;
    }}
    pre {{
        margin: 0;
        font-size: {font_size}px;
        line-height: 1.5;
        font-family: {font_family};
        white-space: pre-wrap;
        word-wrap: break-word;
    }}
    .code-container {{
        display: block;
        padding: 16px 20px;
        border-radius: 12px;
        box-shadow: 0 10px 30px rgba(0,0,0,0.4);
        min-width: 600px;
        width: fit-content;
        max-width: 1100px;
    }}
    {hljs_theme_css}
    </style>
    <script>{hljs_inline}</script>
    <script>
    (function () {{
        var ENABLE_LINE_NUMBERS = {str(bool(use_line_numbers)).lower()};
        var LN_OPTIONS = {{ startFrom: {start_from}, singleLine: {str(bool(single_line)).lower()} }};
        function applyHighlight() {{
            const blocks = document.querySelectorAll('pre code');
            for (const block of blocks) {{
                try {{
                    if (!window.hljs) {{
                        console.error('highlight.js not loaded');
                        continue;
                    }}
                    
                    const classes = Array.from(block.classList);
                    const hasLanguage = classes.some(cls => cls.startsWith('language-') && cls !== 'language-');
                    
                    if (hasLanguage) {{
                        window.hljs.highlightElement(block);
                    }} else {{
                        const result = window.hljs.highlightAuto(block.textContent);
                        block.innerHTML = result.value;
                        block.className = 'hljs ' + result.language;
                    }}
                    if (ENABLE_LINE_NUMBERS && window.hljs && typeof window.hljs.lineNumbersBlock === 'function') {{
                        window.hljs.lineNumbersBlock(block, LN_OPTIONS);
                    }}
                }} catch (e) {{
                    console.error('highlight.js error', e);
                }}
            }}
        }}

        function waitForHLJS(retry) {{
            retry = retry || 0;
            if (window.hljs && typeof window.hljs.highlightElement === 'function') {{
                applyHighlight();
            }} else if (retry < 100) {{
                setTimeout(function () {{ waitForHLJS(retry + 1); }}, 50);
            }} else {{
                console.warn('highlight.js not available after waiting');
            }}
        }}

        if (document.readyState === 'complete' || document.readyState === 'interactive') {{
            waitForHLJS(0);
        }} else {{
            document.addEventListener('DOMContentLoaded', function () {{ waitForHLJS(0); }});
        }}
    }})();
    </script>
</head>
<body>
    <div class="code-container">
        <pre><code class="hljs{' language-' + language_class if language_class else ''}">{escaped_code}</code></pre>
    </div>
</body>
</html>
"""

        filename = f"{uuid.uuid4().hex}.png"
        file_path = os.path.join(self.temp_dir, filename)

        await self._ensure_playwright_browser()

        # 使用共享浏览器实例渲染截图
        if not self._browser:
            # 如果由于某些原因浏览器未启动，尝试补救启动一次
            try:
                if not self._playwright:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
                logger.info("CodeRender Playwright 浏览器在渲染时重新启动")
            except Exception as e:
                logger.error(f"渲染时启动 Playwright 浏览器失败: {e}")
                raise

        page = await self._browser.new_page(viewport={"width": 1200, "height": 800})
        await page.set_content(html_content, wait_until="networkidle")
        
        element = await page.query_selector('.code-container')
        if element:
            await element.screenshot(path=file_path)
        else:
            await page.screenshot(path=file_path, full_page=True)
        
        await page.close()

        return file_path

    def _parse_render_args(self, args_str: str) -> dict:
        """解析渲染参数
        
        支持的参数格式:
        - lang=python 或 -l python
        - theme=dracula 或 -t dracula  
        - size=16 或 -s 16
        - noline 或 -n (不显示行号)
        - line 或 -ln (显示行号)
        
        Returns:
            解析后的参数字典
        """
        result = {
            "language": None,
            "theme": None,
            "font_size": None,
            "line_numbers": None,
            "remaining": ""  # 剩余的代码内容
        }
        
        if not args_str:
            return result
        
        parts = args_str.split()
        remaining_parts = []
        i = 0
        
        while i < len(parts):
            part = parts[i]
            
            # 解析 lang= 或 -l
            if part.startswith("lang="):
                result["language"] = part[5:]
            elif part == "-l" and i + 1 < len(parts):
                i += 1
                result["language"] = parts[i]
            # 解析 theme= 或 -t
            elif part.startswith("theme="):
                result["theme"] = part[6:]
            elif part == "-t" and i + 1 < len(parts):
                i += 1
                result["theme"] = parts[i]
            # 解析 size= 或 -s
            elif part.startswith("size="):
                try:
                    result["font_size"] = int(part[5:])
                except ValueError:
                    pass
            elif part == "-s" and i + 1 < len(parts):
                i += 1
                try:
                    result["font_size"] = int(parts[i])
                except ValueError:
                    pass
            # 解析行号开关
            elif part in ("noline", "-n", "--no-line"):
                result["line_numbers"] = False
            elif part in ("line", "-ln", "--line"):
                result["line_numbers"] = True
            else:
                # 不是参数，加入剩余部分
                remaining_parts.append(part)
            
            i += 1
        
        result["remaining"] = " ".join(remaining_parts)
        return result

    @filter.command("render")
    async def render_code(
        self,
        event: AstrMessageEvent,
    ):
        """渲染代码为图片。
        
        用法: /render [参数] [代码]
        
        参数:
        - language 或 -l: 指定语言 (如 python, js)
        - theme 或 -t: 指定主题 (monokai, dracula, github-dark, one-dark, vs-dark, nord)
        - size 或 -s: 指定字体大小
        - noline 或 -n: 不显示行号
        - line 或 -ln: 显示行号
        
        示例:
        /render -l python -t dracula print("hello")
        /render lang=js theme=nord console.log("hi")
        """
        # 检查黑名单
        if self._is_group_blocked(event):
            return
        
        # 解析原始消息获取参数（支持多行：从第二行开始解析参数）
        raw_text = event.message_str or ""
        lines = raw_text.splitlines()

        message_text = ""
        if lines:
            # 仅使用第二行及之后的内容作为参数区，第一行只保留命令本身
            rest_lines = lines[1:]
            if rest_lines:
                message_text = " ".join(l.strip() for l in rest_lines if l.strip())
        
        # 解析参数
        parsed = self._parse_render_args(message_text)
        
        # 合并参数（完全依赖消息文本动态解析）
        final_language = parsed["language"]
        final_theme = parsed["theme"]
        final_size = parsed["font_size"]
        final_line_numbers = parsed["line_numbers"]
        
        # 获取引用的消息
        reply_message = None
        
        # 尝试获取引用消息的内容
        messages = event.get_messages()
        if messages and isinstance(messages[0], Reply):
            reply_seg = messages[0]
            reply_content = ""
            
            # 1. 尝试从 chain 获取
            if hasattr(reply_seg, 'chain') and reply_seg.chain:
                for seg in reply_seg.chain:
                    if isinstance(seg, Plain):
                        reply_content += seg.text
                    elif hasattr(seg, 'text'):
                        reply_content += str(seg.text)
            
            # 2. 如果 chain 为空或提取失败，尝试 message_str
            if not reply_content and hasattr(reply_seg, 'message_str') and reply_seg.message_str:
                reply_content = reply_seg.message_str
                
            # 3. 尝试 origin (旧版兼容)
            if not reply_content and hasattr(reply_seg, 'origin') and reply_seg.origin:
                reply_content = str(reply_seg.origin)
            
            reply_message = reply_content
        
        # 如果没有引用消息，使用解析后的剩余内容
        if not reply_message:
            if parsed["remaining"]:
                reply_message = parsed["remaining"]
            else:
                yield event.plain_result(
                    "❌ 请引用一条包含代码的消息，或直接在命令后附带代码。\n\n"
                    "用法: /render [参数] [代码]\n\n"
                    "参数:\n"
                    "• -l <语言> 或 lang=<语言>\n"
                    "• -t <主题> 或 theme=<主题>\n"
                    "• -s <字号> 或 size=<字号>\n"
                    "• -n 或 noline (不显示行号)\n\n"
                    "示例: /render -l python -t dracula print('hello')"
                )
                return
        
        # 提取代码
        code, detected_lang = self._extract_code_from_message(reply_message)
        
        if not code or len(code.strip()) == 0:
            yield event.plain_result("❌ 未能从消息中提取到代码")
            return
        
        # 确定语言
        if not final_language:
            final_language = detected_lang
        if not final_language:
            final_language = self._detect_language(code)
        
        # 获取语言显示名称
        lang_display = final_language
        theme_display = final_theme or (self.config.get("theme", "monokai") if self.config else "monokai")
        
        try:
            # 发送处理中提示
            # yield event.plain_result(f"🎨 正在渲染 {lang_display} 代码 (主题: {theme_display})...")
            
            # 渲染代码
            image_path = await self._render_code_to_image(
                code, 
                final_language,
                theme_override=final_theme,
                font_size_override=final_size,
                line_numbers_override=final_line_numbers
            )
            
            if not os.path.exists(image_path):
                yield event.plain_result("❌ 渲染失败：图片生成失败")
                return
            
            # 发送图片
            result = MessageEventResult()
            result.chain.append(ImageComponent(file=image_path))
            
            yield result
            
            logger.info(f"代码渲染成功: {lang_display}, 主题: {theme_display}, {len(code)} 字符")
            
        except Exception as e:
            logger.error(f"渲染代码时发生错误: {e}")
            yield event.plain_result(f"❌ 渲染失败: {str(e)}")


    @filter.command("render_file")
    async def render_file(
        self, 
        event: AstrMessageEvent,
    ):
        """渲染代码文件为图片。
        
        用法: 引用文件消息后发送 /render_file [参数]
        
        参数:
        - theme 或 -t: 指定主题
        - size 或 -s: 指定字体大小
        - noline 或 -n: 不显示行号
        - language 或 -l: 强制指定语言（通常不需要，会自动根据文件名识别）
        """
        # 检查黑名单
        if self._is_group_blocked(event):
            return

        # 解析原始消息获取参数 (支持多行，从第二行开始解析参数)
        raw_text = event.message_str or ""
        lines = raw_text.splitlines()

        message_text = ""
        if lines:
            # 仅使用第二行及之后的内容作为参数区，第一行只保留命令本身
            rest_lines = lines[1:]
            if rest_lines:
                message_text = " ".join(l.strip() for l in rest_lines if l.strip())

        parsed = self._parse_render_args(message_text)
        
        # 合并参数（完全依赖消息文本动态解析）
        final_language = parsed["language"]
        final_theme = parsed["theme"]
        final_size = parsed["font_size"]
        final_line_numbers = parsed["line_numbers"]

        # 获取引用的消息中的文件
        target_file = None
        file_name = ""
        
        messages = event.get_messages()
        if messages and isinstance(messages[0], Reply):
            reply_seg = messages[0]
            if hasattr(reply_seg, 'chain') and reply_seg.chain:
                for seg in reply_seg.chain:
                    if isinstance(seg, File):
                        target_file = seg
                        file_name = seg.name or "unknown"
                        break
        
        if not target_file:
            yield event.plain_result("❌ 请引用一条包含文件的消息")
            return

        try:
            # 获取文件路径 (get_file 会自动下载)
            file_path = await target_file.get_file()
            
            if not file_path or not os.path.exists(file_path):
                yield event.plain_result("❌ 文件获取失败")
                return
                
            # 读取文件内容
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    code = f.read()
            except UnicodeDecodeError:
                # 尝试其他编码
                try:
                    with open(file_path, "r", encoding="gbk") as f:
                        code = f.read()
                except Exception:
                    yield event.plain_result("❌ 文件编码不支持 (仅支持 UTF-8 和 GBK)")
                    return
            except Exception as e:
                yield event.plain_result(f"❌ 读取文件失败: {str(e)}")
                return
            
            if not code or len(code.strip()) == 0:
                yield event.plain_result("❌ 文件内容为空")
                return

            # 确定语言 (优先使用强制指定的，否则根据文件名检测)
            if not final_language:
                final_language = self._detect_language(code, filename=file_name)
            
            # 获取显示名称
            lang_display = final_language
            theme_display = final_theme or (self.config.get("theme", "monokai") if self.config else "monokai")

            # 渲染
            image_path = await self._render_code_to_image(
                code,
                final_language,
                theme_override=final_theme,
                font_size_override=final_size,
                line_numbers_override=final_line_numbers
            )
            
            if not os.path.exists(image_path):
                yield event.plain_result("❌ 渲染失败：图片生成失败")
                return
            
            # 发送图片
            result = MessageEventResult()
            result.chain.append(ImageComponent(file=image_path))
            yield result
            
            logger.info(f"文件渲染成功: {file_name}, 语言: {lang_display}, 主题: {theme_display}")
            
        except Exception as e:
            logger.error(f"处理文件渲染时发生错误: {e}")
            yield event.plain_result(f"❌ 处理失败: {str(e)}")

    @filter.llm_tool(name="render_code_to_image")
    async def render_code_image(
        self, event: AstrMessageEvent, code: str,language: str = "",theme: str = "github"
    ) -> MessageEventResult:
        """
        将代码渲染为图片并发送。

        Args:
            code(str): 要渲染的代码
            language(str): 代码语言. 建议填写。
            theme(str): 主题名称. 建议为idea-light
        """
        if not code or not code.strip():
            logger.warning("代码不能为空")
            yield event.plain_result("❌ 代码不能为空")
            return
            
        logger.info(f"正在渲染代码: language={language}, theme={theme}")
        
        try:
            # 渲染代码为图片
            image_path = await self._render_code_to_image(
                code=code,
                language=language,
                theme_override=theme,
                line_numbers_override=True
            )
            
            if not os.path.exists(image_path):
                logger.error("渲染失败：图片生成失败")
                yield event.plain_result("❌ 代码渲染失败：无法生成图片")
                return
            
            # 发送图片
            result = MessageEventResult()
            result.chain.append(ImageComponent(file=image_path))
            
            logger.info(f"代码渲染成功: {len(code)} 字符")
            yield result
            
        except Exception as e:
            logger.error(f"渲染代码时发生错误: {e}")
            yield event.plain_result(f"❌ 渲染失败: {str(e)}")
    
    async def terminate(self):
        """插件销毁时清理"""
        # 先清理临时文件
        await self._cleanup_temp_files()

        # 关闭 Playwright 浏览器
        try:
            if self._browser:
                await self._browser.close()
                self._browser = None
                logger.info("CodeRender Playwright 浏览器已关闭")
        except Exception as e:
            logger.error(f"关闭 Playwright 浏览器时出错: {e}")

        try:
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
                logger.info("CodeRender Playwright 实例已停止")
        except Exception as e:
            logger.error(f"停止 Playwright 实例时出错: {e}")

    def _generate_hljs_language_registrations(self) -> str:
        """生成自定义语言的 highlight.js 注册代码"""
        if not self.custom_languages:
            return ""
        
        registrations = []
        
        for lang_id, lang_def in self.custom_languages.items():
            # 生成 highlight.js 语言定义
            hljs_def = self._convert_to_hljs_definition(lang_id, lang_def)
            registrations.append(hljs_def)
        
        return "\n".join(registrations)
    
    def _convert_to_hljs_definition(self, lang_id: str, lang_def: dict) -> str:
        """将自定义语言定义转换为 highlight.js 注册代码"""
        name = lang_def.get("name", lang_id)
        aliases = json.dumps(lang_def.get("aliases", []))
        keywords = self._format_hljs_keywords(lang_def.get("keywords", {}))
        
        # 构建 contains 数组
        contains = ["hljs.C_LINE_COMMENT_MODE", "hljs.C_BLOCK_COMMENT_MODE"]
        
        # 添加字符串模式
        if lang_def.get("strings"):
            contains.append(self._format_string_mode(lang_def["strings"]))
        else:
            contains.append("""
            {
                className: 'string',
                variants: [
                    hljs.QUOTE_STRING_MODE,
                    hljs.APOS_STRING_MODE
                ]
            }""")
        
        # 添加数字模式
        if lang_def.get("numbers"):
            contains.append(self._format_number_mode(lang_def["numbers"]))
        else:
            contains.append("hljs.C_NUMBER_MODE")
        
        # 添加其他自定义模式
        if lang_def.get("patterns"):
            for pattern in lang_def["patterns"]:
                contains.append(self._format_custom_pattern(pattern))
        
        contains_str = ",\n                ".join(contains)
        
        return f"""
;(function() {{
    function {lang_id}Language(hljs) {{
        return {{
            name: '{name}',
            aliases: {aliases},
            keywords: {keywords},
            contains: [
                {contains_str}
            ]
        }};
    }}

    if (typeof window !== 'undefined' && window.hljs && !window.hljs.getLanguage('{lang_id}')) {{
        window.hljs.registerLanguage('{lang_id}', {lang_id}Language);
    }}
}})();
"""
    
    def _format_hljs_keywords(self, keywords: dict | list) -> str:
        """格式化关键字为 highlight.js 格式"""
        if isinstance(keywords, list):
            # 简单列表形式，转换为字符串
            return json.dumps(" ".join(keywords))
        elif isinstance(keywords, dict):
            # 字典形式，保留分类
            formatted = {}
            for key, value in keywords.items():
                if isinstance(value, list):
                    formatted[key] = " ".join(value)
                else:
                    formatted[key] = value
            return json.dumps(formatted)
        else:
            return "{}"
    
    def _format_string_mode(self, string_config: dict) -> str:
        """格式化字符串模式"""
        variants = []
        if string_config.get("double_quote", True):
            variants.append("hljs.QUOTE_STRING_MODE")
        if string_config.get("single_quote", True):
            variants.append("hljs.APOS_STRING_MODE")
        if string_config.get("backtick"):
            variants.append("{ begin: '`', end: '`' }")
        
        return f"""{{
                className: 'string',
                variants: [{", ".join(variants)}]
            }}"""
    
    def _format_number_mode(self, number_config: dict) -> str:
        """格式化数字模式"""
        if number_config.get("use_default", True):
            return "hljs.C_NUMBER_MODE"
        
        variants = []
        if number_config.get("binary"):
            variants.append("{ begin: /0[bB][01]+/ }")
        if number_config.get("octal"):
            variants.append("{ begin: /0[oO][0-7]+/ }")
        if number_config.get("hex"):
            variants.append("{ begin: /0[xX][0-9A-Fa-f]+/ }")
        if number_config.get("decimal", True):
            variants.append("{ begin: /\\d+(\\.\\d+)?([eE][+-]?\\d+)?/ }")
        
        return f"""{{
                className: 'number',
                variants: [{", ".join(variants)}],
                relevance: 0
            }}"""
    
    def _format_custom_pattern(self, pattern: dict) -> str:
        """格式化自定义模式"""
        class_name = pattern.get("className", "")
        begin = pattern.get("begin", "")
        end = pattern.get("end", "")
        keywords = pattern.get("keywords", "")
        
        parts = [f"className: '{class_name}'"]
        if begin:
            parts.append(f"begin: /{begin}/")
        if end:
            parts.append(f"end: /{end}/")
        if keywords:
            parts.append(f"keywords: '{keywords}'")
        
        return f"""{{
                {", ".join(parts)}
            }}"""

    def _extract_code_from_message(self, text: str) -> tuple[str, str]:
        """从消息中提取代码和语言提示
        
        Returns:
            (code, language_hint)
        """
        # 匹配 markdown 代码块 \`\`\`language\ncode\`\`\`
        code_block_pattern = r'\`\`\`(\w*)\n?([\s\S]*?)\`\`\`'
        match = re.search(code_block_pattern, text)
        if match:
            lang_hint = match.group(1) or None
            code = match.group(2).strip()
            return code, lang_hint
        
        # 匹配单行代码 `code`
        inline_code_pattern = r'`([^`]+)`'
        match = re.search(inline_code_pattern, text)
        if match:
            return match.group(1), None
        
        # 没有代码块标记，返回原文本
        return text.strip(), None

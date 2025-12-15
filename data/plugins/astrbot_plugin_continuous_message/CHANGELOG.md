# 更新日志

## v2.1.0 (2025-12-01)

### 新增功能
- ✨ **QQ合并转发消息支持**（仅aiocqhttp平台）
  - 自动检测并提取用户直接发送的合并转发消息
  - 自动检测并提取用户回复/引用的合并转发消息
  - 提取聊天记录中的文本内容和图片URL
  - 合并转发内容自动纳入防抖流程，与后续消息一起处理

### 配置项新增
- `enable_forward_analysis` (bool, 默认: true)
  - 控制是否启用合并转发消息分析功能
  
- `forward_prefix` (string, 默认: "【合并转发内容】\n")
  - 在提取的合并转发内容前添加的标识前缀

### 技术改进
- 导入 `json` 模块用于解析合并转发消息内容
- 导入 `astrbot.api.message_components` 作为 `Comp`
- 添加 `aiocqhttp` 平台检测机制
- 新增 `_detect_forward_message()` 方法：检测合并转发消息
- 新增 `_extract_forward_content()` 方法：提取合并转发内容
- 新增 `_parse_raw_content()` 方法：解析原始消息内容（支持JSON字符串和列表）
- 更新 `handle_private_msg()` 方法：在消息处理流程开始时检测和提取合并转发

### 工作流程
```
用户发送消息
    ↓
检测合并转发 (aiocqhttp平台)
    ↓
提取合并转发内容 (文本 + 图片)
    ↓
解析常规消息内容
    ↓
合并两部分内容
    ↓
进入防抖流程
    ↓
结算并发送给LLM
```

### 兼容性
- ✅ 向后兼容v2.0.0的所有功能
- ✅ 非aiocqhttp平台不受影响（自动禁用合并转发功能）
- ✅ 关闭 `enable_forward_analysis` 后行为与v2.0.0完全相同

---

## v2.0.0 (2024)

### 重大更新
- 🎉 全新事件驱动架构
- 使用 `asyncio.Event` 实现异步等待（0 CPU占用）
- 使用 `asyncio.Task.cancel()` 实现精确计时器重置
- 重构消息事件机制，与框架和其他插件完美兼容

### 核心功能
- 智能消息合并（私聊模式）
- 指令消息智能过滤
- 图片识别和传递
- 精确计时器控制
- 灵活配置选项

### 配置项
- `enable` (bool, 默认: true)
- `debounce_time` (float, 默认: 2.0)
- `command_prefixes` (list, 默认: ["/"])
- `merge_separator` (string, 默认: "\n")

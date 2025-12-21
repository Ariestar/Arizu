# LEARNING_LOG.md

## [2025-12-16] 统一记忆插件重构
- **原生知识库集成**: 通过 `context.get_registered_star()` 获取第三方知识库插件实例，直接复用其组件（vector_db、text_splitter、file_parser等），避免重复实现
- **命令冲突解决**: 不注册 `/kb` 命令组，完全依赖第三方知识库插件，避免命令冲突。用户直接使用第三方插件的命令接口
- **统一检索架构**: `UnifiedRetriever` 同时检索知识库和记忆系统，使用 `ResultFuser` 融合结果，支持权重配置
- **记忆类型系统**: `MemoryType` 枚举（conversation/knowledge/learning），`MemoryItem` 统一模型，`DocumentAdapter` 实现双向转换
- **处理器模式**: `BaseProcessor` 抽象基类，`ConversationProcessor`/`KnowledgeProcessor`/`LearningProcessor` 分别处理不同类型记忆
- **智能检索**: `MultiDimRetriever` 多维度检索，`RelevanceRanker` 多因子评分（相关性/重要性/新鲜度/来源），`ResultFuser` RRF/加权融合
- **存储抽象**: `BaseVectorStore` 抽象接口，`VectorEngine` 管理器，支持 Faiss/Milvus Lite/Milvus，降级使用策略
- **第三方插件集成**: 即使第三方插件（`astrbot_plugin_knowledge_base`）不是官方插件，也可以通过 AstrBot 的插件注册机制调用其接口，实现无缝集成

## [2025-12-16] Git版本控制
- **gitignore配置**: 通过`.gitignore`文件排除不需要版本控制的文件，避免提交临时文件和敏感数据
- **Python项目忽略规则**: `__pycache__/` `*.pyc` `venv/` `.env` 等Python特有文件，防止提交字节码和虚拟环境
- **机器人项目忽略项**: `data/cache/` `data/logs/` `models/checkpoints/` 等运行时生成的数据文件，保持仓库清洁
- **AstrBot项目定制**: `data/data_v4.db*` `data/knowledge_base/` `ntqq/` `napcat/` 等运行时数据库和QQ客户端数据，防止提交大量敏感数据
- **插件数据隔离**: `data/plugin_data/` `data/plugins_data/` `data/self_learning_data/` 等插件运行时数据完全排除，保持代码仓库纯净

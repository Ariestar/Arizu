# ARIZU - Custom AstrBot Deployment

[English](#english) | [中文](#chinese)

<a name="english"></a>
## 🇬🇧 English

This repository serves as a **deployment and configuration** hub for a customized [AstrBot](https://github.com/Soulter/AstrBot) instance. It bundles a suite of plugins, configurations, and a Docker Compose setup for easy deployment.

### ✨ Features

This bot is powered by AstrBot and enhanced with the following key plugins:

*   **Core & Learning**:
    *   `astrbot_plugin_self_learning`: Advanced self-learning capabilities to adapt to user interactions.
    *   `astrbot_plugin_mnemosyne`: Memory management for long-term context.
*   **Utilities**:
    *   `latexplotter`: Renders LaTeX formulas as images for chat.
    *   `astrbot_plugin_code_renderer`: Renders code snippets into images.
    *   `astrbot_plugin_github_monitor_shell`: Monitors GitHub repositories for updates.
    *   `astrbot_plugin_disaster_warning`: Provides disaster warning alerts.
*   **Social & Interaction**:
    *   `astrbot_plugin_group_chat_plus`: Enhancements for group chat management.
    *   `astrbot_plugin_meme_manager`: Manages and sends memes.
    *   `astrbot_plugin_qzone`: Integration with Qzone.
    *   `astrbot_plugin_heartflow`: Affection and mood tracking system.
    *   `astrbot_plugin_proactive_chat`: Allows the bot to initiate conversations.

### 🚀 Deployment Guide

This project uses **Docker Compose** for orchestration, integrating **AstrBot** with **NapCat** (OneBot 11 implementation).

#### Prerequisites

*   Docker
*   Docker Compose

#### Quick Start

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/your-username/ARIZU.git
    cd ARIZU
    ```

2.  **Configure Environment**:
    Ensure the `astrbot.yml` file matches your environment needs (ports, volumes).

3.  **Start Services**:
    ```bash
    docker compose -f astrbot.yml up -d
    ```

4.  **Access Web UI**:
    *   AstrBot Dashboard: `http://localhost:6185`
    *   NapCat Dashboard: `http://localhost:6099`

#### Directory Structure

*   `astrbot.yml`: The Docker Compose configuration file defining the services.
*   `data/`: The main data directory mounted to `/AstrBot/data` in the container.
    *   `config/`: Configuration files for AstrBot and plugins.
    *   `plugins/`: Installed plugins source code.
*   `napcat/`: Configuration for the NapCat OneBot client.
*   `ntqq/`: Persistent data for the QQ client (login info, etc.).

### ⚙️ Configuration

*   **AstrBot Config**: Modify files in `data/config/` to adjust bot settings and LLM providers.
*   **Plugin Config**: specific plugin configurations can be found in their respective JSON files within `data/config/` or the plugin directories.

---

<a name="chinese"></a>
## 🇨🇳 中文 (Chinese)

本项目是一个定制化的 [AstrBot](https://github.com/Soulter/AstrBot) **部署与配置**仓库。它集成了多个实用插件、预设配置以及 Docker Compose 编排文件，旨在实现开箱即用的便捷部署。

### ✨ 功能特性

本机器人基于 AstrBot 驱动，并集成了以下核心增强插件：

*   **核心与学习**:
    *   `astrbot_plugin_self_learning`: 高级自学习能力，适应用户交互风格。
    *   `astrbot_plugin_mnemosyne`: 记忆管理系统，用于维护长对话上下文。
*   **实用工具**:
    *   `latexplotter`: 将 LaTeX 数学公式渲染为图片发送。
    *   `astrbot_plugin_code_renderer`: 将代码片段渲染为美观的图片。
    *   `astrbot_plugin_github_monitor_shell`: 监控 GitHub 仓库动态并推送通知。
    *   `astrbot_plugin_disaster_warning`: 提供即时的自然灾害预警。
*   **社交与互动**:
    *   `astrbot_plugin_group_chat_plus`: 增强的群聊管理功能。
    *   `astrbot_plugin_meme_manager`: 表情包管理与发送。
    *   `astrbot_plugin_qzone`: QQ 空间互通集成。
    *   `astrbot_plugin_heartflow`: 情感与心情追踪系统。
    *   `astrbot_plugin_proactive_chat`: 赋予机器人主动发起对话的能力。

### 🚀 部署指南

本项目使用 **Docker Compose** 进行服务编排，整合了 **AstrBot** 与 **NapCat** (OneBot 11 实现)。

#### 前置要求

*   Docker
*   Docker Compose

#### 快速开始

1.  **克隆仓库**:
    ```bash
    git clone https://github.com/your-username/ARIZU.git
    cd ARIZU
    ```

2.  **环境配置**:
    检查 `astrbot.yml` 文件，确保端口和挂载卷符合您的服务器环境。

3.  **启动服务**:
    ```bash
    docker compose -f astrbot.yml up -d
    ```

4.  **访问 Web 控制台**:
    *   AstrBot 管理面板: `http://localhost:6185`
    *   NapCat 管理面板: `http://localhost:6099`

#### 目录结构说明

*   `astrbot.yml`: Docker Compose 配置文件，定义服务编排。
*   `data/`: 核心数据目录，挂载至容器内的 `/AstrBot/data`。
    *   `config/`: AstrBot 本体及各插件的配置文件。
    *   `plugins/`: 已安装插件的源代码。
*   `napcat/`: NapCat (OneBot 客户端) 的配置目录。
*   `ntqq/`: QQ 客户端的持久化数据 (登录信息等)。

### ⚙️ 配置说明

*   **AstrBot 配置**: 修改 `data/config/` 目录下的文件来调整机器人设置和 LLM 模型提供商。
*   **插件配置**: 各插件的详细配置位于 `data/config/` 下的对应 JSON 文件，或直接位于插件目录中。

## 📝 许可证

请参考原 [AstrBot](https://github.com/Soulter/AstrBot) 项目许可证以及各独立插件的许可证。

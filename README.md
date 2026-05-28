# 商机雷达 (WeChat Mini-Program 版)

## 项目简介
这是一个自动化商机获取工具，抓取招投标信息，利用 DeepSeek AI 进行筛选，并通过微信小程序展示。

## 目录结构
- `backend/`: Python FastAPI 后端
- `miniprogram/`: 微信小程序前端
- `docs/`: 项目文档

## 快速开始

### 1. 后端设置

**前置条件**: Python 3.8+

1. 进入后端目录:
   ```bash
   cd backend
   ```

2. 创建并激活虚拟环境 (可选但推荐):
   ```bash
   py -m venv venv
   .\venv\Scripts\activate
   ```

3. 安装依赖:
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

4. 检查 `.env` 配置:
   确保 `DEEPSEEK_API_KEY` 已正确设置。

5. 启动服务:
   ```bash
   py main.py
   ```
   服务将运行在 `http://127.0.0.1:8000`。
   API 文档: `http://127.0.0.1:8000/docs`

### 2. 小程序设置

1. 下载并安装 [微信开发者工具](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)。
2. 打开工具，选择“导入项目”。
3. 目录选择本项目的 `miniprogram` 文件夹。
4. AppID 可以使用测试号。
5. 在开发者工具中，点击“详情” -> “本地设置”，勾选“不校验合法域名、web-view（业务域名）、TLS版本以及HTTPS证书”（开发环境连接本地 API 需要）。

## 使用说明

1. 启动后端服务。
2. 在小程序中，点击“刷新/抓取”按钮触发模拟爬虫。
3. 后端会调用 DeepSeek API 分析模拟数据。
4. 分析完成后，小程序首页将显示商机列表。

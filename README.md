# Bilibili Music Downloader

> 一个用 Flask + 原生 HTML/JS 写的小工具：解析 B 站视频的 DASH 音频流，提供一键下载音频文件的网页界面。仅用于学习、个人自用，**不要商用或大规模分发**。

在线示例（如果你有在跑）：  
https://bilibili-music.top/

---

## 功能概览

- 支持 **B 站视频链接 / BV 号** 输入  
- 调用 B 站开放接口，解析 DASH 音频列表
- 展示每个分 P 的所有可用音轨，包括：
  - 容器格式（`m4a/webm`）
  - 编码（`AAC / HE-AAC / Opus`）
  - 码率（kbps 粗略估算）
- 通过后端 `/audio` 接口代理下载：
  - 自动补上 `User-Agent` / `Referer` 等防盗链头
  - 浏览器直接下载，文件名附带标题、P 序号、码率与扩展名
- 简单玻璃拟态风格前端，背景图可自定义

---

## 架构简介

- 后端：Python + Flask
  - `/api/parse`：解析 BV → 拉取视频信息 → 返回每个分 P 的音频清单（DASH audio）
  - `/audio`：代理上游的音频直链，解决 CORS / 防盗链 / 临时 URL 等问题，并附上下载文件名
- 前端：静态 `static/index.html`
  - 纯原生 JS，调用 `/api/parse` 获取数据
  - 根据返回的音频清单动态渲染下载按钮
  - 提供「遇到失败时禁用断点」选项，以适配部分 CDN 对 Range 的兼容问题

---

## 本地运行（开发）

### 1. 克隆代码

```bash
git clone https://github.com/<你的用户名>/<你的仓库名>.git
cd <你的仓库名>
```

### 2. 创建虚拟环境并安装依赖

要求 Python 3.9+（推荐 3.10/3.11）：

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows 下用 .venv\Scripts\activate

pip install -r requirements.txt
```
### 3. 运行开发服务器
```bash
python app.py
```
默认监听在：http://127.0.0.1:5173/
浏览器打开这个地址即可访问页面。

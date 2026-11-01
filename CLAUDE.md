删除任务时清理中间文件，如卡片、临时文件、模型运行记录。
遇到错误，修复时，不要执着于打补丁，要归纳成一类问题去解决。

## 项目文档

- 当前产品规则与既有界面规范分别见 `docs/PRODUCT.md`、`docs/DESIGN.md`；修改功能或页面前读取相关规则，不擅自重排已有页面。
- 文档索引在 `docs/README.md`；archive、learning、career 中可能含旧架构和失效路径，不将其当成当前实现。

## 运行环境

- 本项目的 Python 解释器固定为 conda 环境：`E:/anaconda/envs/TravelAssistant/python.exe`（Windows 路径 `E:\anaconda\envs\TravelAssistant\python.exe`）。跑测试、启动服务、执行脚本一律用它，例如：
  - 测试：`E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated -q`
  - 后端启动（仓库根目录）：`E:/anaconda/envs/TravelAssistant/python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000`
- **不要使用 PATH 上的默认 `python`**：`C:\Windows\system32\python` 是 Windows Store 执行别名桩，会吞掉 stdout/stderr 且恒返回退出码 0，测试"通过"是假象；`py` 启动器指向的 `D:\python\jieshiqi\python.exe` 也启动失败。
- 依赖已装在该 conda 环境内，无需 venv；缺依赖时用 `E:/anaconda/envs/TravelAssistant/python.exe -m pip install -r requirements.txt`。
- 前端位于 `frontend/`，使用 Vue 3 + TypeScript + Vite；在该目录执行 `npm ci`、`npm run dev`、`npm run build`、`npm test`。开发浏览器访问 Vite 地址，`/api` 经代理访问 FastAPI；不要把模型/地图密钥写入 `VITE_*`。
- Python 只有 `backend/app` 一个规范 `app` 包；不使用 `backend.app` 或根目录兼容包。仍为单后端进程，不增加 Uvicorn workers。

规则

1.判断先当假设

2.不迎合也不抬杠

3.外部证据优先

4.无新证据不改口

5.分清事实和未知

6.接受前主动找反例

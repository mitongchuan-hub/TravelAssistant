删除任务时清理中间文件，如卡片、临时文件、模型运行记录。
遇到错误，修复时，不要执着于打补丁，要归纳成一类问题去解决。

## 运行环境

- 本项目的 Python 解释器固定为 conda 环境：`E:/anaconda/envs/TravelAssistant/python.exe`（Windows 路径 `E:\anaconda\envs\TravelAssistant\python.exe`）。跑测试、启动服务、执行脚本一律用它，例如：
  - 测试：`E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated -q`
  - 启动：`E:/anaconda/envs/TravelAssistant/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000`
- **不要使用 PATH 上的默认 `python`**：`C:\Windows\system32\python` 是 Windows Store 执行别名桩，会吞掉 stdout/stderr 且恒返回退出码 0，测试"通过"是假象；`py` 启动器指向的 `D:\python\jieshiqi\python.exe` 也启动失败。
- 依赖已装在该 conda 环境内，无需 venv；缺依赖时用 `E:/anaconda/envs/TravelAssistant/python.exe -m pip install -r requirements.txt`。

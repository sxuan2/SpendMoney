# SpendMoney

这是一个“收据图片 OCR 识别 + 手动修正金额”的小项目。

## 功能

- 支持网页上传收据图片，自动 OCR 识别
- 也可以扫描 `uploaded_receipts/` 里的收据图片
- 使用 PaddleOCR 识别图片文字
- 将识别结果写入 SQLite 数据库 `finance.db`
- 把已处理图片移动到 `processed_receipts/`
- 用 FastAPI 提供一个网页，手动修正金额为 0 的记录

## 环境要求

- Windows
- Python 3.10 或 3.11
- Conda（使用环境名：`pytest`）

## 一键运行方式

### 第一次（必须）

1. 双击 `setup_ocr.bat`
2. 等依赖安装完成（会安装到 conda `pytest` 环境）

### 日常启动

1. 双击 `run.bat`
2. 浏览器打开 `http://127.0.0.1:8000`
3. 上传图片 -> 预览 -> 确认识别 -> 查看 OCR 文本

> 启动脚本已改为直接使用 `conda run -n pytest`，不依赖终端激活状态。

### 手机访问

- 手机和电脑必须在同一个 Wi-Fi 下
- 电脑端运行后，手机不要访问 `127.0.0.1:8000`，要访问电脑的局域网 IP，例如 `http://192.168.1.10:8000`
- 如果打不开，检查 Windows 防火墙是否拦截了 8000 端口

### 方式 2：手动运行

在项目根目录执行：

```bash
conda activate pytest
pip install -r requirements.txt
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

## 使用顺序

1. 启动服务后打开首页
2. 上传后先预览，点击“确认并识别”
3. 在首页下方补全金额并提交
4. 识别结果页会展示 OCR 文本

## 数据文件

- `finance.db`：SQLite 数据库
- `processed_receipts/`：已处理图片
- `uploaded_receipts/`：待处理图片

## 页面说明

- 首页上方：上传图片，先预览再确认识别
- 首页下方：显示 `amount = 0` 的记录，供手动修正
- 结果页：显示识别状态 + OCR 文本 + 图片

## 故障说明

如果结果页状态是 `ocr_failed`：

1. 先运行一次 `setup_ocr.bat`
2. 重启 `run.bat`
3. 重新上传测试

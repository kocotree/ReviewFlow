FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖：
# - libjpeg62-turbo: pdfplumber 需要
# - libreoffice-writer: docx/doc → PDF 转换（供豆包文件模态直传）
# - fonts-noto-cjk: 转换中文文档时避免字体缺失导致乱码/丢字
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo \
    libreoffice-writer \
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# 安装锁定的 Python 依赖
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# 运行用户不授予 shell/root 权限；LibreOffice 临时文件写入 /tmp。
RUN groupadd --system --gid 10001 reviewflow \
    && useradd --system --uid 10001 --gid reviewflow --home-dir /app reviewflow

# 复制源码
COPY --chown=reviewflow:reviewflow . .

USER reviewflow

# 暴露端口
EXPOSE 8000

# 启动服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

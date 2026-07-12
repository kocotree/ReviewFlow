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

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 暴露端口
EXPOSE 8000

# 启动服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

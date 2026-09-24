FROM python:3.11-slim

# 仅用 Python 标准库，无需联网安装依赖
WORKDIR /srv/app
COPY app/ ./

RUN mkdir -p /data && chmod 777 /data

# 角色通过命令参数选择：api | executor | instrument
# HEALTH_PORT 指向该进程监听端口（compose 各服务自身已配置健康检查）
HEALTHCHECK --interval=5s --timeout=3s --start-period=2s --retries=10 \
    CMD python -c "import os,sys,urllib.request; p=int(os.environ.get('HEALTH_PORT','8080')); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/healthz', timeout=2).status==200 else 1)" || exit 1

ENTRYPOINT ["python", "-u", "/srv/app/run.py"]
CMD ["api"]

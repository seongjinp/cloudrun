ARG imagepath

FROM ${imagepath}/back-baseimage:latest

WORKDIR /app
COPY proxy_main.py /app/proxy_main.py
COPY config.yaml /app/config.yaml
COPY env.py /app/env.py

# Cloud Run이 주입하는 $PORT를 그대로 따른다.
CMD ["sh", "-c", "python3 /app/proxy_main.py --config /app/config.yaml --host 0.0.0.0 --port 80"]

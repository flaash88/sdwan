FROM python:3.12-alpine
RUN apk add --no-cache wireguard-tools iptables ip6tables iproute2 wireguard-go
WORKDIR /app
COPY agent.py /app/agent.py
VOLUME ["/data"]
ENV PYTHONUNBUFFERED=1
CMD ["python", "/app/agent.py"]

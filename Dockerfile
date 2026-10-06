# Multi-stage build: compile Go server
FROM golang:1.21-alpine AS go-builder
WORKDIR /build
COPY server/ ./
RUN go build -o sverdrup-server main.go

# Runtime image
FROM alpine:3.18
RUN apk add --no-cache python3 py3-pip sqlite dcron

# Copy Go server
COPY --from=go-builder /build/sverdrup-server /usr/local/bin/

# Copy Python code
COPY collector/ /app/collector/
COPY processor/ /app/processor/
RUN cd /app/collector && pip install --no-cache-dir -r requirements.txt

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create data directory
RUN mkdir -p /data

EXPOSE 8080

# Entrypoint starts both crond and the Go server
ENTRYPOINT ["/entrypoint.sh"]

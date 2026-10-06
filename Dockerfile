# Multi-stage build: compile Go server
FROM golang:1.26-alpine AS go-builder
WORKDIR /build
COPY server/go.mod server/go.sum ./
RUN go mod download
COPY server/ ./
# The SQLite driver is pure Go, so the binary needs no C toolchain or libc.
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o sverdrup-server .

# Runtime image
FROM alpine:3.18
# The collector and processor use only the Python standard library;
# tzdata lets the processor resolve TZ for daily metrics.
RUN apk add --no-cache python3 sqlite dcron tzdata

# Copy Go server
COPY --from=go-builder /build/sverdrup-server /usr/local/bin/

# Copy Python code
COPY sverdrup/ /app/sverdrup/
COPY collector/ /app/collector/
COPY processor/ /app/processor/

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create data directory
RUN mkdir -p /data

EXPOSE 8080

# Entrypoint starts both crond and the Go server
ENTRYPOINT ["/entrypoint.sh"]

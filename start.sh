#!/usr/bin/env bash
# disklib service control: start | stop | restart | status
set -u

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$APP_DIR/disklib.pid"
LOG_FILE="$APP_DIR/disklib.log"
PORT=5050
URL="http://127.0.0.1:$PORT/"

cmd="${1:-status}"

is_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
    fi
    # fallback: find via port (handles stale PID file or externally-started process)
    local pid
    pid=$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print}' | grep -oP 'pid=\K[0-9]+' | head -1)
    if [[ -n "$pid" ]]; then
        echo "$pid"
        return 0
    fi
    return 1
}

case "$cmd" in
    start)
        if pid=$(is_running); then
            echo "already running (pid $pid) on $URL"
            exit 0
        fi
        cd "$APP_DIR"
        nohup python3 app.py >>"$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        sleep 1
        if pid=$(is_running); then
            echo "started (pid $pid) → $URL  (log: $LOG_FILE)"
        else
            echo "failed to start — check $LOG_FILE" >&2
            exit 1
        fi
        ;;

    stop)
        if pid=$(is_running); then
            kill "$pid"
            for _ in {1..10}; do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.3
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "process $pid did not exit gracefully — sending SIGKILL"
                kill -9 "$pid" 2>/dev/null
            fi
            rm -f "$PID_FILE"
            echo "stopped (pid $pid)"
        else
            echo "not running"
            rm -f "$PID_FILE"
        fi
        ;;

    restart)
        "$0" stop
        sleep 1
        "$0" start
        ;;

    status)
        if pid=$(is_running); then
            echo "running (pid $pid) on $URL"
            http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$URL" 2>/dev/null || echo "—")
            echo "http: $http_code"
        else
            echo "not running"
            exit 3
        fi
        ;;

    *)
        echo "usage: $0 {start|stop|restart|status}" >&2
        exit 2
        ;;
esac

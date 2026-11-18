"""Open the local workspace on Windows, macOS, or Linux."""
import sys
import webbrowser
from http.server import ThreadingHTTPServer

import server


def main():
    server.load_env()
    server.initialize()
    host, port = "127.0.0.1", 8765
    try:
        httpd = ThreadingHTTPServer((host, port), server.Handler)
    except OSError:
        print("8765 端口已被使用。若已启动潮汐，请打开 http://127.0.0.1:8765 。")
        print("也可以运行 python server.py --port 8787 使用其他端口。")
        return 1
    url = f"http://{host}:{port}"
    print(f"潮汐已启动  {url}\n请保持此窗口打开。按 Ctrl+C 可停止服务。", flush=True)
    with httpd:
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

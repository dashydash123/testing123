#!/usr/bin/env python3
"""
HuggingFace Model BOM - local backend.

Why this exists
---------------
The tool needs data from huggingface.co, api.github.com, pypi.org, wikidata and
arxiv. When a browser sits behind an isolation proxy (e.g. Zscaler Browser
Isolation), fetches made *from the page* are blocked. Python's own HTTPS calls
are not subject to browser isolation, so this script fetches the external URLs
server-side; the page only ever talks to http://localhost.

All the BOM logic still lives in project.html. This file only:
  1. serves project.html, and
  2. exposes /proxy?url=... which fetches the upstream URL in Python and
     returns the response (status + body) to the page.

Standard library only - nothing to pip install.
"""

import http.server
import socketserver
import socket
import urllib.request
import urllib.parse
import urllib.error
import ssl
import json
import os
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "project.html")
DEFAULT_UA = "HF-BOM/1.0"
TIMEOUT = 30  # seconds per upstream request


def build_ssl_context():
    """
    Verify TLS the same way the machine already does, so the corporate
    SSL-inspection root (installed in the OS trust store) is trusted with no
    manual cert wrangling.

    Preference order:
      1. `truststore` package  -> uses the OS trust store natively (if present)
      2. REQUESTS_CA_BUNDLE / SSL_CERT_FILE env var pointing at a CA bundle
      3. Python default context -> on Windows this loads the Windows cert
         store, which already contains the corporate root.
    """
    try:
        import truststore  # optional; not required
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass

    ctx = ssl.create_default_context()
    ca = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca and os.path.exists(ca):
        try:
            ctx.load_verify_locations(ca)
        except Exception:
            pass
    return ctx


SSL_CTX = build_ssl_context()


class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, code, body, content_type="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html", "/project.html"):
            return self._serve_html()
        if parsed.path == "/proxy":
            return self._do_proxy(parsed)
        self._send(404, "Not found")

    def _serve_html(self):
        try:
            with open(HTML_FILE, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return self._send(500, "project.html not found next to this script.")
        self._send(200, data, "text/html; charset=utf-8")

    def _do_proxy(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        target = (qs.get("url") or [""])[0]
        if not target.startswith(("http://", "https://")):
            return self._send(400, "bad or missing url")

        headers = {"User-Agent": DEFAULT_UA}
        raw = self.headers.get("X-Upstream-Headers")
        if raw:
            try:
                for k, v in json.loads(raw).items():
                    if k and v:
                        headers[k] = v
            except Exception:
                pass
        headers.setdefault("User-Agent", DEFAULT_UA)  # GitHub API rejects requests with no UA

        req = urllib.request.Request(target, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "application/octet-stream")
                return self._send(resp.status, body, ctype)
        except urllib.error.HTTPError as e:
            # Pass the upstream status + body straight through, so the page's
            # `response.ok` check behaves exactly as it did before.
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            ctype = "text/plain"
            if e.headers:
                ctype = e.headers.get("Content-Type", "text/plain")
            return self._send(e.code, body, ctype)
        except Exception as e:
            return self._send(502, "proxy error: " + str(e))


class ThreadingHTTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def pick_port(start=8000, tries=25):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def main():
    if not os.path.exists(HTML_FILE):
        print("ERROR: project.html must be in the same folder as this script.")
        try:
            input("Press Enter to exit...")
        except EOFError:
            pass
        return

    port = pick_port()
    url = "http://localhost:%d/" % port
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    line = "=" * 58
    print(line)
    print("  HuggingFace Model BOM  -  local server running")
    print("  " + url)
    print("  Keep this window open. Close it to stop the tool.")
    print(line)

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

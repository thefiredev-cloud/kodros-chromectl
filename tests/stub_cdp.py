"""In-process stub of the Chromium DevTools HTTP routes plus a minimal WebSocket CDP endpoint.

HTTP:  /json/version, /json/list, /json/close/ID, /json/activate/ID
WS:    /devtools/browser/<uuid> and /devtools/page/<id>
CDP:   Target.createTarget, Target.closeTarget, Page.enable, Page.navigate,
       Page.captureScreenshot, Runtime.evaluate, Emulation.setFocusEmulationEnabled,
       Input.insertText, Input.dispatchKeyEvent

It binds 127.0.0.1 on an ephemeral port. It never connects anywhere.
"""
import base64
import hashlib
import json
import re
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
BROWSER_ID = "BROWSER-0000"
VERSION = "StubChrome/1.2.3.4"


class Tab:
    def __init__(self, tid, url, title=None, body="", elements=()):
        self.id = tid
        self.url = url
        self.title = title if title is not None else url
        self.body = body
        self.elements = set(elements)  # CSS selectors that "exist"
        self.clicked = []
        self.focused = []
        self.inserted = []
        self.keys = []
        self.focus_emulation = False
        self.navigations = []


class StubCDP:
    """Start with .start(); read .endpoint ('127.0.0.1:PORT'); inspect .tabs and .log."""

    def __init__(self):
        self.tabs = {}
        self.log = []          # (kind, detail) tuples in arrival order
        self.activated = []
        self.next = 0
        self.lock = threading.Lock()
        self.httpd = None
        self.thread = None
        self.port = None

    # -- lifecycle -------------------------------------------------------
    def start(self):
        stub = self

        class Handler(_Handler):
            owner = stub

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    @property
    def endpoint(self):
        return f"127.0.0.1:{self.port}"

    # -- state -----------------------------------------------------------
    def add_tab(self, url="about:blank", title=None, body="", elements=(), tid=None):
        with self.lock:
            self.next += 1
            tid = tid or f"TAB{self.next:04d}0000000000"
            self.tabs[tid] = Tab(tid, url, title, body, elements)
            return self.tabs[tid]

    def calls(self, kind):
        return [d for k, d in self.log if k == kind]

    # -- CDP dispatch ----------------------------------------------------
    def dispatch(self, scope, method, params):
        self.log.append(("cdp", method))
        if method == "Target.createTarget":
            self.log.append(("createTarget", dict(params)))
            tab = self.add_tab(params.get("url", "about:blank"))
            return {"targetId": tab.id}
        if method == "Target.closeTarget":
            tid = params.get("targetId")
            with self.lock:
                ok = self.tabs.pop(tid, None) is not None
            self.log.append(("closeTarget", tid))
            return {"success": ok}
        tab = self.tabs.get(scope)
        if tab is None:
            raise KeyError(f"no such target {scope}")
        if method == "Emulation.setFocusEmulationEnabled":
            tab.focus_emulation = bool(params.get("enabled"))
            return {}
        if method == "Page.enable":
            return {}
        if method == "Page.navigate":
            tab.url = params["url"]
            tab.title = params["url"]
            tab.navigations.append(params["url"])
            return {"frameId": tab.id}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(PNG_1X1).decode()}
        if method == "Input.insertText":
            tab.inserted.append(params["text"])
            return {}
        if method == "Input.dispatchKeyEvent":
            tab.keys.append((params["type"], params.get("key"), params.get("text")))
            return {}
        if method == "Runtime.evaluate":
            return self.evaluate(tab, params["expression"])
        raise KeyError(f"unknown method {method}")

    def evaluate(self, tab, expr):
        def val(v):
            return {"result": {"type": type(v).__name__, "value": v}}

        if expr == "document.readyState":
            return val("complete")
        if expr == "location.href":
            return val(tab.url)
        if "document.body.innerText" in expr:
            return val(tab.body)
        m = re.search(r"document\.querySelector\((\".*\")\)\)$", expr)
        if m and "e.click()" in expr:
            sel = json.loads(m.group(1))
            hit = sel in tab.elements
            if hit:
                tab.clicked.append(sel)
            return val(hit)
        if m and "e.focus()" in expr:
            sel = json.loads(m.group(1))
            hit = sel in tab.elements
            if hit:
                tab.focused.append(sel)
            return val(hit)
        table = {"1+1": 2, "document.title": tab.title, "({a:[1,2]})": {"a": [1, 2]}}
        if expr in table:
            return val(table[expr])
        return {"exceptionDetails": {"text": "Uncaught",
                                     "exception": {"description": f"ReferenceError: {expr} is not defined"}},
                "result": {"type": "object"}}


class _Handler(BaseHTTPRequestHandler):
    owner = None  # set by subclass

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _tab_json(self, t):
        host = self.headers.get("Host")
        return {"id": t.id, "type": "page", "title": t.title, "url": t.url,
                "webSocketDebuggerUrl": f"ws://{host}/devtools/page/{t.id}"}

    def do_GET(self):
        o = self.owner
        path = self.path
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._websocket(path)
        o.log.append(("http", path))
        if path == "/json/version":
            host = self.headers.get("Host")
            return self._json({"Browser": VERSION, "Protocol-Version": "1.3",
                               "webSocketDebuggerUrl": f"ws://{host}/devtools/browser/{BROWSER_ID}"})
        if path in ("/json/list", "/json"):
            with o.lock:
                tabs = list(o.tabs.values())
            # a non-page target, which chromectl must filter out
            extra = [{"id": "SW0000", "type": "service_worker", "title": "sw", "url": "https://x/sw.js",
                      "webSocketDebuggerUrl": f"ws://{self.headers.get('Host')}/devtools/page/SW0000"}]
            return self._json([self._tab_json(t) for t in tabs] + extra)
        if path.startswith("/json/close/"):
            tid = path.rsplit("/", 1)[1]
            with o.lock:
                ok = o.tabs.pop(tid, None) is not None
            body = b"Target is closing" if ok else b"No such target id"
            self.send_response(200 if ok else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/json/activate/"):
            tid = path.rsplit("/", 1)[1]
            o.activated.append(tid)
            body = b"Target activated"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not found"}, 404)

    # -- websocket -------------------------------------------------------
    def _websocket(self, path):
        o = self.owner
        scope = path.rsplit("/", 1)[1]
        o.log.append(("ws", path))
        accept = base64.b64encode(
            hashlib.sha1((self.headers["Sec-WebSocket-Key"] + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        while True:
            try:
                msg = self._read_frame()
            except EOFError:
                return
            if msg is None:
                return
            req = json.loads(msg)
            try:
                result = o.dispatch(scope, req["method"], req.get("params", {}))
                self._send_frame({"id": req["id"], "result": result})
            except KeyError as e:
                self._send_frame({"id": req["id"], "error": {"code": -32601, "message": str(e.args[0])}})

    def _recv(self, n):
        out = b""
        while len(out) < n:
            chunk = self.rfile.read(n - len(out))
            if not chunk:
                raise EOFError
            out += chunk
        return out

    def _read_frame(self):
        data = b""
        while True:
            b0, b1 = self._recv(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv(8))[0]
            mask = self._recv(4) if b1 & 0x80 else b"\0\0\0\0"
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv(n)))
            if b0 & 0x0F == 0x8:
                return None
            data += payload
            if b0 & 0x80:
                return data

    def _send_frame(self, obj):
        data = json.dumps(obj).encode()
        n = len(data)
        if n < 126:
            hdr = bytes([0x81, n])
        elif n < 1 << 16:
            hdr = bytes([0x81, 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([0x81, 127]) + struct.pack(">Q", n)
        self.wfile.write(hdr + data)

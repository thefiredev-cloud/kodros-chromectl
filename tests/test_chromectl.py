"""chromectl tests. Everything runs against the in-process stub in stub_cdp.py.

A socket guard makes any connect to a port other than the stub's fail the test, so
these tests can never reach a real Chromium on 127.0.0.1:9333 (or the remote ports).
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from .stub_cdp import PNG_1X1, VERSION, StubCDP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "chromectl")


def load_module():
    loader = importlib.machinery.SourceFileLoader("chromectl_under_test", SCRIPT)
    spec = importlib.util.spec_from_loader("chromectl_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class StubCase(unittest.TestCase):
    def setUp(self):
        self.stub = StubCDP().start()
        self.addCleanup(self.stub.stop)
        self.blocked = []
        real_connect = socket.socket.connect
        stub_port = self.stub.port
        blocked = self.blocked

        def guarded_connect(sock, address):
            if not (isinstance(address, tuple) and address[0] == "127.0.0.1" and address[1] == stub_port):
                blocked.append(address)
                raise ConnectionRefusedError(f"test guard: refused connect to {address}")
            return real_connect(sock, address)

        p = mock.patch.object(socket.socket, "connect", guarded_connect)
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"CHROMECTL_ENDPOINT": self.stub.endpoint})
        env.start()
        self.addCleanup(env.stop)
        # no ssh tunnels, ever
        sp = mock.patch("subprocess.run", side_effect=AssertionError("subprocess.run must not be called"))
        sp.start()
        self.addCleanup(sp.stop)
        self.ctl = load_module()
        self._close_websockets()
        self.addCleanup(lambda: self.assertEqual(self.blocked, [], "connect to a non-stub address attempted"))

    def _close_websockets(self):
        """chromectl never closes its WS sockets (the process exits); close them here."""
        opened = []
        orig_init = self.ctl.WS.__init__

        def tracking_init(ws, url):
            orig_init(ws, url)
            opened.append(ws.s)

        p = mock.patch.object(self.ctl.WS, "__init__", tracking_init)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: [s.close() for s in opened])

    def run_ctl(self, *argv):
        """Return (exit_code, stdout, exit_message). Exit code None means normal return."""
        out = io.StringIO()
        code, msg = None, ""
        with contextlib.redirect_stdout(out):
            try:
                self.ctl.main(list(argv))
            except SystemExit as e:
                if isinstance(e.code, str):
                    code, msg = 1, e.code
                else:
                    code = e.code
        return code, out.getvalue(), msg


class TestGuard(StubCase):
    def test_guard_blocks_owner_chromium_port(self):
        s = socket.socket()
        self.addCleanup(s.close)
        with self.assertRaises(ConnectionRefusedError):
            s.connect(("127.0.0.1", 9333))
        self.assertEqual(self.blocked, [("127.0.0.1", 9333)])
        self.blocked.clear()

    def test_endpoint_override_wins_for_both_hosts(self):
        self.assertEqual(self.ctl.endpoint("local"), self.stub.endpoint)
        self.assertEqual(self.ctl.endpoint("remote"), self.stub.endpoint)

    def test_default_local_endpoint_is_9333_without_override(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CHROMECTL_ENDPOINT")
            self.assertEqual(self.ctl.endpoint("local"), "127.0.0.1:9333")  # string only, no connect


class TestReadCommands(StubCase):
    def test_status_prints_version_and_endpoint(self):
        code, out, _ = self.run_ctl("status")
        self.assertIsNone(code)
        self.assertEqual(out.strip(), f"{VERSION} {self.stub.endpoint}")

    def test_tabs_lists_pages_only_with_8_char_ids(self):
        a = self.stub.add_tab("https://a.test/", title="Alpha")
        b = self.stub.add_tab("https://b.test/", title="Beta")
        code, out, _ = self.run_ctl("tabs")
        self.assertIsNone(code)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2, out)  # service_worker target filtered out
        self.assertTrue(lines[0].startswith(a.id[:8]) and "Alpha" in lines[0] and "https://a.test/" in lines[0])
        self.assertTrue(lines[1].startswith(b.id[:8]) and "Beta" in lines[1])
        self.assertNotIn("SW0000", out)

    def test_tabs_empty(self):
        code, out, _ = self.run_ctl("tabs")
        self.assertIsNone(code)
        self.assertEqual(out, "")

    def test_text_prints_body(self):
        t = self.stub.add_tab("https://a.test/", body="hello world")
        code, out, _ = self.run_ctl("text", t.id[:6])
        self.assertIsNone(code)
        self.assertEqual(out.strip(), "hello world")
        self.assertTrue(t.focus_emulation)

    def test_eval_number_string_object(self):
        t = self.stub.add_tab("https://a.test/", title="T1")
        self.assertEqual(json.loads(self.run_ctl("eval", t.id[:6], "1+1")[1]), 2)
        self.assertEqual(json.loads(self.run_ctl("eval", t.id[:6], "document.title")[1]), "T1")
        self.assertEqual(json.loads(self.run_ctl("eval", t.id[:6], "({a:[1,2]})")[1]), {"a": [1, 2]})

    def test_eval_js_error_exits_with_description(self):
        t = self.stub.add_tab("https://a.test/")
        code, _, msg = self.run_ctl("eval", t.id[:6], "nope()")
        self.assertEqual(code, 1)
        self.assertIn("ReferenceError", msg)

    def test_shot_writes_png(self):
        t = self.stub.add_tab("https://a.test/")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.png")
            code, out, _ = self.run_ctl("shot", t.id[:6], path)
            self.assertIsNone(code)
            self.assertEqual(out.strip(), path)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), PNG_1X1)


class TestOpenNavClose(StubCase):
    def test_open_creates_background_tab(self):
        code, out, _ = self.run_ctl("open", "https://new.test/")
        self.assertIsNone(code)
        short = out.strip()
        self.assertEqual(len(short), 8)
        create = self.stub.calls("createTarget")
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0]["url"], "https://new.test/")
        self.assertIs(create[0]["background"], True)
        self.assertEqual(self.stub.activated, [])
        _, tabs_out, _ = self.run_ctl("tabs")
        self.assertIn(short, tabs_out)
        self.assertIn("https://new.test/", tabs_out)

    def test_nav_changes_url_and_prints_it(self):
        t = self.stub.add_tab("https://a.test/")
        code, out, _ = self.run_ctl("nav", t.id[:6], "https://b.test/page")
        self.assertIsNone(code)
        self.assertEqual(out.strip(), "https://b.test/page")
        self.assertEqual(t.navigations, ["https://b.test/page"])
        self.assertEqual(t.url, "https://b.test/page")

    def test_close_removes_tab_over_http(self):
        t = self.stub.add_tab("https://a.test/")
        other = self.stub.add_tab("https://keep.test/")
        code, out, _ = self.run_ctl("close", t.id[:8])
        self.assertIsNone(code)
        self.assertEqual(out.strip(), f"closed {t.id[:8]}")
        self.assertNotIn(t.id, self.stub.tabs)
        self.assertIn(other.id, self.stub.tabs)

    def test_target_close_target_over_websocket(self):
        t = self.stub.add_tab("https://a.test/")
        url = self.ctl.http(self.stub.endpoint, "/json/version")["webSocketDebuggerUrl"]
        ws = self.ctl.WS(url)
        self.addCleanup(ws.s.close)
        self.assertEqual(ws.call("Target.closeTarget", targetId=t.id), {"success": True})
        self.assertNotIn(t.id, self.stub.tabs)

    def test_unknown_or_ambiguous_prefix_exits(self):
        self.stub.add_tab("https://a.test/", tid="ABCD1111")
        self.stub.add_tab("https://b.test/", tid="ABCD2222")
        code, _, msg = self.run_ctl("close", "ABCD")
        self.assertEqual(code, 1)
        self.assertIn("2 matches", msg)
        code, _, msg = self.run_ctl("close", "ZZZZ")
        self.assertEqual(code, 1)
        self.assertIn("0 matches", msg)
        self.assertEqual(len(self.stub.tabs), 2)

    def test_prefix_is_case_insensitive(self):
        t = self.stub.add_tab("https://a.test/", tid="ABCD1111")
        code, out, _ = self.run_ctl("close", "abcd")
        self.assertIsNone(code)
        self.assertNotIn(t.id, self.stub.tabs)


class TestInput(StubCase):
    def test_click_hit_and_miss(self):
        t = self.stub.add_tab("https://a.test/", elements=["#go"])
        code, out, _ = self.run_ctl("click", t.id[:6], "#go")
        self.assertIsNone(code)
        self.assertEqual(out.strip(), "clicked")
        self.assertEqual(t.clicked, ["#go"])
        code, _, msg = self.run_ctl("click", t.id[:6], "#missing")
        self.assertEqual(code, 1)
        self.assertIn("no element: #missing", msg)

    def test_type_focuses_and_inserts(self):
        t = self.stub.add_tab("https://a.test/", elements=["input[name=q]"])
        code, out, _ = self.run_ctl("type", t.id[:6], "input[name=q]", "hello")
        self.assertIsNone(code)
        self.assertEqual(out.strip(), "typed")
        self.assertEqual(t.focused, ["input[name=q]"])
        self.assertEqual(t.inserted, ["hello"])

    def test_type_missing_element(self):
        t = self.stub.add_tab("https://a.test/")
        code, _, msg = self.run_ctl("type", t.id[:6], "#nope", "x")
        self.assertEqual(code, 1)
        self.assertIn("no element: #nope", msg)
        self.assertEqual(t.inserted, [])

    def test_key_enter_sends_down_char_up(self):
        t = self.stub.add_tab("https://a.test/")
        code, out, _ = self.run_ctl("key", t.id[:6], "Enter")
        self.assertIsNone(code)
        self.assertEqual(out.strip(), "key Enter")
        self.assertEqual(t.keys, [("rawKeyDown", "Enter", None), ("char", "Enter", "\r"), ("keyUp", "Enter", None)])

    def test_key_escape_has_no_char_event(self):
        t = self.stub.add_tab("https://a.test/")
        self.run_ctl("key", t.id[:6], "Escape")
        self.assertEqual([k[0] for k in t.keys], ["rawKeyDown", "keyUp"])

    def test_key_plain_letter_sends_char(self):
        t = self.stub.add_tab("https://a.test/")
        self.run_ctl("key", t.id[:6], "a")
        self.assertEqual(t.keys, [("rawKeyDown", "a", None), ("char", "a", "a"), ("keyUp", "a", None)])


class TestFront(StubCase):
    def test_front_refused_without_flag_and_never_connects(self):
        t = self.stub.add_tab("https://a.test/")
        before = list(self.stub.log)
        code, out, msg = self.run_ctl("front", t.id[:6])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("changes what the owner sees", msg)
        self.assertIn("--owner-visible", msg)
        self.assertEqual(self.stub.activated, [])
        self.assertEqual(self.stub.log, before, "refusal must not contact the browser at all")

    def test_front_refused_for_remote_host_too(self):
        t = self.stub.add_tab("https://a.test/")
        code, _, msg = self.run_ctl("--host", "remote", "front", t.id[:6])
        self.assertEqual(code, 1)
        self.assertIn("changes what the owner sees", msg)
        self.assertEqual(self.stub.activated, [])
        self.assertEqual(self.stub.log, [])

    def test_front_with_flag_after_id(self):
        t = self.stub.add_tab("https://a.test/")
        code, out, _ = self.run_ctl("front", t.id[:6], "--owner-visible")
        self.assertIsNone(code)
        self.assertEqual(out.strip(), f"front {t.id[:8]}")
        self.assertEqual(self.stub.activated, [t.id])

    def test_front_with_flag_before_id(self):
        t = self.stub.add_tab("https://a.test/")
        code, out, _ = self.run_ctl("front", "--owner-visible", t.id[:6])
        self.assertIsNone(code)
        self.assertEqual(self.stub.activated, [t.id])

    def test_front_flag_without_id_is_usage_error(self):
        code, _, msg = self.run_ctl("front", "--owner-visible")
        self.assertEqual(code, 1)
        self.assertIn("usage: chromectl front ID --owner-visible", msg)
        self.assertEqual(self.stub.log, [])

    def test_front_flag_only_on_front(self):
        t = self.stub.add_tab("https://a.test/")
        self.run_ctl("tabs")
        self.assertEqual(self.stub.activated, [])

    def test_no_other_command_activates_a_tab(self):
        t = self.stub.add_tab("https://a.test/", body="x", elements=["#a"])
        self.run_ctl("status")
        self.run_ctl("tabs")
        self.run_ctl("open", "https://n.test/")
        self.run_ctl("nav", t.id[:6], "https://b.test/")
        self.run_ctl("text", t.id[:6])
        self.run_ctl("click", t.id[:6], "#a")
        self.run_ctl("key", t.id[:6], "Tab")
        self.run_ctl("close", t.id[:6])
        self.assertEqual(self.stub.activated, [])


class TestUsage(StubCase):
    def test_no_args_prints_usage(self):
        code, _, msg = self.run_ctl()
        self.assertEqual(code, 1)
        self.assertIn("Usage: chromectl", msg)

    def test_bad_host_prints_usage(self):
        code, _, msg = self.run_ctl("--host", "mars", "status")
        self.assertEqual(code, 1)
        self.assertIn("Usage: chromectl", msg)

    def test_usage_documents_owner_visible(self):
        self.assertIn("front ID --owner-visible", self.ctl.__doc__)


class TestCliProcess(unittest.TestCase):
    """Run the real script as a child process, still against the stub only."""

    def setUp(self):
        self.stub = StubCDP().start()
        self.addCleanup(self.stub.stop)
        self.env = dict(os.environ, CHROMECTL_ENDPOINT=self.stub.endpoint)
        self.assertNotIn(":9333", self.env["CHROMECTL_ENDPOINT"])

    def cli(self, *argv):
        return subprocess.run([sys.executable, SCRIPT, *argv], env=self.env, capture_output=True,
                              text=True, timeout=30)

    def test_status_prints_browser_version(self):
        r = self.cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"{VERSION} {self.stub.endpoint}")

    def test_front_refusal_exit_code_and_message(self):
        t = self.stub.add_tab("https://a.test/")
        r = self.cli("front", t.id[:6])
        self.assertEqual(r.returncode, 1)
        self.assertIn("changes what the owner sees", r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.stub.activated, [])
        self.assertEqual(self.stub.log, [])

    def test_front_with_flag_activates(self):
        t = self.stub.add_tab("https://a.test/")
        r = self.cli("front", t.id[:6], "--owner-visible")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"front {t.id[:8]}")
        self.assertEqual(self.stub.activated, [t.id])

    def test_open_then_text_then_close_round_trip(self):
        r = self.cli("open", "https://rt.test/")
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = r.stdout.strip()
        self.stub.tabs[next(i for i in self.stub.tabs if i.startswith(tid))].body = "round trip"
        self.assertEqual(self.cli("text", tid).stdout.strip(), "round trip")
        self.assertEqual(self.cli("close", tid).stdout.strip(), f"closed {tid}")
        self.assertEqual(self.stub.tabs, {})


if __name__ == "__main__":
    unittest.main()

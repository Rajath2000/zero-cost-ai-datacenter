#!/usr/bin/env python3
"""Datacenter control panel: a small local web UI for a Windows GPU box reached over SSH.

Run:  DC_HOST=<windows-lan-ip> python3 dc_panel.py   (then open http://127.0.0.1:8765)
Listens on 127.0.0.1 only, and runs only the fixed actions defined below.

Configuration (environment variables):
  DC_HOST          Windows machine's LAN IP (required)
  DC_SSH_ALIAS     Host alias in ~/.ssh/config (default: datacenter)
  DC_NAME          Name shown in the page header (default: Datacenter PC)
  DC_WIN_FOLDER    Folder VS Code opens on Windows (default: C:/)
  DC_OLLAMA_URL    Fixed Ollama URL; disables the automatic SSH-tunnel fallback
  DC_PANEL_PORT    Local port for the panel (default: 8765)
"""
import html
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST_IP = os.environ.get("DC_HOST") or sys.exit("Set DC_HOST to the Windows machine's LAN IP")
SSH_ALIAS = os.environ.get("DC_SSH_ALIAS", "datacenter")
OLLAMA_URL = os.environ.get("DC_OLLAMA_URL", f"http://{HOST_IP}:11434")
PORT = int(os.environ.get("DC_PANEL_PORT", "8765"))
DC_NAME = os.environ.get("DC_NAME", "Datacenter PC")
WIN_FOLDER = os.environ.get("DC_WIN_FOLDER", "C:/")

SERVICES = {"ssh": 22, "ollama": 11434, "rustdesk": 21118}


def port_open(port, timeout=1.5):
    try:
        with socket.create_connection((HOST_IP, port), timeout=timeout):
            return True
    except OSError:
        return False


def terminal(cmd):
    script = f'tell application "Terminal" to do script "{cmd}"\ntell application "Terminal" to activate'
    subprocess.Popen(["osascript", "-e", script])


def open_vscode():
    uri = f"vscode-remote://ssh-remote+{SSH_ALIAS}/{WIN_FOLDER.lstrip('/')}"
    try:
        subprocess.Popen(["code", "--folder-uri", uri])
    except FileNotFoundError:
        subprocess.Popen(["open", "-a", "Visual Studio Code"])


ACTIONS = {
    "terminal": lambda: terminal(f"ssh {SSH_ALIAS}"),
    "claude": lambda: terminal(f"ssh -t {SSH_ALIAS} claude"),
    "gpu": lambda: terminal(f"ssh -t {SSH_ALIAS} nvidia-smi -l 2"),
    "rustdesk": lambda: subprocess.Popen(["open", "-a", "RustDesk"]),
    "vscode": open_vscode,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, obj, code=200):
        # Keep the Windows IP out of anything shown in the browser (e.g. error messages).
        body = json.dumps(obj).replace(HOST_IP, "windows-pc").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path.startswith("/api/"):
            ensure_ollama()
        if self.path == "/":
            body = PAGE.replace("{{NAME}}", html.escape(DC_NAME)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            status = {name: port_open(p) for name, p in SERVICES.items()}
            try:
                urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3).close()
                status["ollama"] = True
            except Exception:
                status["ollama"] = False
            self.send_json(status)
        elif self.path == "/api/models":
            try:
                with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as r:
                    models = [m["name"] for m in json.load(r).get("models", [])]
                self.send_json({"models": models})
            except Exception as e:
                self.send_json({"error": str(e)}, 502)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/api/open/"):
            action = ACTIONS.get(self.path.rsplit("/", 1)[-1])
            if not action:
                return self.send_json({"error": "unknown action"}, 404)
            action()
            return self.send_json({"ok": True})
        if self.path == "/api/chat":
            ensure_ollama()
            data = self.read_json()
            payload = json.dumps({
                "model": data.get("model"),
                "messages": data.get("messages", []),
                "stream": True,
            }).encode()
            req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=payload,
                                         headers={"Content-Type": "application/json"})
            try:
                upstream = urllib.request.urlopen(req, timeout=300)
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            with upstream:
                for line in upstream:
                    self.wfile.write(line)
                    self.wfile.flush()
            return
        self.send_json({"error": "not found"}, 404)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Datacenter Panel</title>
<style>
:root {
  --bg: #f6f7f9; --card: #ffffff; --text: #16181d; --muted: #6b7280;
  --border: #e3e5ea; --accent: #2563eb; --ok: #16a34a; --bad: #dc2626;
  --user: #e8efff; --bot: #f1f2f4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --card: #171a21; --text: #e7e9ee; --muted: #9aa1ad;
    --border: #262a33; --accent: #5b8cff; --ok: #34d399; --bad: #f87171;
    --user: #1e2a47; --bot: #1d2028;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 24px 16px; display: grid; gap: 16px; }
header { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }
h1 { font-size: 22px; margin: 0; }
.sub { color: var(--muted); font-size: 13px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
.card h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 0 0 12px; }
.status { display: flex; gap: 16px; flex-wrap: wrap; }
.pill { display: flex; align-items: center; gap: 8px; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); }
.dot.ok { background: var(--ok); } .dot.bad { background: var(--bad); }
.actions { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px; }
button { font: inherit; cursor: pointer; border-radius: 8px; border: 1px solid var(--border);
  background: var(--card); color: var(--text); padding: 10px 12px; text-align: left; }
button:hover { border-color: var(--accent); }
button .t { font-weight: 600; display: block; } button .d { color: var(--muted); font-size: 12px; }
button.primary { background: var(--accent); color: #fff; border-color: var(--accent); text-align: center; }
.chat-top { display: flex; gap: 8px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
select, textarea { font: inherit; color: var(--text); background: var(--bg);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px; }
#log { height: 360px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding: 4px; }
.msg { padding: 8px 12px; border-radius: 10px; white-space: pre-wrap; max-width: 85%; }
.msg.user { background: var(--user); align-self: flex-end; }
.msg.bot { background: var(--bot); align-self: flex-start; }
.msg.err { color: var(--bad); }
.composer { display: flex; gap: 8px; margin-top: 10px; }
.composer textarea { flex: 1; resize: vertical; min-height: 44px; }
.hint { color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<main>
  <header>
    <h1>Datacenter</h1>
    <span class="sub">{{NAME}}</span>
  </header>

  <section class="card">
    <h2>Status</h2>
    <div class="status">
      <span class="pill"><span class="dot" id="s-ssh"></span>SSH</span>
      <span class="pill"><span class="dot" id="s-ollama"></span>Ollama</span>
      <span class="pill"><span class="dot" id="s-rustdesk"></span>RustDesk</span>
      <button id="refresh" style="margin-left:auto;padding:4px 10px">Refresh</button>
    </div>
  </section>

  <section class="card">
    <h2>Open</h2>
    <div class="actions">
      <button data-a="terminal"><span class="t">Terminal</span><span class="d">PowerShell on Windows</span></button>
      <button data-a="claude"><span class="t">Claude Code</span><span class="d">Runs on Windows hardware</span></button>
      <button data-a="vscode"><span class="t">VS Code</span><span class="d">Remote-SSH to Windows</span></button>
      <button data-a="rustdesk"><span class="t">Screen</span><span class="d">Open RustDesk</span></button>
      <button data-a="gpu"><span class="t">GPU monitor</span><span class="d">nvidia-smi, live</span></button>
    </div>
  </section>

  <section class="card">
    <h2>Ollama chat</h2>
    <div class="chat-top">
      <label for="model">Model</label>
      <select id="model"><option>loading…</option></select>
      <button id="clear" style="padding:6px 10px">New chat</button>
    </div>
    <div id="log"></div>
    <div class="composer">
      <textarea id="prompt" placeholder="Ask something… (Enter to send, Shift+Enter for new line)"></textarea>
      <button class="primary" id="send">Send</button>
    </div>
    <p class="hint">Models run on the Windows PC. Pull new ones with <code>ollama pull &lt;name&gt;</code> in the Terminal.</p>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
let history = [];

async function refreshStatus() {
  for (const k of ["ssh", "ollama", "rustdesk"]) $("s-" + k).className = "dot";
  try {
    const s = await (await fetch("/api/status")).json();
    for (const [k, v] of Object.entries(s)) $("s-" + k).className = "dot " + (v ? "ok" : "bad");
  } catch {}
}

async function loadModels() {
  const sel = $("model");
  try {
    const r = await (await fetch("/api/models")).json();
    if (r.error) throw new Error(r.error);
    let saved = null;
    try { saved = localStorage.getItem("dc-model"); } catch {}
    sel.replaceChildren(...r.models.map(m => new Option(m, m, false, m === saved)));
    if (!r.models.length) sel.replaceChildren(new Option("no models"));
  } catch (e) {
    sel.replaceChildren(new Option("Ollama unreachable"));
  }
}

function add(role, text) {
  const d = document.createElement("div");
  d.className = "msg " + role;
  d.textContent = text;
  $("log").appendChild(d);
  $("log").scrollTop = $("log").scrollHeight;
  return d;
}

async function send() {
  const text = $("prompt").value.trim();
  const model = $("model").value;
  if (!text) return;
  $("prompt").value = "";
  add("user", text);
  history.push({ role: "user", content: text });
  const out = add("bot", "…");
  let reply = "";
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, messages: history }),
    });
    if (!res.ok) throw new Error((await res.json()).error || res.statusText);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const j = JSON.parse(line);
        if (j.error) throw new Error(j.error);
        reply += j.message?.content || "";
        out.textContent = reply;
        $("log").scrollTop = $("log").scrollHeight;
      }
    }
    history.push({ role: "assistant", content: reply });
  } catch (e) {
    out.className = "msg bot err";
    out.textContent = "Error: " + e.message;
  }
}

document.querySelectorAll("[data-a]").forEach(b =>
  b.addEventListener("click", () => fetch("/api/open/" + b.dataset.a, { method: "POST" })));
$("refresh").onclick = () => { refreshStatus(); loadModels(); };
$("send").onclick = send;
$("clear").onclick = () => { history = []; $("log").innerHTML = ""; };
$("model").onchange = () => { try { localStorage.setItem("dc-model", $("model").value); } catch {} };
$("prompt").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
refreshStatus();
loadModels();
</script>
</body>
</html>
"""

TUNNEL = None
TUNNEL_PORT = 11435
TUNNEL_LOCK = threading.Lock()


def ensure_ollama():
    """Point OLLAMA_URL at Windows' Ollama, (re)starting an SSH tunnel when it isn't reachable directly.

    Called on every request, so the panel recovers after the Windows PC reboots or the Mac wakes from sleep.
    """
    global OLLAMA_URL, TUNNEL
    if "DC_OLLAMA_URL" in os.environ:
        return
    with TUNNEL_LOCK:
        if port_open(SERVICES["ollama"]):
            OLLAMA_URL = f"http://{HOST_IP}:11434"
            return
        OLLAMA_URL = f"http://127.0.0.1:{TUNNEL_PORT}"
        if TUNNEL and TUNNEL.poll() is None:
            return
        if not port_open(SERVICES["ssh"]):
            return
        TUNNEL = subprocess.Popen(
            ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
             "-L", f"{TUNNEL_PORT}:localhost:11434", SSH_ALIAS])
        time.sleep(1.5)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Datacenter panel: http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if TUNNEL:
            TUNNEL.terminate()

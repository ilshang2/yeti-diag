"""MCP server (stdio, no dependencies): every yeti_diag command as a tool for AI agents.

Start: py -3.13 -m yeti_diag.mcp_server --elm COM9     (or --sim to try without a car)

Tools are generated from the command line parser, so a new command shows up
here by itself. Each call runs `python -m yeti_diag --json ...` in its own
process (the serial port is opened and closed per call) and returns the JSON
envelope described in AI_GUIDE.md. Write commands never get --yes: they only
produce a plan; `apply` sends it, and its description tells the agent that a
person must have approved the plan's old and new values first.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys

from . import cli

PROTOCOL_VERSION = "2024-11-05"
ROOT = pathlib.Path(__file__).resolve().parent.parent

APPLY_NOTE = ("只有在人看過計畫的舊值、新值並明確同意後才能呼叫。只送一次，失敗不可重試；"
              "結束碼 5 (寫入後驗證失敗) 要立刻停止並回報。")
WRITE_NOTE = "會改車：這個工具只產生寫入計畫 (含舊值/新值)，不會寫入；要寫入請把計畫給人看，同意後用 apply。"
HIDDEN = {"commands"}  # the tool list itself is the catalog
SNIFF_MAX_SECONDS = 600  # the per-call process limit is 900 s


def tool_name(cmd):
    return cmd.replace("-", "_")


def build_tools():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {c.dest: c.help for c in sub._choices_actions}
    tools, specs = [], {}
    for cmd, p in sub.choices.items():
        if cmd in HIDDEN:
            continue
        props, required, params = {}, [], []
        for a in p._actions:
            if a.dest == "help":
                continue
            if a.nargs in ("+", "*"):
                schema = {"type": "array", "items": {"type": "string"}}
            elif a.nargs == 0:  # store_true flags
                schema = {"type": "boolean"}
            else:
                schema = {"type": "string"}
            if a.choices:
                schema["enum"] = list(a.choices)
            schema["description"] = a.help or ""
            props[a.dest] = schema
            if not a.option_strings:
                required.append(a.dest)
            params.append(a)
        if cmd == "sniff":  # through MCP nobody can press Ctrl+C: the duration is required
            required.append("seconds")
            props["seconds"]["description"] = f"錄幾秒 (必填，最多 {SNIFF_MAX_SECONDS})"
        desc = helps.get(cmd) or cmd
        if cmd == "apply":
            desc += "。" + APPLY_NOTE
        elif cmd in cli.WRITE_COMMANDS:
            desc += "。" + WRITE_NOTE
        tools.append({"name": tool_name(cmd), "description": desc,
                      "inputSchema": {"type": "object", "properties": props, "required": required}})
        specs[tool_name(cmd)] = (cmd, params)
    return tools, specs


def argv_for(cmd, params, arguments):
    argv = [cmd]
    for a in params:
        value = arguments.get(a.dest)
        if value is None:
            continue
        if a.option_strings:
            if a.nargs == 0:
                if value:
                    argv.append(a.option_strings[0])
            else:
                argv += [a.option_strings[0], str(value)]
        elif isinstance(value, list):
            argv += [str(v) for v in value]
        else:
            argv.append(str(value))
    return argv


class Server:
    def __init__(self, transport_argv, runner=None):
        self.transport = transport_argv
        self.tools, self.specs = build_tools()
        self.runner = runner or self._run

    def _run(self, argv):
        """One command in its own process; returns (exit code, JSON text)."""
        env = {"PYTHONPATH": f"{ROOT / '.deps'}{os.pathsep}{ROOT}", "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run([sys.executable, "-m", "yeti_diag", *self.transport, "--json", *argv],
                              capture_output=True, cwd=ROOT, env={**os.environ, **env}, timeout=900)
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace")

    def handle(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            return self._ok(mid, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "yeti_diag", "version": cli.TOOL_VERSION},
                                  "instructions": "Skoda Yeti 診斷工具。先呼叫 voltage；讀取工具不會改車；"
                                                  "寫入一律兩步 (先計畫，人同意後 apply)。詳見 AI_GUIDE.md。"})
        if method == "tools/list":
            return self._ok(mid, {"tools": self.tools})
        if method == "tools/call":
            name = msg["params"]["name"]
            if name not in self.specs:
                return self._err(mid, -32602, f"unknown tool {name}")
            cmd, params = self.specs[name]
            arguments = dict(msg["params"].get("arguments") or {})
            if cmd == "sniff":
                try:
                    seconds = float(arguments.get("seconds"))
                except (TypeError, ValueError):
                    return self._err(mid, -32602, "sniff 需要 seconds (錄幾秒)")
                arguments["seconds"] = min(max(seconds, 1), SNIFF_MAX_SECONDS)
            code, text = self.runner(argv_for(cmd, params, arguments))
            return self._ok(mid, {"content": [{"type": "text", "text": text}], "isError": code not in (0, 1)})
        if method == "ping":
            return self._ok(mid, {})
        if mid is None:
            return None  # notifications (initialized, cancelled) need no answer
        return self._err(mid, -32601, f"method not found: {method}")

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid, code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    def serve(self, inp, out):
        for line in inp:
            if not line.strip():
                continue
            try:
                reply = self.handle(json.loads(line))
            except Exception as e:  # noqa: BLE001 - one bad request must not kill the server
                reply = self._err(None, -32603, f"{type(e).__name__}: {e}")
            if reply is not None:
                out.write(json.dumps(reply, ensure_ascii=False) + "\n")
                out.flush()


def main(argv=None):
    p = argparse.ArgumentParser(prog="yeti_diag.mcp_server")
    p.add_argument("--elm", metavar="COM")
    p.add_argument("--sim", action="store_true")
    p.add_argument("--sim-elm", action="store_true")
    a = p.parse_args(argv)
    transport = ["--elm", a.elm] if a.elm else ["--sim-elm"] if a.sim_elm else ["--sim"]
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # one message per LF line, no CRLF on Windows
    Server(transport).serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()

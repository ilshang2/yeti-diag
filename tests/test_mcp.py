"""The MCP server an AI agent talks to."""
import io
import json

from yeti_diag import mcp_server


def call(server, method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})


def test_tools_come_from_the_command_line():
    tools = {t["name"]: t for t in call(mcp_server.Server(["--sim"]), "tools/list")["result"]["tools"]}
    assert {"autoscan", "measure", "kwp_dtc", "feature", "apply", "voltage"} <= set(tools)
    assert "commands" not in tools
    measure = tools["measure"]["inputSchema"]
    assert measure["required"] == ["module", "groups"] and measure["properties"]["groups"]["type"] == "array"
    assert "只產生寫入計畫" in tools["feature"]["description"]
    assert "明確同意" in tools["apply"]["description"]


def test_no_tool_can_skip_confirmation():
    for t in call(mcp_server.Server(["--sim"]), "tools/list")["result"]["tools"]:
        assert "yes" not in t["inputSchema"]["properties"]


def test_call_builds_the_command_line():
    seen = []
    server = mcp_server.Server(["--sim"], runner=lambda argv: (seen.append(argv), (0, '{"ok": true}'))[1])
    reply = call(server, "tools/call", {"name": "measure", "arguments": {"module": "19", "groups": [1, 2],
                                                                         "repeat": 3}})
    assert seen == [["measure", "19", "1", "2", "--repeat", "3"]]
    assert reply["result"]["isError"] is False
    call(server, "tools/call", {"name": "autoscan", "arguments": {"probe": True}})
    assert seen[-1] == ["autoscan", "--probe"]


def test_refused_write_is_an_error_result():
    server = mcp_server.Server(["--sim"], runner=lambda argv: (4, '{"ok": false}'))
    assert call(server, "tools/call", {"name": "apply", "arguments": {"plan_id": "x"}})["result"]["isError"]


def test_real_call_on_simulator():
    reply = call(mcp_server.Server(["--sim"]), "tools/call", {"name": "voltage", "arguments": {}})
    doc = json.loads(reply["result"]["content"][0]["text"])
    assert doc["ok"] and doc["data"]["voltage"] > 12


def test_stdio_session():
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "nope"}]
    out = io.StringIO()
    mcp_server.Server(["--sim"]).serve(io.StringIO("\n".join(json.dumps(m) for m in lines)), out)
    replies = [json.loads(r) for r in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"]["name"] == "yeti_diag"
    assert replies[2]["error"]["code"] == -32601


def test_sniff_needs_a_duration_over_mcp():
    # Nobody can press Ctrl+C for the agent: seconds is required and capped.
    seen = []
    server = mcp_server.Server(["--sim"], runner=lambda argv: (seen.append(argv), (0, '{"ok": true}'))[1])
    tool = next(t for t in call(server, "tools/list")["result"]["tools"] if t["name"] == "sniff")
    assert "seconds" in tool["inputSchema"]["required"]
    assert "error" in call(server, "tools/call", {"name": "sniff", "arguments": {}})
    call(server, "tools/call", {"name": "sniff", "arguments": {"seconds": 99999}})
    assert seen[-1] == ["sniff", "--seconds", str(mcp_server.SNIFF_MAX_SECONDS)]

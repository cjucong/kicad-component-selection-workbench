# -*- coding: utf-8 -*-
"""Call a KiCad IPC SDK action for an already-running KiCad editor process."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


KICAD_MCP_ROOT = Path(r"C:\Program Files\KiCad\10.0\bin\kicad-mcp")


def main() -> int:
    parser = argparse.ArgumentParser(description="Call a KiCad IPC action.")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--editor", default="schematic")
    parser.add_argument("--api", required=True)
    parser.add_argument("--cmd-type", default="cpp_sdk_action")
    parser.add_argument("--params-json", default="{}")
    args = parser.parse_args()

    sys.path.insert(0, str(KICAD_MCP_ROOT))
    from kicad_mcp.kicad_client import KiCadClient
    from kicad_mcp.utils import build_socket_url

    client = KiCadClient(build_socket_url(args.pid, args.editor), args.editor)
    params = json.loads(args.params_json)
    response = client.cpp_sdk_action(api_name=args.api, params=params, cmd_type=args.cmd_type)
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response and response.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

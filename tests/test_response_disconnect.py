"""Closing a preview is an expected client disconnect, not a second API error."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from part_selection_server import json_response


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
@pytest.mark.parametrize("stage", ["headers", "body"])
def test_cancelled_response_closes_connection_without_an_error_reply(error, stage):
    handler = SimpleNamespace(send_response=Mock(), send_header=Mock(), end_headers=Mock(),
                              wfile=SimpleNamespace(write=Mock()), close_connection=False)
    target = handler.end_headers if stage == "headers" else handler.wfile.write
    target.side_effect = error("preview closed")
    json_response(handler, {"ok": True, "detail": {}})
    assert handler.close_connection is True
    handler.send_response.assert_called_once_with(200)


def test_response_does_not_hide_unrelated_io_errors():
    handler = SimpleNamespace(send_response=Mock(), send_header=Mock(), end_headers=Mock(),
                              wfile=SimpleNamespace(write=Mock(side_effect=OSError("unrelated"))))
    with pytest.raises(OSError, match="unrelated"):
        json_response(handler, {"ok": True})

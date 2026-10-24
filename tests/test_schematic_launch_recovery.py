"""Exercise the real desktop entry decision; no native windows or production files."""
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as Obj
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/kicad_plugin"))
import desktop


def project(root):
    root.mkdir(exist_ok=True)
    (root / "board.kicad_pro").write_text("{}", encoding="utf-8")
    (root / "board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    return root


def api(root, token="live", *, metadata=True):
    doc = Obj(board_filename="board.kicad_sch")
    def no_board():
        raise RuntimeError("No PCB handler")
    return Obj(get_board=no_board, get_open_documents=lambda kind: [doc],
        get_project=lambda doc: Obj(path=str(root) if metadata else ""),
        _client=Obj(_kicad_token=token))


def dead():
    def unavailable(*args):
        raise ConnectionError("Failed to connect to KiCad: Connection refused")
    return Obj(get_board=unavailable, get_open_documents=unavailable)


def launch(root, factory, candidates, *, verified=False):
    with ExitStack() as stack:
        lifecycle = MagicMock(restart=False, install_update=False, allow_close=True)
        stack.enter_context(patch.dict(os.environ, {"KICAD_API_SOCKET": "ipc://expired",
            "KICAD_API_TOKEN": "expired-token", "KIPRJMOD": str(root)}))
        stack.enter_context(patch.dict(sys.modules, {
            "kipy": Obj(KiCad=factory),
            "kipy.proto.common.types.base_types_pb2": Obj(DocumentType=Obj(DOCTYPE_SCHEMATIC=1)),
            "webview": MagicMock(), "updater": Obj(PluginUpdater=MagicMock()),
        }))
        stack.enter_context(patch("part_selection_pcb_session.pcb_socket_candidates", return_value=candidates))
        stack.enter_context(patch.object(desktop, 'verify_launching_schematic', return_value=verified))
        stack.enter_context(patch.object(desktop, "cache_directory", return_value=root.parent))
        stack.enter_context(patch.object(desktop.logging, "basicConfig"))
        picker = stack.enter_context(patch.object(desktop, "choose_project", return_value=None))
        error = stack.enter_context(patch.object(desktop, "show_error"))
        backend = stack.enter_context(patch.object(desktop, "ensure_backend", return_value="http://127.0.0.1:1234/"))
        stack.enter_context(patch.object(desktop, "ensure_config", return_value=root / "config.json"))
        stack.enter_context(patch.object(desktop, "acquire_window", return_value=(None, True)))
        stack.enter_context(patch.object(desktop, "release_window"))
        stack.enter_context(patch("part_selection_lifecycle.DesktopLifecycle", return_value=lifecycle))
        stack.enter_context(patch.object(desktop, "relaunch_desktop", side_effect=AssertionError("Unexpected relaunch")))
        result = desktop.main([])
        return Obj(result=result, picker_calls=picker.call_count, errors=[c.args[0] for c in error.call_args_list],
            backend_roots=[c.args[0] for c in backend.call_args_list],
            socket=os.environ.get("KICAD_API_SOCKET"), token=os.environ.get("KICAD_API_TOKEN"))


def test_stale_editor_socket_recovers_current_schematic_without_file_picker(scratch_project):
    root = project(scratch_project / "target")
    calls = []
    def factory(**kwargs):
        calls.append(kwargs)
        return api(root, metadata=False) if kwargs.get("socket_path") == "ipc://current" else dead()
    result = launch(root, factory, ["ipc://expired", "ipc://current"], verified=True)
    assert result.backend_roots == [root]
    assert result.picker_calls == 0
    assert result.result == 0
    assert result.socket is None
    assert result.token is None
    assert all(c.get("kicad_token") != "expired-token" for c in calls if c.get("socket_path") == "ipc://current")


def test_expired_token_is_cleared_after_current_manager_verification(scratch_project):
    root = project(scratch_project / "target")
    def factory(**kwargs):
        token = kwargs.get("kicad_token", os.environ.get("KICAD_API_TOKEN"))
        return api(root, token="restarted") if token == "" else dead()
    result = launch(root, factory, ["ipc://expired"], verified=True)
    assert result.backend_roots == [root]
    assert result.picker_calls == 0
    assert result.token is None


def test_verified_manager_recovery_does_not_wait_for_another_ipc_attempt(scratch_project):
    root = project(scratch_project / "target")
    calls = []
    def factory(**kwargs):
        calls.append(kwargs)
        return dead() if len(calls) < 2 else api(root)
    result = launch(root, factory, ["ipc://expired"], verified=True)
    assert result.backend_roots == [root]
    assert result.picker_calls == 0
    assert len(calls) == 1


@pytest.mark.parametrize("other", [False, True])
def test_disconnected_or_other_project_does_not_open_picker_or_wrong_workbench(scratch_project, other):
    root = project(scratch_project / "target")
    other_root = project(scratch_project / "other")
    def factory(**kwargs):
        return api(other_root) if other and kwargs.get("socket_path") == "ipc://other" else dead()
    result = launch(root, factory, ["ipc://expired", "ipc://other"])
    assert result.backend_roots == []
    assert result.picker_calls == 0
    assert result.result == 1
    assert "重试" in result.errors[0]


def test_two_live_matching_sessions_are_not_chosen_by_endpoint_order(scratch_project):
    root = project(scratch_project / "target")
    def factory(**kwargs):
        endpoint = kwargs.get("socket_path")
        return api(root, token=endpoint) if endpoint in {"ipc://one", "ipc://two"} else dead()
    result = launch(root, factory, ["ipc://expired", "ipc://one", "ipc://two"])
    assert result.backend_roots == []
    assert result.picker_calls == 0
    assert result.result == 1


def test_recovery_does_not_use_environment_without_a_verified_document(scratch_project):
    root = project(scratch_project / "target")
    missing = api(root, metadata=False)
    missing.get_open_documents = lambda kind: [Obj(board_filename="missing.kicad_sch")]
    result = launch(root, lambda **kwargs: missing, ["ipc://expired"])
    assert result.backend_roots == []
    assert result.picker_calls == 0
    assert result.result == 1


def test_live_document_from_a_different_project_is_rejected(scratch_project):
    root = project(scratch_project / 'target')
    other = project(scratch_project / 'other')
    result = launch(root, lambda **kwargs: api(other), [], verified=True)
    assert result.backend_roots == [] and result.picker_calls == 0
    assert result.result == 1
    assert '不一致' in result.errors[0]

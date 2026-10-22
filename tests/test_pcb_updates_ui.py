from run_pcb_updates_smoke import run_checks


def test_pcb_update_browser_contract():
    assert '"ok":true' in run_checks()

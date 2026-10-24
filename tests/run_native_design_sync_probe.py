"""Compatibility entry for the current automatic project save/restart probe."""
import argparse
from pathlib import Path

from run_native_project_cycle_probe import finished, run


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    report = args.report_dir.resolve()
    if report.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error('Report directory must be outside source')
    report.mkdir(parents=True, exist_ok=True)
    raise SystemExit(run(report))

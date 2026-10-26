"""Audit/apply zero-copper-movement standard library unification without CAD writes."""
import argparse
import json
from pathlib import Path

from part_selection_footprint_unify import FootprintUnifier
from part_selection_server import PartSelectionApp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path, required=True, help='Report outside the KiCad project')
    parser.add_argument('--target', action='append', default=[], help='VALUE=Library:Footprint')
    args = parser.parse_args()
    app = PartSelectionApp(args.config)
    try:
        report = args.report.resolve()
        if report.is_relative_to(app.project_root):
            raise ValueError('核对报告必须存放在 KiCad 工程外')
        result = FootprintUnifier(app).run(apply=args.apply, targets=dict(t.split('=',1) for t in args.target),
            progress=lambda stage, message: print(message, flush=True))
        report.parent.mkdir(parents=True,exist_ok=True)
        report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        lines=['# 封装零偏移统一核对', '',
               '只复用现有库封装，不生成适配副本；本次没有自动同步原理图或 PCB。', '',
               f"分组：{result['groups']}；可统一：{len(result['changes'])}；跳过：{len(result['skipped'])}。", '',
               '| 位号 | Value | 原封装 | 目标封装 | 最大偏差 mm | 结果 |',
               '| :-- | :-- | :-- | :-- | --: | :-- |']
        for row in result['changes']+result['skipped']:
            error=row.get('max_error_mm')
            values=[row['ref'],row['value'],row['before'],row['after'],
                    '—' if error is None else f'{error:.6f}',row['reason']]
            lines.append('| '+' | '.join(str(value).replace('|','／').replace('\n',' ') for value in values)+' |')
        report.with_suffix('.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        print(json.dumps({'changed':len(result['changes']),'skipped':len(result['skipped']),
                          'applied':result['applied'],'report':str(report)},ensure_ascii=False))
    finally:
        app.search_jobs.close()
        app.selection_jobs.close()


if __name__ == '__main__':
    main()

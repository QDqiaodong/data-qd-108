"""独立命令:``python -m repair_survival.workbook --source FILE.xlsx --output-dir DIR``。"""
from __future__ import annotations

import argparse
import json

from .adapter import WorkbookError, collect


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='repair_survival.workbook',
        description='Collect Assets/Faults xlsx ledgers into the legacy four-column CSV plus a cell audit')
    parser.add_argument('--source', required=True, help='ordinary OOXML .xlsx workbook')
    parser.add_argument('--output-dir', required=True,
                        help='directory that must not exist yet; receives assets.csv and cells.json')
    args = parser.parse_args(argv)
    try:
        collection = collect(args.source, args.output_dir)
    except (WorkbookError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')
    summary = {
        'mode': collection['mode'],
        'source_sha256': collection['source']['sha256'],
        'date_system': collection['workbook']['date_system'],
        'output_dir': args.output_dir,
        'assets': len(collection['assets']),
        'faults': len(collection['faults']),
        'selected_failures': sum(1 for asset in collection['assets'] if asset['selected_ticket_id']),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Show a Python script's structure without executing it or showing secret literals."""
import argparse
import ast
from pathlib import Path
import sys

SAFE_LITERALS = {'__main__', 'r', 'w', 'a', 'utf-8',
                 '/root/gold-tracker-bot', '/tmp/gold_collector_output.json',
                 '/tmp/final_forecast.txt', '/tmp/final_forecast.json',
                 '/root/gold-tracker-bot/analysis_db.json',
                 '/root/gold-tracker-bot/rich_analysis_db.json',
                 'analysis_db.json', 'rich_analysis_db.json', 'daily_prices.json',
                 'forecasts', 'prices', 'evaluation', 'predicted_ranges', 'snapshot',
                 '4h', '24h', '1w'}


class HideValues(ast.NodeTransformer):
    def visit_Constant(self, node):
        value = node.value
        if isinstance(value, str):
            replacement = value if value in SAFE_LITERALS else '<VALUE_HIDDEN>'
        elif isinstance(value, bytes):
            replacement = b'<VALUE_HIDDEN>'
        elif isinstance(value, (int, float, complex)) and not isinstance(value, bool):
            replacement = 0
        else:
            replacement = value
        return ast.copy_location(ast.Constant(value=replacement), node)


def sanitized_source(source):
    tree = ast.parse(source)
    tree = HideValues().visit(tree)
    ast.fix_missing_locations(tree)
    # Unparsing drops all original comments, including commented credentials.
    return ast.unparse(tree)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('script', type=Path)
    args = parser.parse_args()
    try:
        if args.script.stat().st_size > 2*1024*1024:
            raise ValueError('Too large')
        output = sanitized_source(args.script.read_text(encoding='utf-8-sig'))
    except Exception:
        # Never include SyntaxError.text or the source in a traceback.
        print('Inspection failed; no source or credential values were displayed.', file=sys.stderr)
        return 1
    print('# Structure only: literals are hidden; original file was not executed or changed.')
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

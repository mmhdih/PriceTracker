"""Report only validated horizon results; never change trading thresholds automatically."""
import argparse
import json
from tracker_store import get_store, performance

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--full', action='store_true')
    args = parser.parse_args()
    db = get_store().export()
    stats = {h: performance(db,h) for h in ('4h','24h','1w')}
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if args.full:
        print('Legacy evaluations and lessons remain archived; no automatic parameter tuning was applied.')

if __name__ == '__main__':
    main()

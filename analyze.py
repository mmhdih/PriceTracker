"""Compatibility entrypoint: collect fresh data and run the full pipeline."""
from pipeline import main
if __name__ == '__main__':
    raise SystemExit(main(collect_fresh=True))

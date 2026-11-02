#!/usr/bin/env python3
"""Run the local H3 workbench; the existing CLI remains h3ref.py."""
import argparse

from workbench.server import create_app
from workbench.instance import single_instance
from h3ref.config import ROOT


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    with single_instance(ROOT / ".workbench"):
        create_app().run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False, debug=False)

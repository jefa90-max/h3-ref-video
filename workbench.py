#!/usr/bin/env python3
"""Run the local H3 workbench; the existing CLI remains h3ref.py."""
import argparse
import webbrowser

from werkzeug.serving import make_server

from workbench.server import create_app
from workbench.instance import single_instance
from workbench.i18n import message
from h3ref.config import ROOT


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--open", action="store_true", help=message("open_browser_help"))
    args = parser.parse_args()
    with single_instance(ROOT / ".workbench"):
        app = create_app()
        try:
            with make_server("127.0.0.1", args.port, app, threaded=True) as server:
                url = f"http://127.0.0.1:{server.port}"
                print(message("server_started", url=url), flush=True)
                if args.open:
                    webbrowser.open(url)
                server.serve_forever()
        finally:
            app.extensions["colab_runtime"].close()
            app.extensions["jobs"].close()

"""python3 -m studio [--host 0.0.0.0] [--port 4848] [--no-browser]"""
import argparse

from .server import serve

parser = argparse.ArgumentParser(prog="python3 -m studio", description="GL310 Studio")
parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 pour l'ouvrir aussi depuis la tour (réseau local)")
parser.add_argument("--port", type=int, default=4848)
parser.add_argument("--no-browser", action="store_true", help="ne pas ouvrir le navigateur")
args = parser.parse_args()
serve(args.host, args.port, not args.no_browser)

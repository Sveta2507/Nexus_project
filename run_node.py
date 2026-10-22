import argparse
import logging
from pathlib import Path

import uvicorn

from app.config import Settings
from app.main import create_app


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Nexus distributed collaboration node")
    parser.add_argument("--node-id", default="node-a")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--peer", action="append", default=[], help="Peer base URL; may be repeated")
    parser.add_argument("--secret", default="nexus-local-development-secret")
    parser.add_argument("--ssl-keyfile", type=Path)
    parser.add_argument("--ssl-certfile", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    # кожен вузол має власні налаштування, порт і sqlite-файл, а адреси peer визначають напрямки реплікації
    database = args.database or Path("data") / f"{args.node_id}.db"
    settings = Settings(
        node_id=args.node_id,
        host=args.host,
        port=args.port,
        database_path=database,
        peers=args.peer,
        federation_secret=args.secret,
    )
    # назва вузла, pid і назва потоку в журналі допомагають показати багатопотокову роботу під час захисту
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{args.node_id}] [%(process)d:%(threadName)s] %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_level="info",
        ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None,
        ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
    )


if __name__ == "__main__":
    main()

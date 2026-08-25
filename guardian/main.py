import logging
import os

import uvicorn

from .app import PUBLIC_PORT, build_app


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(build_app(), host="0.0.0.0", port=PUBLIC_PORT)


if __name__ == "__main__":
    main()

import logging
import os

import uvicorn

from .main import create_app

logging.basicConfig(level=os.environ.get("TRAVELARR_LOG", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
for noisy in ("urllib3", "plexapi"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("TRAVELARR_PORT", "8484")),
            log_level="warning", access_log=False)

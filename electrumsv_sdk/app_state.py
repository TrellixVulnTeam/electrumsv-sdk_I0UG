import datetime
import json
import logging
import os
from pathlib import Path
import sys
from typing import List

from electrumsv_node import electrumsv_node

from .argparsing import ArgParser
from .config import Config
from .constants import NameSpace
from .controller import Controller

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

logger = logging.getLogger("app_state")
filelock_logger = logging.getLogger("filelock")
filelock_logger.setLevel(logging.WARNING)
orm_logger = logging.getLogger("peewee")
orm_logger.setLevel(logging.WARNING)


class AppState:
    """Global application state"""

    def __init__(self, arguments: List[str]):
        self.argparser = ArgParser()
        self.argparser.manual_argparsing(arguments)  # allows library to inject args (vs sys.argv)
        self.cli_inputs = self.argparser.generate_cli_inputs()
        self.config = Config(cli_inputs=self.cli_inputs)
        if self.cli_inputs.namespace == NameSpace.CONFIG:
            self.config.print_json()

        self.add_file_logging_for_sdk_logs()

        self.argparser.validate_cli_args()

        self.calling_context_dir: Path = Path(os.getcwd())
        self.sdk_package_dir: Path = Path(MODULE_DIR)

        # Ensure all three plugin locations are importable
        sys.path.insert(0, f"{self.config.SDK_HOME_DIR}")  # user_components
        sys.path.insert(0, f"{self.calling_context_dir}")  # local plugins
        sys.path.insert(0, str(self.sdk_package_dir))  # builtin_components

        self.controller = Controller(self)

    def add_file_logging_for_sdk_logs(self):
        root_logger = logging.getLogger('root')
        logfile_name = "sdk.log"
        log_dir = self.config.SDK_HOME_DIR / "logs" / "sdk"
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / logfile_name)
        root_logger.addHandler(file_handler)

    def handle_first_ever_run(self) -> None:
        assert self.config.CONFIG_PATH is not None
        try:
            with open(self.config.CONFIG_PATH, "r") as f:
                data = f.read()
                if data:
                    config = json.loads(data)
                else:
                    config = {}
        except FileNotFoundError:
            with open(self.config.CONFIG_PATH, "w") as f:
                config = {"is_first_run": True}
                f.write(json.dumps(config, indent=4))

        if config.get("is_first_run") or config.get("is_first_run") is None:
            with open(self.config.CONFIG_PATH, "w") as f:
                config = {"is_first_run": False}
                f.write(json.dumps(config, indent=4))

            electrumsv_node.reset()

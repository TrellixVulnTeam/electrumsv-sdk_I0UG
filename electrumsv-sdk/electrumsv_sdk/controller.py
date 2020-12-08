import pprint
import logging
import sys
from typing import List

from electrumsv_node import electrumsv_node

from .constants import NameSpace
from .config import ImmutableConfig
from .components import ComponentStore, ComponentState, Component
from .plugin_tools import AbstractPlugin
from .utils import cast_str_int_args_to_int

logger = logging.getLogger("runners")


class Controller:
    """Five main execution pathways (corresponding to 5 cli commands)"""

    def __init__(self, app_state: "ApplicationState"):
        self.app_state = app_state
        self.component_store = ComponentStore()
        self.component_info = None

    def get_relevant_components(self, selected_component: str) -> List[dict]:
        relevant_components = []
        component_state = self.component_store.get_status()
        for component_dict in component_state.values():
            if component_dict.get('component_type') == selected_component:
                relevant_components.append(component_dict)
        return relevant_components

    def install(self, config: ImmutableConfig) -> None:
        logger.info("Installing component...")
        if config.component_id or config.selected_component:
            component_module = self.component_store.instantiate_plugin(config)
            component_module.install()

        # no args implies start all
        if not config.component_id and not config.selected_component:
            for component in self.component_store.component_map:
                new_config = ImmutableConfig(
                    selected_component=component,
                    background_flag=config.background_flag
                )
                self.install(new_config)

    def start(self, config: ImmutableConfig) -> None:
        logger.info("Starting component...")
        if config.component_id or config.selected_component:
            component_module = self.component_store.instantiate_plugin(config)
            component_module.start()
            self.status_check(component_module)

        # no args implies start all (default component ids only - e.g. node1, electrumx1 etc.)
        if not config.component_id and not config.selected_component:
            for component in self.component_store.component_map:
                new_config = ImmutableConfig(
                    selected_component=component,
                    background_flag=config.background_flag
                )
                self.start(new_config)

    def stop(self, config: ImmutableConfig) -> None:
        """stop all (no args) does not only stop default component ids but all component ids of
        each type - hence the need to hunt them all down."""
        if config.component_id:
            component_dict = \
                self.component_store.component_status_data_by_id(config.component_id)
            component_module = self.component_store.instantiate_plugin(config)
            component_module.stop()
            component_obj = Component.from_dict(component_dict)
            component_obj.component_state = ComponentState.STOPPED
            self.component_store.update_status_file(component_obj)

        elif config.selected_component:
            relevant_components = self.get_relevant_components(config.selected_component)
            if relevant_components:
                # dynamic imports of plugin
                for component_dict in relevant_components:
                    component_name = component_dict.get("component_type")
                    new_config = ImmutableConfig(
                        selected_component=component_name,
                        background_flag=config.background_flag,
                    )
                    component_module = self.component_store.instantiate_plugin(new_config)
                    component_module.stop()
                    component_obj = Component.from_dict(component_dict)
                    component_obj.component_state = ComponentState.STOPPED
                    self.component_store.update_status_file(component_obj)

        # no args implies stop all - (recursive)
        if not config.component_id and not config.selected_component:
            for component in sorted(self.component_store.component_map.keys(), reverse=True):
                new_config = ImmutableConfig(
                    selected_component=component,
                    background_flag=config.background_flag,
                )
                self.stop(new_config)
            logger.info(f"terminated: all")

    def reset(self, config: ImmutableConfig) -> None:
        if config.component_id or config.selected_component:
            component_module = self.component_store.instantiate_plugin(config)
            component_module.reset()

        # no args (no --id or <component_type>) implies reset all (node, electrumx, electrumsv)
        if not config.component_id and not config.selected_component:
            for component in self.component_store.component_map.keys():
                new_config = ImmutableConfig(
                    selected_component=component,
                    background_flag=config.background_flag,
                )
                self.reset(new_config)
            logger.info(f"reset: all")

    def status_check(self, component_module: AbstractPlugin):
        """The 'status_check()' entrypoint of the plugin must always run after the start()
        command.

        'status_check()' should return None, True or False (usually in response to polling for an
        http 200 OK response or 4XX/5XX error. However alternative litmus tests are customizable
        via the plugin.

        None type indicates that it was only run transiently (e.g. ElectrumSV's offline CLI mode)

        The status_monitor subsequently is updated with the status."""
        component_id = component_module.config.component_id
        component_name = component_module.COMPONENT_NAME

        # if --id flag or <component_type> are set and the
        if component_id != "" or component_name:
            is_running = component_module.status_check()

            if is_running is None:
                return

            if is_running is False:
                component_module.component_info.component_state = ComponentState.FAILED
                logger.error(f"{component_name} failed to start")

            elif is_running is True:
                component_module.component_info.component_state = ComponentState.RUNNING
                logger.debug(f"{component_name} online")
            self.component_store.update_status_file(component_module.component_info)
        else:
            raise Exception("neither component --id or component_name given")

    def node(self, config: ImmutableConfig) -> None:
        """Essentially bitcoin-cli interface to RPC API that works 'out of the box' with minimal
        config.

        --id flag defaults to rpchost=127.0.0.1
        --rpchost and --rpcport are manual overrides for sending requests to a node on a remote
        machine but cannot be mixed with the --id flag (it is disallowed)
        """
        DEFAULT_RPCHOST = "127.0.0.1"
        DEFAULT_RPCPORT = 18332
        node_argparser = self.app_state.argparser.parser_map[NameSpace.NODE]
        cli_options = [arg for arg in config.node_args if arg.startswith("--")]
        rpc_args = [arg for arg in config.node_args if not arg.startswith("--")]
        rpc_args = cast_str_int_args_to_int(rpc_args)

        parsed_node_options = node_argparser.parse_args(cli_options)

        cli_options_conflict = parsed_node_options.id and \
            (parsed_node_options.rpchost or parsed_node_options.rpcport)
        if cli_options_conflict:
            logger.error("cannot mix --rpchost / --rpcport flags with --id flag - must use one or "
                         "the other method of selecting a node instance")
            sys.exit(1)

        no_cli_options = not parsed_node_options.id and not \
            (parsed_node_options.rpchost or parsed_node_options.rpcport)
        if no_cli_options:
            component_id = "node1"  # default node instance to attempt
        elif parsed_node_options.id:
            component_id = parsed_node_options.id

        if no_cli_options or parsed_node_options.id:
            component_dict = self.component_store.component_status_data_by_id(component_id)
            if component_dict:
                rpchost = DEFAULT_RPCHOST
                rpcport = component_dict.get("metadata").get("rpcport")
            elif not component_dict and not parsed_node_options.id:
                logger.error(f"could not locate rpcport for node instance: {component_id}, "
                             f"using default of 18332")
                rpchost = DEFAULT_RPCHOST
                rpcport = DEFAULT_RPCPORT
            else:
                logger.error(f"could not locate metadata for node instance: {component_id}")
                exit(1)
        # --rpchost or --rpcport selected
        else:
            rpchost = parsed_node_options.rpchost or DEFAULT_RPCHOST
            rpcport = parsed_node_options.rpcport or DEFAULT_RPCPORT

        assert electrumsv_node.is_running(rpcport, rpchost), (
            "bitcoin node must be running to respond to rpc methods. "
            "try: electrumsv-sdk start --node"
        )

        if len(rpc_args) < 1:
            logger.error("RPC method not indicated. Requires at least one argument")
            exit(1)

        result = electrumsv_node.call_any(rpc_args[0], *rpc_args[1:])
        logger.info(result.json()["result"])

    def status(self):
        status = self.component_store.get_status()
        pprint.pprint(status, indent=4)

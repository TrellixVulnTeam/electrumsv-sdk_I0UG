import datetime
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
import requests


logger = logging.getLogger("utils")
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'


def checkout_branch(branch: str):
    if branch != "":
        subprocess.run(f"git checkout {branch}", shell=True, check=True)


def create_if_not_exist(path):
    path = Path(path)
    root = Path(path.parts[0])  # Root
    cur_dir = Path(root)
    for part in path.parts:
        if Path(part) != root:
            cur_dir = cur_dir.joinpath(part)
        if cur_dir.exists():
            continue
        else:
            os.mkdir(cur_dir)
            print(f"created '{cur_dir}' successfully")


def make_bat_file(filename, commandline_string_split, env_vars):
    open(filename, "w").close()
    with open(filename, "a") as f:
        f.write("@echo off\n")
        for key, val in env_vars.items():
            f.write(f"set {key}={val}\n")
        for subcmd in commandline_string_split:
            f.write(f"{subcmd}" + " ")
        f.write("\n")
        f.write("pause\n")


def make_bash_file(filename, commandline_string_split, env_vars):
    open(filename, "w").close()
    with open(filename, "a") as f:
        f.write("#!/bin/bash\n")
        f.write("set echo off\n")
        for key, val in env_vars.items():
            f.write(f"export {key}={val}\n")
        for subcmd in commandline_string_split:
            f.write(f"{subcmd}" + " ")
        f.write("\n")
        f.write('read -s -n 1 -p "Press any key to continue" . . .\n')
        f.write("exit")


def make_esv_daemon_script(esv_script, electrumsv_env_vars):
    commandline_string = (
        f"{sys.executable} {esv_script} --regtest daemon -dapp restapi "
        f"--v=debug --file-logging --restapi --server=127.0.0.1:51001:t "
        f"--portable"
    )

    if sys.platform == "win32":
        commandline_string_split = shlex.split(commandline_string, posix=0)
        make_bat_file("electrumsv.bat", commandline_string_split, electrumsv_env_vars)

    elif sys.platform in ["linux", "darwin"]:
        commandline_string_split = shlex.split(commandline_string, posix=1)
        make_bash_file("electrumsv.sh", commandline_string_split, electrumsv_env_vars)


def make_esv_gui_script(esv_script, electrumsv_env_vars):
    commandline_string = (
        f"{sys.executable} {esv_script} --regtest --v=debug --file-logging "
        f"--server=127.0.0.1:51001:t --portable"
    )

    if sys.platform == "win32":
        commandline_string_split = shlex.split(commandline_string, posix=0)
        make_bat_file("electrumsv-gui.bat", commandline_string_split, electrumsv_env_vars)

    elif sys.platform in ["linux", "darwin"]:
        commandline_string_split = shlex.split(commandline_string, posix=1)
        make_bash_file("electrumsv-gui.sh", commandline_string_split, electrumsv_env_vars)


def get_str_datetime():
    return datetime.datetime.now().strftime(TIME_FORMAT)


def topup_wallet():
    logger.debug("topping up wallet...")
    payload = json.dumps({"jsonrpc": "2.0", "method": "sendtoaddress",
        "params": ["mwv1WZTsrtKf3S9mRQABEeMaNefLbQbKpg", 25], "id": 0, })
    result = requests.post("http://rpcuser:rpcpassword@127.0.0.1:18332", data=payload)
    result.raise_for_status()
    logger.debug(result.json())
    logger.debug(f"topped up wallet with 25 coins")


def create_wallet():
    try:
        logger.debug("creating wallet...")
        wallet_name = "worker1"
        url = (
            f"http://127.0.0.1:9999/v1/regtest/dapp/wallets/"
            f"{wallet_name}.sqlite/create_new_wallet"
        )
        payload = {"password": "test"}
        response = requests.post(url, data=json.dumps(payload))
        response.raise_for_status()
        logger.debug(
            f"new wallet created in {response.json()['value']['new_wallet']}"
        )
    except Exception as e:
        logger.exception(e)


def delete_wallet(app_state):
    esv_wallet_db_directory = app_state.electrumsv_regtest_wallets_dir
    create_if_not_exist(esv_wallet_db_directory.__str__())

    try:
        time.sleep(1)
        logger.debug("deleting wallet...")
        logger.debug(
            "wallet directory before: %s",
            os.listdir(esv_wallet_db_directory.__str__()),

        )
        wallet_name = "worker1"
        file_names = [
            wallet_name + ".sqlite",
            wallet_name + ".sqlite-shm",
            wallet_name + ".sqlite-wal",
        ]
        for file_name in file_names:
            file_path = esv_wallet_db_directory.joinpath(file_name)
            if Path.exists(file_path):
                os.remove(file_path)
        logger.debug(
            "wallet directory after: %s",
            os.listdir(esv_wallet_db_directory.__str__()),
        )
    except Exception as e:
        logger.exception(e)
    else:
        return


def cast_str_int_args_to_int(node_args):
    int_indices = []
    for index, arg in enumerate(node_args):
        if arg.isdigit():
            int_indices.append(index)

    for i in int_indices:
        node_args[i] = int(node_args[i])
    return node_args

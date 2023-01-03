#!/usr/bin/python3

from argparse import ArgumentParser

import i3ipc

# Assumption: it exists 10 workspaces (otherwise, change this value)
NUM_WORKSPACES = 10

if __name__ == "__main__":
    arguments_parser = ArgumentParser()
    arguments_parser.add_argument(
        "-s",
        "--switch",
        action="store_true",
        help="switch to the first empty workspace",
    )
    arguments_parser.add_argument(
        "-m",
        "--move",
        action="store_true",
        help="move the currently focused container to the first empty workspace",
    )
    arguments = arguments_parser.parse_args()
    assert (
        arguments.switch or arguments.move
    )  # at least one of the flags must be specificated

    ipc = i3ipc.Connection()
    tree = ipc.get_tree()
    workspace = tree.find_focused().workspace()

    workspaces = tree.workspaces()

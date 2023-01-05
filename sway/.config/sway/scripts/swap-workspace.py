#!/usr/bin/python3

from argparse import ArgumentParser

import i3ipc

if __name__ == "__main__":
    arguments_parser = ArgumentParser()
    arguments_parser.add_argument(
        "-t",
        "--to",
        action="store",
        help="Move workspace to the given position.",
    )
    arguments_parser.add_argument(
        "-s",
        "--swap",
        action="store",
        help="Swap workspace positionally.",
    )
    arguments = arguments_parser.parse_args()
    assert arguments.to or arguments.swap

    ipc = i3ipc.Connection()
    tree = ipc.get_tree()
    workspace = tree.find_focused().workspace()
    workspace_number = workspace.num

    if arguments.to:
        ipc.command(f"rename workspace number {arguments.to} to {workspace_number}")
        ipc.command(f"rename workspace to {arguments.to}")
        reply = ipc.command(f"workspace {arguments.to}")

        assert reply[0].success
    elif arguments.swap:
        if arguments.swap == "left":
            target = workspace_number - 1
        elif arguments.swap == "right":
            target = workspace_number + 1
        else:
            raise Exception("Invalid swap argument.")

        ipc.command(f"rename workspace number {target} to {workspace_number}")
        ipc.command(f"rename workspace to {target}")
        ipc.command(f"rename workspace {target}")
        reply = ipc.command(f"workspace {target}")

        assert reply[0].success

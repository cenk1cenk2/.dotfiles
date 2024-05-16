#!/usr/bin/python3

from argparse import ArgumentParser

import i3ipc

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
    current_workspace = tree.find_focused().workspace()
    workspaces = tree.workspaces()  # includes current_workspace
    workspace_numbers = [workspace.num for workspace in workspaces]

    def command(command, should_assert=True):
        print(f"send: {command}")
        replies = ipc.command(command)

        if should_assert:
            for reply in replies:
                if reply.error:
                    print(f"fail: {command} -> {reply.error}")

                assert reply.success

    # Get the minor empty workspace's number (or set it as the current workspace's number if all are busy)
    target = min(set(range(1, max(workspace_numbers) + 2)) - set(workspace_numbers))

    if arguments.switch and len(current_workspace.nodes) == 0 and target > current_workspace.num:
        target = current_workspace.num

    # Use the value of first_empty_workspace_number to make the requested actions
    if arguments.move and arguments.switch:
        # Avoid wallpaper flickering when moving and switching by specifying both actions in the same Sway's command
        command(
            f"move container to workspace number {target}, workspace number {target}"
        )

    elif arguments.switch:
        command(f"workspace number {target}")

    elif arguments.move:
        command(f"move container to workspace number {target}")

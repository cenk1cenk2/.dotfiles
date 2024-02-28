#!/usr/bin/python3

from argparse import ArgumentParser

import i3ipc

if __name__ == "__main__":
    arguments_parser = ArgumentParser()
    arguments_parser.add_argument(
        "-t",
        "--to",
        type=int,
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
    workspaces = tree.workspaces()
    workspace_number = workspace.num
    workspace_numbers = [workspace.num for workspace in workspaces]
    temp_workspace = max(workspace_numbers) + 1

    def command(command, should_assert=True):
        print(f"send: {command}")
        replies = ipc.command(command)

        if should_assert:
            for reply in replies:
                if reply.error:
                    print(f"fail: {command} -> {reply.error}")

                assert reply.success

    def target_to_temp(target):
        if target in set(workspace_numbers) and target != workspace_number:
            command(f"rename workspace number {target} to {temp_workspace}")

    def temp_to_initial(target, initial):
        if target in set(workspace_numbers) and target != workspace_number:
            command(f"rename workspace number {temp_workspace} to {initial}")

    if arguments.to:
        target = arguments.to

        target_to_temp(target)
        command(
            f"rename workspace number {workspace_number} to {target}, workspace number {target}"
        )
        temp_to_initial(target, workspace_number)

    elif arguments.swap:
        if arguments.swap == "left":
            candidates = [
                candidate
                for candidate in workspace_numbers
                if candidate <= workspace_number
            ]

            target = max(
                set(
                    range(
                        min(candidates) - 1
                        if len(candidates) > 0 and min(candidates) - 1 == 1
                        else 1,
                        max(candidates) if len(candidates) > 0 else workspace_number,
                    )
                )
            )
        elif arguments.swap == "right":
            candidates = [
                candidate
                for candidate in workspace_numbers
                if candidate > workspace_number
            ]
            target = min(
                set(
                    range(
                        min(candidates)
                        if len(candidates) > 0
                        else workspace_number + 1,
                        max(candidates) + 2
                        if len(candidates) > 0
                        else workspace_number + 2,
                    )
                )
            )
        else:
            raise Exception("Invalid swap argument.")

        target_to_temp(target)
        command(
            f"rename workspace number {workspace_number} to {target}, workspace number {target}"
        )
        temp_to_initial(target, workspace_number)

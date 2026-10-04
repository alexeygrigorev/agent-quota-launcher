"""Shared aplexer run-tag construction.

A task id that already carries the ``task-`` prefix is used verbatim so the
prefix is never doubled (``task-task-...``); bare ids keep the prefix so
native session tags stay namespaced.
"""


def run_tag_for(task_id):
    tid = str(task_id)
    return tid if tid.startswith("task-") else f"task-{tid}"

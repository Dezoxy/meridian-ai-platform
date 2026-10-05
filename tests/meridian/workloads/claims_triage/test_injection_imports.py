"""The shape of the injection grader's imports (S061).

The grader reads another module's private name only by accident of history; a
rename there would then break it without a public interface to point at.
"""

import ast
import inspect

from meridian.workloads.claims_triage import injection

WORKLOAD = "meridian.workloads.claims_triage"


def test_the_injection_grader_imports_no_private_name_from_the_workload() -> None:
    tree = ast.parse(inspect.getsource(injection))

    private = [
        f"{'.' * node.level}{node.module or ''}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.level > 0 or (node.module or "").startswith(WORKLOAD))
        for alias in node.names
        if alias.name.startswith("_")
    ]

    assert private == []

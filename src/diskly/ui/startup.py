import sys
from collections.abc import Mapping


def application_arguments(
    executable: str, environment: Mapping[str, str], *, platform: str = sys.platform
) -> list[str]:
    arguments = [executable]
    if platform == "linux" and not environment.get("QT_QPA_PLATFORMTHEME"):
        arguments.extend(["-platformtheme", "generic"])
    return arguments

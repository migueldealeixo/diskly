import pytest

from diskly.ui.startup import application_arguments


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"SNAP_NAME": "code", "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"},
        {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"},
        {"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"},
        {"XDG_CURRENT_DESKTOP": "KDE"},
        {"QT_QPA_PLATFORMTHEME": ""},
    ],
)
def test_linux_startup_avoids_gtk_without_requiring_editor_variables(
    environment: dict[str, str],
) -> None:
    original = environment.copy()

    assert application_arguments("diskly", environment, platform="linux") == [
        "diskly",
        "-platformtheme",
        "generic",
    ]
    assert environment == original


@pytest.mark.parametrize("theme", ["gtk3", "qt6ct", "generic"])
def test_explicit_platform_theme_takes_precedence(theme: str) -> None:
    environment = {"QT_QPA_PLATFORMTHEME": theme}

    assert application_arguments("diskly", environment, platform="linux") == ["diskly"]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_other_platforms_keep_normal_startup(platform: str) -> None:
    assert application_arguments("diskly", {}, platform=platform) == ["diskly"]

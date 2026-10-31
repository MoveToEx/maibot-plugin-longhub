"""MaiBot Runner entry point (loaded as a package by the native plugin loader)."""

from .maibot_adapter.plugin import LongHubPlugin


def create_plugin():
    return LongHubPlugin()

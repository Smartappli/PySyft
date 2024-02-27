# stdlib
from enum import Enum


class SyftProtocol(Enum):
    """Enum class to represent the different Syft protocols."""

    HTTP = "http"
    VEILID = "veilid"

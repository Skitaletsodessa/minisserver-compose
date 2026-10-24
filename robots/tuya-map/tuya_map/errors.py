class MapError(ValueError):
    """Any problem with map data: truncated, corrupt, unsupported version, implausible size. Never anything else is raised for bad input."""

"""Auto-import extractor modules to trigger @register_extractor decorators."""

# Each import triggers the @register_extractor decorator at module level
try:
    from meho_claude.core.topology.extractors import kubernetes  # noqa: F401
except ImportError:
    pass  # kubernetes-asyncio not installed

try:
    from meho_claude.core.topology.extractors import vmware  # noqa: F401
except ImportError:
    pass  # pyvmomi not installed

try:
    from meho_claude.core.topology.extractors import proxmox  # noqa: F401
except ImportError:
    pass  # proxmoxer not installed

try:
    from meho_claude.core.topology.extractors import gcp  # noqa: F401
except ImportError:
    pass  # google-cloud-compute not installed

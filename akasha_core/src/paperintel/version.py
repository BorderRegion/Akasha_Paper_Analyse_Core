"""Version constants.

SPEC_VERSION tracks the frozen specification (``spec/paperintel_final_spec``).
PIPELINE_VERSION tracks the analysis pipeline implementation. Every canonical
analysis artifact must bind to both (spec doc 01 §21).
"""

__version__ = "1.0.0"

SPEC_VERSION = "1.0.0"
PIPELINE_VERSION = "1.0.0"

# Serialization version for request manifests (spec doc 07 §6).
REQUEST_SERIALIZATION_VERSION = "1.0.0"

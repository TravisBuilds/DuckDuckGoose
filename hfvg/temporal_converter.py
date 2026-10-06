"""
Custom Temporal data converter for datetime serialization.
"""

from datetime import datetime
from typing import Any

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    DataConverter,
    DefaultPayloadConverter,
    JSONPlainPayloadConverter,
)


class DateTimeJSONPayloadConverter(JSONPlainPayloadConverter):
    """JSON converter that handles datetime objects."""

    def to_payload(self, value: Any) -> Payload | None:
        """Convert value to payload, handling datetime."""
        if isinstance(value, datetime):
            # Convert datetime to ISO 8601 string
            value = value.isoformat()
        return super().to_payload(value)


# Create custom data converter with datetime support
temporal_data_converter = DataConverter(
    payload_converter_class=DefaultPayloadConverter(
        custom_payload_converters=[
            DateTimeJSONPayloadConverter(),
        ]
    )
)

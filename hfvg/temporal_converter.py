"""
Temporal data converter using SDK's pydantic converter.

Uses temporalio.contrib.pydantic.pydantic_data_converter which handles:
- Pydantic models
- Datetime objects
- Nested structures
"""

from temporalio.contrib.pydantic import pydantic_data_converter

temporal_data_converter = pydantic_data_converter

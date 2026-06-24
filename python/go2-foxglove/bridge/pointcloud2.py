"""sensor_msgs/PointCloud2 IDL for direct CycloneDDS (no ROS 2).

Matches the ROS 2 sensor_msgs schema so we can subscribe to the Go2's
rt/utlidar/cloud_deskewed and pass its packed `data` straight through to a
Foxglove PointCloud. Adapted from the go2-initial-test lidar service.

Do NOT add `from __future__ import annotations` — IdlStruct resolves the type
hints by name at class-definition time and PEP-563 breaks it.
"""
from dataclasses import dataclass, field

from cyclonedds.idl import IdlStruct
from cyclonedds.idl.types import int32, sequence, uint8, uint32


@dataclass
class _Time(IdlStruct, typename="builtin_interfaces::msg::dds_::Time_"):
    sec: int32 = 0
    nanosec: uint32 = 0


@dataclass
class _Header(IdlStruct, typename="std_msgs::msg::dds_::Header_"):
    stamp: _Time = field(default_factory=_Time)
    frame_id: str = ""


@dataclass
class _PointField(IdlStruct, typename="sensor_msgs::msg::dds_::PointField_"):
    name: str = ""
    offset: uint32 = 0
    datatype: uint8 = 0
    count: uint32 = 0


@dataclass
class PointCloud2_(IdlStruct, typename="sensor_msgs::msg::dds_::PointCloud2_"):
    header: _Header = field(default_factory=_Header)
    height: uint32 = 0
    width: uint32 = 0
    fields: sequence[_PointField] = field(default_factory=list)
    is_bigendian: bool = False
    point_step: uint32 = 0
    row_step: uint32 = 0
    data: sequence[uint8] = field(default_factory=list)
    is_dense: bool = False

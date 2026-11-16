"""Quirk for Aqara Climate Sensor W100 (lumi.sensor_ht.agl001).

The W100 is a remote control, not an HVAC thermostat. The quirk exposes a
virtual climate entity and mirrors its state to and from the W100.

Climate state changes and external sensor values are mirrored to the W100
only while the corresponding Thermostat control or External sensor mode is
enabled. When a mode is disabled, changes are stored locally in Home Assistant
and used when the corresponding mode is enabled again.

When updating or replacing the quirk, or moving the W100 between Zigbee
platforms, perform a factory reset before pairing it again. Press the Reset
button 10 times to restore the W100 to factory default settings.

Fan mode Off is a virtual state that hides the fan indicator on the W100
display; it is not a native W100 fan mode.

Requires Home Assistant 2026.11.0 or later.
"""

import struct
import time
from typing import Any, Final, cast

from zha.application.platforms import ZclPlatformEntity
from zha.application.platforms.climate import BaseThermostat
from zha.application.platforms.climate.const import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_OFF,
    ZCL_TEMP,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from zha.application.platforms.number.const import NumberMode
from zha.application.platforms.switch import ConfigurableAttributeSwitch
from zha.exceptions import ZHAException
from zigpy import types as t
from zigpy.typing import AddressingMode
from zigpy.zcl import (
    AttributeReadEvent,
    AttributeReportedEvent,
    AttributeUpdatedEvent,
    AttributeWrittenEvent,
    foundation,
)
from zigpy.zcl.clusters.hvac import Fan, FanMode, SystemMode, Thermostat
from zigpy.zcl.clusters.measurement import TemperatureMeasurement
from zigpy.zcl.foundation import (
    BaseAttributeDefs,
    DataTypeId,
    ZCLAttributeDef,
    ZCLHeader,
)

from zhaquirks import CustomCluster, LocalDataCluster
from zhaquirks.builder import (
    PERCENTAGE,
    EntityPlatform,
    EntityType,
    NumberDeviceClass,
    QuirkBuilder,
    UnitOfTemperature,
    UnitOfTime,
)
from zhaquirks.const import (
    COMMAND,
    DOUBLE_PRESS,
    LONG_PRESS,
    LONG_RELEASE,
    SHORT_PRESS,
    BatterySize,
)
from zhaquirks.xiaomi import (
    BATTERY_QUANTITY_ATTR,
    BATTERY_SIZE_ATTR,
    XiaomiPowerConfigurationPercent,
)
from zhaquirks.xiaomi.aqara.opple_remote import (
    COMMAND_1_DOUBLE,
    COMMAND_1_HOLD,
    COMMAND_1_RELEASE,
    COMMAND_1_SINGLE,
    COMMAND_2_DOUBLE,
    COMMAND_2_HOLD,
    COMMAND_2_RELEASE,
    COMMAND_2_SINGLE,
    COMMAND_3_DOUBLE,
    COMMAND_3_HOLD,
    COMMAND_3_RELEASE,
    COMMAND_3_SINGLE,
    MultistateInputCluster,
)

AQARA_MFG_CODE: Final = 0x115F
THERMOSTAT_ON_BIT: Final = 0x01
EXTERNAL_SENSOR_ON_BIT: Final = 0x02


class SamplingFrequency(t.enum8):
    """Temperature and humidity sampling frequency."""

    Low = 1
    Medium = 2
    High = 3
    Custom = 4


class ReportMode(t.enum8):
    """Temperature or humidity report mode."""

    Off = 0
    Threshold = 1
    Interval = 2
    Threshold_and_interval = 3


class TempHumidityStatus(t.enum8):
    """Temperature or humidity status."""

    Normal = 0
    High = 1
    Low = 2


class W100PowerConfigurationCluster(XiaomiPowerConfigurationPercent):
    """Aqara Climate Sensor W100 power configuration cluster."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize the cluster."""
        super().__init__(*args, **kwargs)

        self._CONSTANT_ATTRIBUTES = {
            BATTERY_QUANTITY_ATTR: 2,
            BATTERY_SIZE_ATTR: BatterySize.CR2450,
        }

    def handle_cluster_general_request(
        self,
        hdr: ZCLHeader,
        args: list,
        *,
        dst_addressing: AddressingMode | None = None,
    ) -> None:
        """Ignore the device's standard battery reports."""
        if hdr.command_id == foundation.GeneralCommand.Report_Attributes:
            if not hdr.frame_control.disable_default_response:
                self.send_default_rsp(hdr, foundation.Status.SUCCESS)
            return
        super().handle_cluster_general_request(hdr, args, dst_addressing=dst_addressing)


def _build_w100_frame(payload: bytes, action: int) -> bytes:
    """Build a W100 command_raw frame."""
    prefix = bytes((0xAA, 0x71, len(payload) + 3, 0x44, 0x00))
    checksum = (-sum(prefix)) & 0xFF
    return prefix + bytes((checksum, action, 0x41, len(payload))) + payload


class W100ManufacturerCluster(CustomCluster):
    """Aqara Climate Sensor W100 manufacturer cluster."""

    cluster_id = 0xFCC0
    ep_attribute = "w100_manu"

    class AttributeDefs(BaseAttributeDefs):
        """Attribute definitions."""

        low_temperature: Final = ZCLAttributeDef(
            id=0x0166,
            type=t.int16s,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        high_temperature: Final = ZCLAttributeDef(
            id=0x0167,
            type=t.int16s,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        low_humidity: Final = ZCLAttributeDef(
            id=0x016D,
            type=t.int16s,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        high_humidity: Final = ZCLAttributeDef(
            id=0x016E,
            type=t.int16s,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        temperature_status: Final = ZCLAttributeDef(
            id=0x0168,
            type=TempHumidityStatus,
            zcl_type=DataTypeId.uint8,
            access="rp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        humidity_status: Final = ZCLAttributeDef(
            id=0x016F,
            type=TempHumidityStatus,
            zcl_type=DataTypeId.uint8,
            access="rp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        temp_humidity_sampling: Final = ZCLAttributeDef(
            id=0x0170,
            type=SamplingFrequency,
            zcl_type=DataTypeId.uint8,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        temp_humidity_sampling_period: Final = ZCLAttributeDef(
            id=0x0162,
            type=t.uint32_t,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        temperature_report_mode: Final = ZCLAttributeDef(
            id=0x0165,
            type=ReportMode,
            zcl_type=DataTypeId.uint8,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        temperature_report_interval: Final = ZCLAttributeDef(
            id=0x0163,
            type=t.uint32_t,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        temperature_report_threshold: Final = ZCLAttributeDef(
            id=0x0164,
            type=t.uint16_t,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        humidity_report_mode: Final = ZCLAttributeDef(
            id=0x016C,
            type=ReportMode,
            zcl_type=DataTypeId.uint8,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        humidity_report_interval: Final = ZCLAttributeDef(
            id=0x016A,
            type=t.uint32_t,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        humidity_report_threshold: Final = ZCLAttributeDef(
            id=0x016B,
            type=t.uint16_t,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        thermostat_line_auto_hide: Final = ZCLAttributeDef(
            id=0x0173,
            type=t.Bool,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        mode_flags: Final = ZCLAttributeDef(
            id=0x0172,
            type=t.uint32_t,
            access="rp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        command_raw: Final = ZCLAttributeDef(
            id=0xFFF2,
            type=t.LVBytes,
            access="rwp",
            manufacturer_code=AQARA_MFG_CODE,
        )
        aqara_heartbeat: Final = ZCLAttributeDef(
            id=0x00F7,
            type=t.LVBytes,
            access="rp",
            manufacturer_code=AQARA_MFG_CODE,
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the W100 manufacturer cluster."""
        super().__init__(*args, **kwargs)
        for event_type in (AttributeReadEvent, AttributeReportedEvent):
            self.on_event(event_type.event_type, self._handle_attribute_event)

    def _handle_attribute_event(
        self, event: AttributeReadEvent | AttributeReportedEvent
    ) -> None:
        """Handle manufacturer attribute events."""
        if event.attribute_id == self.AttributeDefs.command_raw.id:
            self.create_catching_task(
                self._handle_command_raw_report(bytes(event.value))
            )

        if event.attribute_id == self.AttributeDefs.aqara_heartbeat.id:
            self._handle_heartbeat(event.value)

    async def _handle_command_raw_report(self, payload: bytes) -> None:
        """Process a command_raw report."""
        thermostat = cast(W100ThermostatCluster, self.endpoint.w100_thermostat)
        if thermostat.THERMOSTAT_MARKER in payload:
            if payload.endswith(thermostat.THERMOSTAT_MARKER):
                await thermostat.sync_thermostat()
            else:
                try:
                    updates = thermostat.parse_thermostat_payload(payload)
                except (ValueError, UnicodeDecodeError) as exc:
                    self.debug(
                        "Failed to parse thermostat payload %s: %s",
                        payload.hex(),
                        exc,
                    )
                else:
                    if updates:
                        thermostat.update_from_w100(updates)
                        await thermostat.sync_thermostat()

        # W100 reliably requests humidity but may skip temperature requests.
        # Send both cached external values on humidity request.
        if W100ExternalSensorCluster.EXTERNAL_HUMIDITY_MARKER in payload:
            await self.endpoint.w100_external_sensor.sync_external_sensor(
                W100ExternalSensorCluster.AttributeDefs.external_humidity.id,
                W100ExternalSensorCluster.AttributeDefs.external_temperature.id,
            )

    async def write_command_raw(
        self, value: bytes
    ) -> list[list[foundation.WriteAttributesStatusRecord]]:
        """Write a raw W100 command frame."""
        return await self.write_attributes(
            {self.AttributeDefs.command_raw: value},
            manufacturer=AQARA_MFG_CODE,
            update_cache=False,
        )

    def _handle_heartbeat(self, data: bytes) -> None:
        """Parse W100 heartbeat and update battery percentage."""

        while len(data) >= 2:
            tag = data[0]

            try:
                typed_value, data = foundation.TypeValue.deserialize(data[1:])
            except (KeyError, ValueError):
                self.debug(
                    "Failed to deserialize heartbeat tag 0x%02X, data: %r",
                    tag,
                    data,
                )
                return

            value = typed_value.value

            if tag == 0x66:
                self.endpoint.power.battery_percent_reported(value)

    async def apply_custom_configuration(self, *args: Any, **kwargs: Any) -> None:
        """Read mode flags and heartbeat during device configuration."""
        try:
            await self.read_attributes([self.AttributeDefs.mode_flags.id])
            await self.read_attributes([self.AttributeDefs.aqara_heartbeat.id])
        except Exception as exc:  # noqa: BLE001
            self.debug("Failed to read attributes: %s", exc, exc_info=True)


class W100ThermostatCluster(LocalDataCluster):
    """W100 thermostat local state cluster."""

    cluster_id = 0xFCF5
    ep_attribute = "w100_thermostat"

    class AttributeDefs(BaseAttributeDefs):
        """Attribute definitions."""

        system_mode: Final = ZCLAttributeDef(
            id=0x5001,
            type=Thermostat.SystemMode,
            manufacturer_code=None,
        )
        target_temperature: Final = ZCLAttributeDef(
            id=0x5007,
            type=t.Single,
            manufacturer_code=None,
        )
        fan_mode: Final = ZCLAttributeDef(
            id=0x5005,
            type=Fan.FanMode,
            manufacturer_code=None,
        )
        last_active_mode: Final = ZCLAttributeDef(
            id=0x5006,
            type=Thermostat.SystemMode,
            manufacturer_code=None,
        )

    THERMOSTAT_MARKER: Final[bytes] = bytes.fromhex("08000844")

    SYSTEM_MODE_TO_W100_MODE: Final[dict[int, int]] = {
        Thermostat.SystemMode.Cool: 0,
        Thermostat.SystemMode.Heat: 1,
        Thermostat.SystemMode.Auto: 2,
    }
    W100_MODE_TO_SYSTEM_MODE: Final[dict[int, int]] = {
        value: key for key, value in SYSTEM_MODE_TO_W100_MODE.items()
    }
    FAN_MODE_TO_W100_FAN_MODE: Final[dict[int, int]] = {
        Fan.FanMode.Auto: 0,
        Fan.FanMode.Low: 1,
        Fan.FanMode.Medium: 2,
        Fan.FanMode.High: 3,
    }
    W100_FAN_MODE_TO_FAN_MODE: Final[dict[int, int]] = {
        value: key for key, value in FAN_MODE_TO_W100_FAN_MODE.items()
    }

    _DEFAULT_VALUES: Final = {
        AttributeDefs.system_mode.id: Thermostat.SystemMode.Off,
        AttributeDefs.target_temperature.id: 20.0,
        AttributeDefs.fan_mode.id: Fan.FanMode.Off,
        AttributeDefs.last_active_mode.id: Thermostat.SystemMode.Heat,
    }

    _TARGET_TEMPERATURE_SYNC_MODES = {
        Thermostat.SystemMode.Heat,
        Thermostat.SystemMode.Cool,
    }

    @classmethod
    def parse_thermostat_payload(cls, payload: bytes) -> dict[str, int | float]:
        """Parse thermostat updates from a W100 payload."""
        data = payload.split(cls.THERMOSTAT_MARKER, 1)[1]
        raw_value, _ = t.LVBytes.deserialize(data)
        updates: dict[str, int | float] = {}

        for part in bytes(raw_value).decode("ascii").rstrip("\x00").split("_"):
            if len(part) < 2:
                continue
            key, value = part[0].lower(), part[1:]
            if key == "t":
                updates[key] = float(value)
            elif key in ("p", "m", "s"):
                updates[key] = int(value)

        return updates

    def update_from_w100(self, updates: dict[str, int | float]) -> None:
        """Apply W100 thermostat updates to local attributes."""
        if "m" in updates:
            m = self.W100_MODE_TO_SYSTEM_MODE[updates["m"]]
            self._update_attribute(self.AttributeDefs.system_mode.id, m)
            self._update_attribute(self.AttributeDefs.last_active_mode.id, m)

        if "t" in updates:
            self._update_attribute(
                self.AttributeDefs.target_temperature.id,
                round(float(updates["t"]), 1),
            )

        if "s" in updates:
            self._update_attribute(
                self.AttributeDefs.fan_mode.id,
                self.W100_FAN_MODE_TO_FAN_MODE[updates["s"]],
            )

        if updates.get("p") == 1:
            self._update_attribute(
                self.AttributeDefs.system_mode.id,
                Thermostat.SystemMode.Off,
            )
        elif updates.get("p") == 0:
            self._update_attribute(
                self.AttributeDefs.system_mode.id,
                self.get(self.AttributeDefs.last_active_mode.id),
            )

    async def sync_thermostat(self) -> None:
        """Sync the cached thermostat state to the W100."""
        device_ieee = self.endpoint.device.ieee.serialize()[::-1]

        mode = self.get(self.AttributeDefs.system_mode.id)
        power = 1 if mode == Thermostat.SystemMode.Off else 0

        if power:
            mode = self.get(self.AttributeDefs.last_active_mode.id)

        temperature = self.get(self.AttributeDefs.target_temperature.id)
        fan = self.get(self.AttributeDefs.fan_mode.id)

        command = f"P{power}_M{self.SYSTEM_MODE_TO_W100_MODE[mode]}_T{temperature:g}"
        if fan is not None and fan != Fan.FanMode.Off:
            command += f"_S{self.FAN_MODE_TO_W100_FAN_MODE[fan]}"

        ascii_payload = command.encode("ascii")
        payload = (
            b"\x00\x00"
            + device_ieee[:6]
            + self.THERMOSTAT_MARKER
            + bytes([len(ascii_payload)])
            + ascii_payload
            + b"\x00"
        )
        await self.endpoint.w100_manu.write_command_raw(
            _build_w100_frame(payload, 0x05)
        )

    async def write_attributes(
        self,
        attributes: dict[str | int | foundation.ZCLAttributeDef, Any],
        **kwargs: Any,
    ) -> list[list[foundation.WriteAttributesStatusRecord]]:
        """Write local thermostat attributes and sync the W100."""
        attrids = {self.find_attribute(attr).id for attr in attributes}
        result = await super().write_attributes(attributes, **kwargs)

        system_mode_id = self.AttributeDefs.system_mode.id
        system_mode = self.get(system_mode_id)

        if system_mode_id in attrids and system_mode != Thermostat.SystemMode.Off:
            self._update_attribute(
                self.AttributeDefs.last_active_mode.id,
                system_mode,
            )

        should_sync = (
            system_mode_id in attrids
            or (
                self.AttributeDefs.target_temperature.id in attrids
                and system_mode in self._TARGET_TEMPERATURE_SYNC_MODES
            )
            or (
                self.AttributeDefs.fan_mode.id in attrids
                and system_mode != Thermostat.SystemMode.Off
            )
        )
        if should_sync:
            mode_flags = self.endpoint.w100_manu.get(
                W100ManufacturerCluster.AttributeDefs.mode_flags.id
            )
            if mode_flags is not None and (mode_flags & THERMOSTAT_ON_BIT):
                self.create_catching_task(self.sync_thermostat())

        return result


class W100ExternalSensorCluster(LocalDataCluster):
    """W100 external sensor local state cluster."""

    cluster_id = 0xFCF6
    ep_attribute = "w100_external_sensor"

    class AttributeDefs(BaseAttributeDefs):
        """Attribute definitions."""

        external_temperature: Final = ZCLAttributeDef(
            id=0x0000,
            type=t.int16s,
            manufacturer_code=None,
        )
        external_humidity: Final = ZCLAttributeDef(
            id=0x0001,
            type=t.uint16_t,
            manufacturer_code=None,
        )

    EXTERNAL_TEMPERATURE_MARKER: Final[bytes] = bytes.fromhex("00010055")
    EXTERNAL_HUMIDITY_MARKER: Final[bytes] = bytes.fromhex("00020055")

    _DEFAULT_VALUES = {
        AttributeDefs.external_temperature.id: 2000,
        AttributeDefs.external_humidity.id: 5000,
    }

    async def sync_external_sensor(self, *attrids: int) -> None:
        """Sync selected cached external sensor values to the W100."""
        device_ieee = self.endpoint.device.ieee.serialize()[::-1]
        fictive_sensor_mac = device_ieee[:-1] + bytes([device_ieee[-1] ^ 0x01])

        for attrid in attrids:
            if attrid == self.AttributeDefs.external_humidity.id:
                marker = self.EXTERNAL_HUMIDITY_MARKER
            elif attrid == self.AttributeDefs.external_temperature.id:
                marker = self.EXTERNAL_TEMPERATURE_MARKER
            else:
                continue

            payload = (
                fictive_sensor_mac + marker + struct.pack(">f", float(self.get(attrid)))
            )
            await self.endpoint.w100_manu.write_command_raw(
                _build_w100_frame(payload, 0x05)
            )

    async def write_attributes(
        self,
        attributes: dict[str | int | foundation.ZCLAttributeDef, Any],
        **kwargs: Any,
    ) -> list[list[foundation.WriteAttributesStatusRecord]]:
        """Write local external sensor attributes and sync written values."""
        result = await super().write_attributes(attributes, **kwargs)

        mode_flags = self.endpoint.w100_manu.get(
            W100ManufacturerCluster.AttributeDefs.mode_flags.id
        )

        if mode_flags is None or not (mode_flags & EXTERNAL_SENSOR_ON_BIT):
            return result

        for attr in attributes:
            attrid = self.find_attribute(attr).id

            if attrid in (
                self.AttributeDefs.external_temperature.id,
                self.AttributeDefs.external_humidity.id,
            ):
                self.create_catching_task(self.sync_external_sensor(attrid))

        return result


class W100ThermostatControlSwitch(ConfigurableAttributeSwitch):
    """W100 thermostat control switch."""

    _attribute_name = W100ManufacturerCluster.AttributeDefs.mode_flags.name

    @property
    def is_on(self) -> bool:
        """Return if thermostat control is enabled on the device."""
        mode_flags = self._cluster.get(self._attribute_name)
        return bool(mode_flags and mode_flags & THERMOSTAT_ON_BIT)

    async def async_turn_on_off(self, state: bool) -> None:
        """Turn thermostat control on or off."""
        ieee = self._cluster.endpoint.device.ieee.serialize()[::-1]
        payload = bytes.fromhex("6891000018") + ieee
        if state:
            payload += (
                b"\x00\x00"
                + ieee[:6]
                + bytes.fromhex("08000844150a0109e7a9bae8b083e58a9f000000000001012a40")
            )
        else:
            payload += bytes(12)

        action = 0x02 if state else 0x04
        result = await self._cluster.write_command_raw(
            _build_w100_frame(payload, action)
        )
        status = result[0][0].status

        if status is not foundation.Status.SUCCESS:
            raise ZHAException(
                f"Failed to turn thermostat control {'on' if state else 'off'}: {status}"
            )


class W100ExternalSensorSwitch(ConfigurableAttributeSwitch):
    """W100 external sensor switch."""

    _attribute_name = W100ManufacturerCluster.AttributeDefs.mode_flags.name

    @property
    def is_on(self) -> bool:
        """Return if external sensor mode is enabled on the device."""
        mode_flags = self._cluster.get(self._attribute_name)
        return bool(mode_flags and mode_flags & EXTERNAL_SENSOR_ON_BIT)

    async def async_turn_on_off(self, state: bool) -> None:
        """Turn external sensor mode on or off."""
        ieee = self._cluster.endpoint.device.ieee.serialize()[::-1]
        sensor_mac = ieee[:-1] + bytes([ieee[-1] ^ 0x01])
        timestamp = int(time.time()).to_bytes(4, "big")

        humidity = (
            b"\x15",
            bytes.fromhex("00020055150a0100000106e6b9bfe5baa6000000000001020865"),
        )
        temperature = (
            b"\x14",
            bytes.fromhex("00010055150a0100000106e6b8a9e5baa6000000000001020763"),
        )

        action = 0x02 if state else 0x04
        for channel, suffix in (humidity, temperature):
            payload = timestamp + channel + ieee
            if state:
                payload += sensor_mac + suffix
            else:
                payload += bytes(12)

            result = await self._cluster.write_command_raw(
                _build_w100_frame(payload, action)
            )
            status = result[0][0].status

            if status is not foundation.Status.SUCCESS:
                raise ZHAException(
                    f"Failed to turn {'on' if state else 'off'} external sensor: {status}"
                )


class W100ClimateEntity(BaseThermostat, ZclPlatformEntity):
    """W100 climate entity backed by the W100 thermostat cluster."""

    DEFAULT_MIN_TEMP: Final = 4.5
    DEFAULT_MAX_TEMP: Final = 37

    HVAC_MODES: Final[list[HVACMode]] = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.AUTO,
    ]

    FAN_MODES: Final[list[str]] = [
        FAN_OFF,
        FAN_AUTO,
        FAN_LOW,
        FAN_MEDIUM,
        FAN_HIGH,
    ]

    SYSTEM_MODE_TO_HVAC: Final = {
        Thermostat.SystemMode.Off: HVACMode.OFF,
        Thermostat.SystemMode.Heat: HVACMode.HEAT,
        Thermostat.SystemMode.Cool: HVACMode.COOL,
        Thermostat.SystemMode.Auto: HVACMode.AUTO,
    }

    HVAC_TO_SYSTEM_MODE: Final = {
        value: key for key, value in SYSTEM_MODE_TO_HVAC.items()
    }

    FAN_MODE_TO_ZCL: Final = {
        FAN_OFF: Fan.FanMode.Off,
        FAN_AUTO: Fan.FanMode.Auto,
        FAN_LOW: Fan.FanMode.Low,
        FAN_MEDIUM: Fan.FanMode.Medium,
        FAN_HIGH: Fan.FanMode.High,
    }

    ZCL_TO_FAN_MODE: Final = {value: key for key, value in FAN_MODE_TO_ZCL.items()}

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()

        for cluster in (self._cluster, self._temperature_cluster):
            for event_type in (
                AttributeReadEvent,
                AttributeReportedEvent,
                AttributeUpdatedEvent,
                AttributeWrittenEvent,
            ):
                self._on_remove_callbacks.append(
                    cluster.on_event(
                        event_type.event_type,
                        self.handle_attribute_updated,
                    )
                )

    def handle_attribute_updated(
        self,
        event: AttributeReadEvent
        | AttributeReportedEvent
        | AttributeUpdatedEvent
        | AttributeWrittenEvent,
    ) -> None:
        """Handle W100 thermostat or temperature update."""
        self.maybe_emit_state_changed_event()

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return True

    @property
    def _temperature_cluster(self) -> TemperatureMeasurement:
        """Return temperature measurement cluster."""
        return self.endpoint.zigpy_endpoint.in_clusters[
            TemperatureMeasurement.cluster_id
        ]

    @property
    def _local_temperature(self) -> int | None:
        """Return measured temperature in centi-degrees."""
        return self._temperature_cluster.get(
            TemperatureMeasurement.AttributeDefs.measured_value.name
        )

    @property
    def _system_mode(self) -> SystemMode | None:
        """Return cached W100 system mode."""
        return self._cluster.get(W100ThermostatCluster.AttributeDefs.system_mode.name)

    @property
    def _target_temperature(self) -> float | None:
        """Return cached target temperature in degrees Celsius."""
        return self._cluster.get(
            W100ThermostatCluster.AttributeDefs.target_temperature.name
        )

    @property
    def _fan_mode(self) -> FanMode | None:
        """Return cached fan mode."""
        return self._cluster.get(W100ThermostatCluster.AttributeDefs.fan_mode.name)

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        temperature = self._local_temperature

        if temperature is None:
            return None

        return temperature / ZCL_TEMP

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def target_temperature_high(self) -> float | None:
        """Return the upper bound temperature we try to reach."""
        return None

    @property
    def target_temperature_low(self) -> float | None:
        """Return the lower bound temperature we try to reach."""
        return None

    @property
    def outdoor_temperature(self) -> float | None:
        """Return the outdoor temperature."""
        return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return HVAC operation mode."""
        mode = self._system_mode
        return self.SYSTEM_MODE_TO_HVAC.get(mode) if mode is not None else None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available HVAC operation modes."""
        return self.HVAC_MODES

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        return None

    @property
    def fan_mode(self) -> str | None:
        """Return current FAN mode."""
        mode = self._fan_mode
        return self.ZCL_TO_FAN_MODE.get(mode, FAN_OFF) if mode is not None else FAN_OFF

    @property
    def fan_modes(self) -> list[str]:
        """Return supported FAN modes."""
        return self.FAN_MODES

    @property
    def preset_mode(self) -> str | None:
        """Return current preset mode."""
        return None

    @property
    def preset_modes(self) -> list[str] | None:
        """Return supported preset modes."""
        return None

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return self.DEFAULT_MAX_TEMP

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return self.DEFAULT_MIN_TEMP

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Return the list of supported features."""
        return (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target operation mode."""
        if hvac_mode not in self.hvac_modes:
            return

        await self._cluster.write_attributes(
            {
                W100ThermostatCluster.AttributeDefs.system_mode.name: self.HVAC_TO_SYSTEM_MODE[
                    hvac_mode
                ],
            }
        )
        self.maybe_emit_state_changed_event()

    async def async_set_temperature(
        self,
        target_temp_low: float | None = None,
        target_temp_high: float | None = None,
        temperature: float | None = None,
        hvac_mode: HVACMode | None = None,
    ) -> None:
        """Set new target operation mode and/or temperature in one write."""
        attributes: dict[str, Any] = {}

        if hvac_mode is not None:
            if hvac_mode not in self.hvac_modes:
                return

            attributes[W100ThermostatCluster.AttributeDefs.system_mode.name] = (
                self.HVAC_TO_SYSTEM_MODE[hvac_mode]
            )

        if temperature is not None:
            attributes[W100ThermostatCluster.AttributeDefs.target_temperature.name] = (
                temperature
            )

        if not attributes:
            self._cluster.debug(
                "incorrect temperature setting for '%s' mode", self.hvac_mode
            )
            return

        await self._cluster.write_attributes(attributes)
        self.maybe_emit_state_changed_event()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan mode."""
        zcl_fan_mode = self.FAN_MODE_TO_ZCL.get(fan_mode)

        if zcl_fan_mode is None:
            return

        await self._cluster.write_attributes(
            {
                W100ThermostatCluster.AttributeDefs.fan_mode.name: zcl_fan_mode,
            }
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        self._cluster.debug("Preset mode '%s' is not supported", preset_mode)


(
    QuirkBuilder("Aqara", "lumi.sensor_ht.agl001")
    .friendly_name(manufacturer="Aqara", model="Climate Sensor W100")
    .replaces(W100PowerConfigurationCluster)
    .replaces(W100ManufacturerCluster)
    .replaces(MultistateInputCluster, endpoint_id=1)
    .replaces(MultistateInputCluster, endpoint_id=2)
    .replaces(MultistateInputCluster, endpoint_id=3)
    .device_automation_triggers(
        {
            (SHORT_PRESS, "plus"): {COMMAND: COMMAND_1_SINGLE},
            (DOUBLE_PRESS, "plus"): {COMMAND: COMMAND_1_DOUBLE},
            (LONG_PRESS, "plus"): {COMMAND: COMMAND_1_HOLD},
            (LONG_RELEASE, "plus"): {COMMAND: COMMAND_1_RELEASE},
            (SHORT_PRESS, "center"): {COMMAND: COMMAND_2_SINGLE},
            (DOUBLE_PRESS, "center"): {COMMAND: COMMAND_2_DOUBLE},
            (LONG_PRESS, "center"): {COMMAND: COMMAND_2_HOLD},
            (LONG_RELEASE, "center"): {COMMAND: COMMAND_2_RELEASE},
            (SHORT_PRESS, "minus"): {COMMAND: COMMAND_3_SINGLE},
            (DOUBLE_PRESS, "minus"): {COMMAND: COMMAND_3_DOUBLE},
            (LONG_PRESS, "minus"): {COMMAND: COMMAND_3_HOLD},
            (LONG_RELEASE, "minus"): {COMMAND: COMMAND_3_RELEASE},
        }
    )
    .adds(W100ThermostatCluster)
    .adds(W100ExternalSensorCluster)
    .adds_entity(
        W100ClimateEntity,
        endpoint_id=1,
        cluster_id=W100ThermostatCluster.cluster_id,
        entity_type=EntityType.STANDARD,
        unique_id_suffix="thermostat",
        force_create=True,
        translation_key="thermostat",
        fallback_name="Thermostat",
    )
    .adds_entity(
        W100ThermostatControlSwitch,
        endpoint_id=1,
        cluster_id=W100ManufacturerCluster.cluster_id,
        entity_type=EntityType.CONFIG,
        unique_id_suffix="thermostat_control",
        force_create=True,
        translation_key="thermostat_control",
        fallback_name="Thermostat control",
    )
    .adds_entity(
        W100ExternalSensorSwitch,
        endpoint_id=1,
        cluster_id=W100ManufacturerCluster.cluster_id,
        entity_type=EntityType.CONFIG,
        unique_id_suffix="external_sensor",
        force_create=True,
        translation_key="external_sensor",
        fallback_name="External sensor",
    )
    .switch(
        attribute_name="thermostat_line_auto_hide",
        cluster_id=W100ManufacturerCluster.cluster_id,
        on_value=0,
        off_value=1,
        translation_key="thermostat_line_auto_hide",
        fallback_name="Thermostat line auto-hide",
    )
    .number(
        attribute_name="external_temperature",
        cluster_id=W100ExternalSensorCluster.cluster_id,
        device_class=NumberDeviceClass.TEMPERATURE,
        entity_type=EntityType.STANDARD,
        min_value=-99.9,
        max_value=100.0,
        step=0.1,
        multiplier=0.01,
        unit=UnitOfTemperature.CELSIUS,
        mode=NumberMode.BOX,
        translation_key="external_temperature",
        fallback_name="External temperature",
    )
    .number(
        attribute_name="external_humidity",
        cluster_id=W100ExternalSensorCluster.cluster_id,
        device_class=NumberDeviceClass.HUMIDITY,
        entity_type=EntityType.STANDARD,
        min_value=0.0,
        max_value=99.0,
        step=1.0,
        multiplier=0.01,
        unit=PERCENTAGE,
        mode=NumberMode.BOX,
        translation_key="external_humidity",
        fallback_name="External humidity",
    )
    .number(
        attribute_name="high_temperature",
        cluster_id=W100ManufacturerCluster.cluster_id,
        device_class=NumberDeviceClass.TEMPERATURE,
        min_value=23.0,
        max_value=60.0,
        step=0.5,
        multiplier=0.01,
        unit=UnitOfTemperature.CELSIUS,
        translation_key="high_temperature",
        fallback_name="High temperature",
    )
    .number(
        attribute_name="low_temperature",
        cluster_id=W100ManufacturerCluster.cluster_id,
        device_class=NumberDeviceClass.TEMPERATURE,
        min_value=-20.0,
        max_value=22.0,
        step=0.5,
        multiplier=0.01,
        unit=UnitOfTemperature.CELSIUS,
        translation_key="low_temperature",
        fallback_name="Low temperature",
    )
    .number(
        attribute_name="high_humidity",
        cluster_id=W100ManufacturerCluster.cluster_id,
        device_class=NumberDeviceClass.HUMIDITY,
        min_value=65.0,
        max_value=99.99,
        step=1.0,
        multiplier=0.01,
        unit=PERCENTAGE,
        translation_key="high_humidity",
        fallback_name="High humidity",
    )
    .number(
        attribute_name="low_humidity",
        cluster_id=W100ManufacturerCluster.cluster_id,
        device_class=NumberDeviceClass.HUMIDITY,
        min_value=0.0,
        max_value=30.0,
        step=1.0,
        multiplier=0.01,
        unit=PERCENTAGE,
        translation_key="low_humidity",
        fallback_name="Low humidity",
    )
    .enum(
        attribute_name="temperature_status",
        enum_class=TempHumidityStatus,
        cluster_id=W100ManufacturerCluster.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="temperature_status",
        fallback_name="Temperature status",
    )
    .enum(
        attribute_name="humidity_status",
        enum_class=TempHumidityStatus,
        cluster_id=W100ManufacturerCluster.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.DIAGNOSTIC,
        translation_key="humidity_status",
        fallback_name="Humidity status",
    )
    .enum(
        attribute_name="temp_humidity_sampling",
        enum_class=SamplingFrequency,
        cluster_id=W100ManufacturerCluster.cluster_id,
        translation_key="temp_humidity_sampling",
        fallback_name="Temperature and humidity sampling",
    )
    .number(
        attribute_name="temp_humidity_sampling_period",
        cluster_id=W100ManufacturerCluster.cluster_id,
        device_class=NumberDeviceClass.DURATION,
        min_value=0.5,
        max_value=600.0,
        step=0.5,
        multiplier=0.001,
        unit=UnitOfTime.SECONDS,
        translation_key="temp_humidity_sampling_period",
        fallback_name="Temperature and humidity sampling period",
    )
    .enum(
        attribute_name="temperature_report_mode",
        enum_class=ReportMode,
        cluster_id=W100ManufacturerCluster.cluster_id,
        translation_key="temperature_report_mode",
        fallback_name="Temperature report mode",
    )
    .number(
        attribute_name="temperature_report_interval",
        cluster_id=W100ManufacturerCluster.cluster_id,
        device_class=NumberDeviceClass.DURATION,
        min_value=1.0,
        max_value=600.0,
        step=1.0,
        multiplier=0.001,
        unit=UnitOfTime.SECONDS,
        translation_key="temperature_report_interval",
        fallback_name="Temperature report interval",
    )
    .number(
        attribute_name="temperature_report_threshold",
        cluster_id=W100ManufacturerCluster.cluster_id,
        device_class=NumberDeviceClass.TEMPERATURE,
        min_value=0.2,
        max_value=3.0,
        step=0.1,
        multiplier=0.01,
        unit=UnitOfTemperature.CELSIUS,
        translation_key="temperature_report_threshold",
        fallback_name="Temperature report threshold",
    )
    .enum(
        attribute_name="humidity_report_mode",
        enum_class=ReportMode,
        cluster_id=W100ManufacturerCluster.cluster_id,
        translation_key="humidity_report_mode",
        fallback_name="Humidity report mode",
    )
    .number(
        attribute_name="humidity_report_interval",
        cluster_id=W100ManufacturerCluster.cluster_id,
        device_class=NumberDeviceClass.DURATION,
        min_value=1.0,
        max_value=600.0,
        step=1.0,
        multiplier=0.001,
        unit=UnitOfTime.SECONDS,
        translation_key="humidity_report_interval",
        fallback_name="Humidity report interval",
    )
    .number(
        attribute_name="humidity_report_threshold",
        cluster_id=W100ManufacturerCluster.cluster_id,
        device_class=NumberDeviceClass.HUMIDITY,
        min_value=2.0,
        max_value=10.0,
        step=0.5,
        multiplier=0.01,
        unit=PERCENTAGE,
        translation_key="humidity_report_threshold",
        fallback_name="Humidity report threshold",
    )
    .add_to_registry()
)

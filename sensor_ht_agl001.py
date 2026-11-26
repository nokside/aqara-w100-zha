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

Requires Home Assistant 2026.12.0b0 or later.
"""

import struct
import time
from typing import Any, Final

from zha.application.platforms import ZclPlatformEntity
from zha.application.platforms.climate import BaseThermostat
from zha.application.platforms.climate.const import (
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    ZCL_TEMP,
    ClimateEntityFeature,
    HVACMode,
)
from zha.application.platforms.number.const import NumberMode
from zha.application.platforms.switch import ConfigurableAttributeSwitch
from zigpy import types as t
from zigpy.typing import AddressingMode
from zigpy.zcl import (
    AttributeReadEvent,
    AttributeReportedEvent,
    AttributeUpdatedEvent,
    AttributeWrittenEvent,
    ClusterType,
    foundation,
)
from zigpy.zcl.clusters.general import MultistateInput
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
    ButtonEvent,
    ButtonEventType,
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
        self._frame_counter = 0
        for event_type in (AttributeReadEvent, AttributeReportedEvent):
            self.on_event(event_type.event_type, self._handle_attribute_event)

    def _handle_attribute_event(
        self, event: AttributeReadEvent | AttributeReportedEvent
    ) -> None:
        """Handle manufacturer attribute events."""
        if event.attribute_id == self.AttributeDefs.command_raw.id:
            self._handle_command_raw(bytes(event.value))

        if event.attribute_id == self.AttributeDefs.aqara_heartbeat.id:
            self._handle_heartbeat(
                event.value,
                is_reported_event=isinstance(event, AttributeReportedEvent),
            )

    def _handle_command_raw(self, payload: bytes) -> None:
        """Handle incoming command_raw attribute data."""
        if W100ThermostatCluster.THERMOSTAT_MARKER in payload:
            self.endpoint.w100_thermostat.handle_pmts(payload)

        # W100 reliably requests humidity but may skip temperature requests.
        # Send both cached external values on humidity request.
        if W100ExternalSensorCluster.EXTERNAL_HUMIDITY_MARKER in payload:
            self.endpoint.w100_external_sensor.send_state(
                W100ExternalSensorCluster.AttributeDefs.external_humidity.id,
                W100ExternalSensorCluster.AttributeDefs.external_temperature.id,
            )

    async def write_command_raw(self, payload: bytes, action: int) -> None:
        """Build and write a W100 command frame."""
        header = bytes((0xAA, 0x71, len(payload) + 3, 0x44, self._frame_counter))
        checksum = (-sum(header)) & 0xFF
        frame = header + bytes((checksum, action, 0x41, len(payload))) + payload

        self._frame_counter = (self._frame_counter + 1) & 0xFF

        await self.write_attributes({self.AttributeDefs.command_raw: frame})

    def _handle_heartbeat(self, data: bytes, *, is_reported_event: bool) -> None:
        """Parse W100 heartbeat and update attributes."""

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
            elif is_reported_event:
                if tag == 0x64:
                    self.endpoint.temperature.update_attribute(
                        self.endpoint.temperature.AttributeDefs.measured_value.id,
                        value,
                    )
                elif tag == 0x65:
                    self.endpoint.humidity.update_attribute(
                        self.endpoint.humidity.AttributeDefs.measured_value.id,
                        value,
                    )

    async def apply_custom_configuration(self, *args: Any, **kwargs: Any) -> None:
        """Read device attributes during configuration."""
        try:
            await self.read_attributes(
                [
                    attr.id
                    for attr in self.AttributeDefs
                    if attr.id != self.AttributeDefs.command_raw.id
                ]
            )
            for cluster in (self.endpoint.temperature, self.endpoint.humidity):
                await cluster.read_attributes([cluster.AttributeDefs.measured_value.id])
        except Exception as exc:  # noqa: BLE001
            self.debug("Failed to read attributes: %s", exc, exc_info=True)


class W100ThermostatControlSwitch(ConfigurableAttributeSwitch):
    """W100 thermostat control switch."""

    _attribute_name = W100ManufacturerCluster.AttributeDefs.mode_flags.name

    @property
    def is_on(self) -> bool:
        """Return if thermostat control is enabled on the device."""
        return bool(self._cluster.get(self._attribute_name) & THERMOSTAT_ON_BIT)

    async def async_turn_on_off(self, state: bool) -> None:
        """Turn thermostat control on or off."""
        ieee = self._cluster.endpoint.device.ieee.serialize()[::-1]
        hub_mac = ieee[:6]
        payload = bytes.fromhex("6891000018") + ieee
        if state:
            payload += (
                b"\x00\x00"
                + hub_mac
                + bytes.fromhex("08000844150a0109e7a9bae8b083e58a9f000000000001012a40")
            )
        else:
            payload += bytes(12)

        action = 0x02 if state else 0x04
        await self._cluster.write_command_raw(payload, action)


class W100ExternalSensorSwitch(ConfigurableAttributeSwitch):
    """W100 external sensor switch."""

    _attribute_name = W100ManufacturerCluster.AttributeDefs.mode_flags.name

    @property
    def is_on(self) -> bool:
        """Return if external sensor mode is enabled on the device."""
        return bool(self._cluster.get(self._attribute_name) & EXTERNAL_SENSOR_ON_BIT)

    async def async_turn_on_off(self, state: bool) -> None:
        """Turn external sensor mode on or off."""
        ieee = self._cluster.endpoint.device.ieee.serialize()[::-1]
        virtual_sensor_ieee = ieee[:-1] + bytes([ieee[-1] ^ 0x01])
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
                payload += virtual_sensor_ieee + suffix
            else:
                payload += bytes(12)

            await self._cluster.write_command_raw(payload, action)


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

    _DEFAULT_VALUES: Final = {
        AttributeDefs.external_temperature.id: 2000,
        AttributeDefs.external_humidity.id: 5000,
    }

    def send_state(self, *attrids: int) -> None:
        """Send cached external sensor values."""
        device_ieee = self.endpoint.device.ieee.serialize()[::-1]
        virtual_sensor_ieee = device_ieee[:-1] + bytes([device_ieee[-1] ^ 0x01])

        for attrid in attrids:
            marker = (
                self.EXTERNAL_HUMIDITY_MARKER
                if attrid == self.AttributeDefs.external_humidity.id
                else self.EXTERNAL_TEMPERATURE_MARKER
            )

            payload = (
                virtual_sensor_ieee
                + marker
                + struct.pack(">f", float(self.get(attrid)))
            )

            self.create_catching_task(
                self.endpoint.w100_manu.write_command_raw(payload, 0x05)
            )

    async def write_attributes(
        self,
        attributes: dict[str | int | ZCLAttributeDef, Any],
        **kwargs,
    ) -> list[list[foundation.WriteAttributesStatusRecord]]:
        """Write local external sensor attributes and send their values."""
        result = await super().write_attributes(attributes, **kwargs)

        mode_flags = self.endpoint.w100_manu.get(
            W100ManufacturerCluster.AttributeDefs.mode_flags.id
        )

        if mode_flags is not None and mode_flags & EXTERNAL_SENSOR_ON_BIT:
            self.send_state(
                *(self.find_attribute(attr).id for attr in attributes)
            )

        return result


class W100ThermostatCluster(LocalDataCluster):
    """W100 thermostat local state cluster."""

    cluster_id = 0xFCF5
    ep_attribute = "w100_thermostat"

    class AttributeDefs(BaseAttributeDefs):
        """Attribute definitions."""

        system_mode: Final = ZCLAttributeDef(
            id=0x0007,
            type=t.uint8_t,
            manufacturer_code=None,
        )
        last_mode: Final = ZCLAttributeDef(
            id=0x0002,
            type=t.uint8_t,
            manufacturer_code=None,
        )
        target_temperature: Final = ZCLAttributeDef(
            id=0x0003,
            type=t.Single,
            manufacturer_code=None,
        )
        fan_mode: Final = ZCLAttributeDef(
            id=0x0008,
            type=t.uint8_t,
            manufacturer_code=None,
        )
        fan_hide: Final = ZCLAttributeDef(
            id=0x0005,
            type=t.Bool,
            manufacturer_code=None,
        )

    THERMOSTAT_MARKER: Final[bytes] = bytes.fromhex("08000844")

    M_TO_SYSTEM: Final = {0: 3, 1: 4, 2: 1}
    SYSTEM_TO_M: Final = {value: key for key, value in M_TO_SYSTEM.items()}

    _DEFAULT_VALUES: Final = {
        AttributeDefs.system_mode.id: 0,
        AttributeDefs.last_mode.id: 1,
        AttributeDefs.target_temperature.id: 20,
        AttributeDefs.fan_mode.id: 0,
        AttributeDefs.fan_hide.id: True,
    }

    def handle_pmts(self, payload: bytes) -> None:
        """Handle incoming PMTS and sync thermostat state."""
        data = payload.partition(self.THERMOSTAT_MARKER)[2]
        attributes = {}

        if data:
            try:
                raw, _ = t.LVBytes.deserialize(data)
                attributes = {
                    part[0]: int(part[1:])
                    for part in raw.decode("ascii").split("_")
                    if part[:1] in ("P", "M", "T", "S")
                }
            except ValueError:
                self.debug("Failed to parse PMTS payload: %s", payload.hex())

        if "P" in attributes or "M" in attributes:
            system_mode = self.get(self.AttributeDefs.system_mode.id)
            p = attributes.get("P", int(system_mode == 0))
            if "M" in attributes:
                self._update_attribute(self.AttributeDefs.last_mode.id, attributes["M"])

            m = self.get(self.AttributeDefs.last_mode.id)
            self._update_attribute(
                self.AttributeDefs.system_mode.id, 0 if p == 1 else self.M_TO_SYSTEM[m]
            )

        if "T" in attributes:
            self._update_attribute(
                self.AttributeDefs.target_temperature.id, attributes["T"]
            )

        if "S" in attributes:
            self._update_attribute(
                self.AttributeDefs.fan_mode.id,
                attributes["S"],
            )

        self.send_state()

    def send_state(self) -> None:
        """Sync the cached PMTS state to the W100."""
        ieee = self.endpoint.device.ieee.serialize()[::-1]
        hub_mac = ieee[:6]

        system_mode = self.get(self.AttributeDefs.system_mode.id)
        p = int(system_mode == 0)
        m = self.get(self.AttributeDefs.last_mode.id)
        temperature = self.get(self.AttributeDefs.target_temperature.id)
        s = self.get(self.AttributeDefs.fan_mode.id)

        text = f"P{p}_M{m}_T{int(temperature)}"

        if not self.get(
            self.AttributeDefs.fan_hide.id
        ):
            text += f"_S{s}"

        payload = (
            b"\x00\x00"
            + hub_mac
            + self.THERMOSTAT_MARKER
            + t.LVBytes(text.encode("ascii")).serialize()
            + b"\x00"
        )

        self.create_catching_task(
            self.endpoint.w100_manu.write_command_raw(payload, 0x05)
        )

    async def write_attributes(
        self,
        attributes: dict[str | int | ZCLAttributeDef, Any],
        **kwargs,
    ) -> list[list[foundation.WriteAttributesStatusRecord]]:
        """Write local attributes and sync thermostat state."""
        result = await super().write_attributes(attributes, **kwargs)

        mode_flags = self.endpoint.w100_manu.get(
            W100ManufacturerCluster.AttributeDefs.mode_flags.id
        )

        if mode_flags is not None and mode_flags & THERMOSTAT_ON_BIT:
            self.send_state()

        return result


class W100ClimateEntity(BaseThermostat, ZclPlatformEntity):
    """W100 climate entity backed by the W100 thermostat cluster."""

    DEFAULT_MIN_TEMP: Final = 4.5
    DEFAULT_MAX_TEMP: Final = 37

    SYSTEM_TO_HVAC: Final = {
        0: HVACMode.OFF,
        1: HVACMode.AUTO,
        3: HVACMode.COOL,
        4: HVACMode.HEAT,
    }

    HVAC_TO_SYSTEM: Final = {
        value: key for key, value in SYSTEM_TO_HVAC.items()
    }

    HVAC_MODES: Final[list[HVACMode]] = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.AUTO,
    ]

    MODE_TO_FAN: Final = {
        0: FAN_AUTO,
        1: FAN_LOW,
        2: FAN_MEDIUM,
        3: FAN_HIGH,
    }

    FAN_TO_MODE: Final = {
        value: key for key, value in MODE_TO_FAN.items()
    }

    FAN_MODES: Final[list[str]] = list(FAN_TO_MODE)

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
        """Keep the virtual thermostat available while W100 is unavailable.

        Allow it to receive and retain state changes for synchronization
        when W100 becomes available again.
        """
        return True

    @property
    def _temperature_cluster(self) -> TemperatureMeasurement:
        """Return temperature measurement cluster."""
        return self.endpoint.zigpy_endpoint.in_clusters[
            TemperatureMeasurement.cluster_id
        ]

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        temperature = self._temperature_cluster.get(
            TemperatureMeasurement.AttributeDefs.measured_value.name
        )
        return None if temperature is None else temperature / ZCL_TEMP

    @property
    def target_temperature(self) -> float:
        """Return the target temperature."""
        return float(
            self._cluster.get(W100ThermostatCluster.AttributeDefs.target_temperature.id)
        )

    @property
    def target_temperature_high(self) -> None:
        """Return the upper bound temperature we try to reach."""
        return None

    @property
    def target_temperature_low(self) -> None:
        """Return the lower bound temperature we try to reach."""
        return None

    @property
    def outdoor_temperature(self) -> None:
        """Return the outdoor temperature."""
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return HVAC operation mode."""
        return self.SYSTEM_TO_HVAC[
            self._cluster.get(W100ThermostatCluster.AttributeDefs.system_mode.id)
        ]

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available HVAC operation modes."""
        return self.HVAC_MODES

    @property
    def hvac_action(self) -> None:
        """Return the current HVAC action."""
        return None

    @property
    def fan_mode(self) -> str:
        """Return current fan mode."""
        return self.MODE_TO_FAN[
            self._cluster.get(W100ThermostatCluster.AttributeDefs.fan_mode.id)
        ]

    @property
    def fan_modes(self) -> list[str]:
        """Return supported FAN modes."""
        return self.FAN_MODES

    @property
    def preset_mode(self) -> None:
        """Return current preset mode."""
        return None

    @property
    def preset_modes(self) -> None:
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
        """Set new HVAC operation mode."""
        if hvac_mode not in self.hvac_modes:
            return

        system_mode = self.HVAC_TO_SYSTEM[hvac_mode]
        attributes = {"system_mode": system_mode}

        if system_mode != 0:
            attributes["last_mode"] = W100ThermostatCluster.SYSTEM_TO_M[system_mode]

        await self._cluster.write_attributes(attributes)

    async def async_set_temperature(
        self,
        target_temp_low: float | None = None,
        target_temp_high: float | None = None,
        temperature: float | None = None,
        hvac_mode: HVACMode | None = None,
    ) -> None:
        """Set new HVAC operation mode and/or target temperature."""
        attributes: dict[str, int | float] = {}

        if hvac_mode is not None:
            if hvac_mode not in self.hvac_modes:
                return

            system_mode = self.HVAC_TO_SYSTEM[hvac_mode]
            attributes["system_mode"] = system_mode

            if system_mode != 0:
                attributes["last_mode"] = W100ThermostatCluster.SYSTEM_TO_M[system_mode]

        if temperature is not None:
            attributes["target_temperature"] = temperature
        elif hvac_mode is None:
            return

        await self._cluster.write_attributes(attributes)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan mode."""
        if fan_mode not in self.fan_modes:
            return

        await self._cluster.write_attributes(
            {"fan_mode": self.FAN_TO_MODE[fan_mode]}
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        self.debug("Preset mode '%s' is not supported", preset_mode)


(
    QuirkBuilder("Aqara", "lumi.sensor_ht.agl001")
    .friendly_name(manufacturer="Aqara", model="Climate Sensor W100")
    .child_device(
        "thermostat",
        translation_key="thermostat_link",
        fallback_name="Thermostat link",
    )
    .child_device(
        "external_sensor",
        translation_key="external_sensor_link",
        fallback_name="External sensor link",
    )
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
    # Each button reports on its own endpoint. It counts its presses itself, reports a
    # hold at the hold threshold and the release after it (Zigbee2MQTT TH-S04D: hold 0,
    # single 1, double 2, release 255). Whether the hold is repeated while the button
    # stays down is not confirmed, so repeats are allowed, and the release only ends a
    # hold seen, in case it also follows other presses. Nothing is sent while the
    # device is in thermostat mode.
    .button_event(
        events=[
            ButtonEvent(
                event_type=ButtonEventType.SINGLE_PRESS,
                trigger=(SHORT_PRESS, "plus"),
            ),
            ButtonEvent(
                event_type=ButtonEventType.MULTI_PRESS,
                trigger=(DOUBLE_PRESS, "plus"),
                press_count=2,
            ),
            ButtonEvent(
                event_type=ButtonEventType.LONG_PRESS_START,
                trigger=(LONG_PRESS, "plus"),
                repeats=True,
            ),
            ButtonEvent(
                event_type=ButtonEventType.LONG_PRESS_END,
                trigger=(LONG_RELEASE, "plus"),
                while_held=True,
            ),
        ],
        cluster_id=MultistateInput.cluster_id,
        cluster_type=ClusterType.Server,
        endpoint_id=1,
        unique_id_suffix="button_plus",
        translation_key="plus_button",
        fallback_name="Plus button",
    )
    .button_event(
        events=[
            ButtonEvent(
                event_type=ButtonEventType.SINGLE_PRESS,
                trigger=(SHORT_PRESS, "center"),
            ),
            ButtonEvent(
                event_type=ButtonEventType.MULTI_PRESS,
                trigger=(DOUBLE_PRESS, "center"),
                press_count=2,
            ),
            ButtonEvent(
                event_type=ButtonEventType.LONG_PRESS_START,
                trigger=(LONG_PRESS, "center"),
                repeats=True,
            ),
            ButtonEvent(
                event_type=ButtonEventType.LONG_PRESS_END,
                trigger=(LONG_RELEASE, "center"),
                while_held=True,
            ),
        ],
        cluster_id=MultistateInput.cluster_id,
        cluster_type=ClusterType.Server,
        endpoint_id=2,
        unique_id_suffix="button_center",
        translation_key="center_button",
        fallback_name="Center button",
    )
    .button_event(
        events=[
            ButtonEvent(
                event_type=ButtonEventType.SINGLE_PRESS,
                trigger=(SHORT_PRESS, "minus"),
            ),
            ButtonEvent(
                event_type=ButtonEventType.MULTI_PRESS,
                trigger=(DOUBLE_PRESS, "minus"),
                press_count=2,
            ),
            ButtonEvent(
                event_type=ButtonEventType.LONG_PRESS_START,
                trigger=(LONG_PRESS, "minus"),
                repeats=True,
            ),
            ButtonEvent(
                event_type=ButtonEventType.LONG_PRESS_END,
                trigger=(LONG_RELEASE, "minus"),
                while_held=True,
            ),
        ],
        cluster_id=MultistateInput.cluster_id,
        cluster_type=ClusterType.Server,
        endpoint_id=3,
        unique_id_suffix="button_minus",
        translation_key="minus_button",
        fallback_name="Minus button",
    )
    .adds(W100ThermostatCluster)
    .adds(W100ExternalSensorCluster)
    .adds_entity(
        W100ClimateEntity,
        endpoint_id=1,
        cluster_id=W100ThermostatCluster.cluster_id,
        entity_type=EntityType.STANDARD,
        unique_id_suffix="thermostat",
        translation_key="thermostat",
        fallback_name="Thermostat",
        child_device_id="thermostat",
    )
    .adds_entity(
        W100ThermostatControlSwitch,
        endpoint_id=1,
        cluster_id=W100ManufacturerCluster.cluster_id,
        entity_type=EntityType.CONFIG,
        unique_id_suffix="thermostat_control",
        translation_key="remote_control",
        fallback_name="Remote control",
        child_device_id="thermostat",
    )
    .adds_entity(
        W100ExternalSensorSwitch,
        endpoint_id=1,
        cluster_id=W100ManufacturerCluster.cluster_id,
        entity_type=EntityType.CONFIG,
        unique_id_suffix="external_sensor",
        translation_key="external_sensor",
        fallback_name="External sensor",
        child_device_id="external_sensor",
    )
    .switch(
        attribute_name="thermostat_line_auto_hide",
        cluster_id=W100ManufacturerCluster.cluster_id,
        on_value=0,
        off_value=1,
        translation_key="thermostat_line_auto_hide",
        fallback_name="Thermostat line auto-hide",
        child_device_id="thermostat",
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
        translation_key="temperature",
        fallback_name="Temperature",
        child_device_id="external_sensor",
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
        translation_key="humidity",
        fallback_name="Humidity",
        child_device_id="external_sensor",
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
        translation_key="comfort_temperature_max",
        fallback_name="Comfort temperature max",
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
        translation_key="comfort_temperature_min",
        fallback_name="Comfort temperature min",
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
        translation_key="comfort_humidity_max",
        fallback_name="Comfort humidity max",
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
        translation_key="comfort_humidity_min",
        fallback_name="Comfort humidity min",
    )
    .enum(
        attribute_name="temperature_status",
        enum_class=TempHumidityStatus,
        cluster_id=W100ManufacturerCluster.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.STANDARD,
        translation_key="temperature_comfort_status",
        fallback_name="Temperature comfort status",
    )
    .enum(
        attribute_name="humidity_status",
        enum_class=TempHumidityStatus,
        cluster_id=W100ManufacturerCluster.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.STANDARD,
        translation_key="humidity_comfort_status",
        fallback_name="Humidity comfort status",
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
        device_class=NumberDeviceClass.TEMPERATURE_DELTA,
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
    .switch(
        attribute_name="fan_hide",
        cluster_id=W100ThermostatCluster.cluster_id,
        on_value=0,
        off_value=1,
        translation_key="show_fan",
        fallback_name="Show fan",
        child_device_id="thermostat",
    )
    .skip_configuration()
    .add_to_registry()
)

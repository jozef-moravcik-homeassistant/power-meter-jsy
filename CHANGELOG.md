<p align="center">
  <img src="https://raw.githubusercontent.com/jozef-moravcik-homeassistant/power-meter-jsy/main/images/power_meter_jsy_1.jpg" alt="Power Meter JSY" width="360">
</p>

<h1 align="center">⚡ Power Meter JSY for Home Assistant</h1>

<p align="center">
  <b>
  Multi-device Modbus integration for <a href="https://jsypowermeter.com" target="_blank" rel="noopener noreferrer">JSY Power Meter devices</a>.
  </b>
</p>

<p align="center">

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![GitHub release](https://img.shields.io/github/release/jozef-moravcik-homeassistant/power-meter-jsy.svg)](https://github.com/jozef-moravcik-homeassistant/power-meter-jsy/releases)
[![License](https://img.shields.io/github/license/jozef-moravcik-homeassistant/power-meter-jsy.svg)](LICENSE)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-custom%20integration-41BDF5.svg)](https://www.home-assistant.io)
[![IoT class](https://img.shields.io/badge/IoT%20class-local%20polling-blue.svg)](https://developers.home-assistant.io/docs/creating_integration_manifest/#iot-class)

</p>

## v1.01.02

## 📋 Changes
- Removed the forced reconnect of the core Modbus hub in “Existing Modbus Node” mode. The integration no longer touches the private internals of the core modbus integration.
- If the selected Modbus hub is not connected, this is now only reported through the error code sensor. Reconnecting is left to the core Modbus integration (this may take up to 60 s after switching a node from USB to an existing Modbus hub).
- Manifest: added after_dependencies: ["modbus"] and sorted the keys (Hassfest).
# Raspberry Pi Power Control
#
# Copyright (C) 2020 Jordan Ruthe <jordanruthe@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import os
import asyncio
import gpiod
from tornado.ioloop import IOLoop
from tornado import gen

class PrinterPower:
    def __init__(self, config):
        self.server = config.get_server()
        self.server.register_endpoint(
            "/machine/gpio_power/devices", ['GET'],
            self._handle_list_devices)
        self.server.register_endpoint(
            "/machine/gpio_power/status", ['GET'],
            self._handle_power_request)
        self.server.register_endpoint(
            "/machine/gpio_power/on", ['POST'],
            self._handle_power_request)
        self.server.register_endpoint(
            "/machine/gpio_power/off", ['POST'],
            self._handle_power_request)
        self.server.register_remote_method(
            "set_device_power", self.set_device_power)

        self.chip_factory = GpioChipFactory()
        self.current_dev = None
        self.devices = {}
        prefix_sections = config.get_prefix_sections("power")
        logging.info(f"Power plugin loading devices: {prefix_sections}")
        for section in prefix_sections:
            dev = GpioDevice(config[section], self.chip_factory)
            self.devices[dev.get_name()] = dev

    async def _handle_list_devices(self, web_request):
        dev_list = [d.get_device_info() for d in self.devices.values()]
        output = {"devices": dev_list}
        return output

    async def _handle_power_request(self, web_request):
        args = web_request.get_args()
        ep = web_request.get_endpoint()
        if not args:
            raise self.server.error("No arguments provided")
        requsted_devs = {k: self.devices.get(k, None) for k in args}
        result = {}
        req = ep.split("/")[-1]
        for name, device in requsted_devs.items():
            if device is not None:
                result[name] = await self._process_request(device, req)
            else:
                result[name] = "device_not_found"
        return result

    async def _process_request(self, device, req):
        if req in ["on", "off"]:
            ret = device.set_power(req)
            if asyncio.iscoroutine(ret):
                await ret
            dev_info = device.get_device_info()
            self.server.send_event("gpio_power:power_changed", dev_info)
        elif req == "status":
            ret = device.refresh_status()
            if asyncio.iscoroutine(ret):
                await ret
            dev_info = device.get_device_info()
        else:
            raise self.server.error(f"Unsupported power request: {req}")
        return dev_info['status']

    def set_device_power(self, device, state):
        status = None
        if isinstance(state, bool):
            status = "on" if state else "off"
        elif isinstance(state, str):
            status = state.lower()
            if status in ["true", "false"]:
                status = "on" if status == "true" else "off"
        if status not in ["on", "off"]:
            logging.info(f"Invalid state received: {state}")
            return
        if device not in self.devices:
            logging.info(f"No device found: {device}")
            return
        ioloop = IOLoop.current()
        ioloop.spawn_callback(
            self._process_request, self.devices[device], status)

    async def add_device(self, name, device):
        if name in self.devices:
            raise self.server.error(
                f"Device [{name}] already configured")
        ret = device.initialize()
        if asyncio.iscoroutine(ret):
            await ret
        self.devices[name] = device

    async def close(self):
        for device in self.devices.values():
            if hasattr(device, "close"):
                ret = device.close()
                if asyncio.iscoroutine(ret):
                    await ret
        self.chip_factory.close()


class GpioChipFactory:
    def __init__(self):
        self.chips = {}

    def get_gpio_chip(self, chip_name):
        if chip_name in self.chips:
            return self.chips[chip_name]
        chip = gpiod.Chip(chip_name, gpiod.Chip.OPEN_BY_NAME)
        self.chips[chip_name] = chip
        return chip

    def close(self):
        for chip in self.chips.values():
            chip.close()

class GpioDevice:
    def __init__(self, config, chip_factory):
        name_parts = config.get_name().split(maxsplit=1)
        if len(name_parts) != 2:
            raise config.error(f"Invalid Section Name: {config.get_name()}")
        self.name = name_parts[1]
        self.state = "init"
        pin, chip_id, invert = self._parse_pin(config)
        try:
            chip = chip_factory.get_gpio_chip(chip_id)
            self.line = chip.get_line(pin)
            if invert:
                self.line.request(
                    consumer="moonraker", type=gpiod.LINE_REQ_DIR_OUT,
                    flags=gpiod.LINE_REQ_FLAG_ACTIVE_LOW)
            else:
                self.line.request(
                    consumer="moonraker", type=gpiod.LINE_REQ_DIR_OUT)
        except Exception:
            self.state = "error"
            logging.exception(
                f"Unable to init {pin}.  Make sure the gpio is not in "
                "use by another program or exported by sysfs.")
            raise config.error("Power GPIO Config Error")
        self.set_power("off")

    def _parse_pin(self, config):
        pin = cfg_pin = config.get("pin")
        invert = False
        if pin[0] == "!":
            pin = pin[1:]
            invert = True
        chip_id = "gpiochip0"
        pin_parts = pin.split("/")
        if len(pin_parts) == 2:
            chip_id, pin = pin_parts
        elif len(pin_parts) == 1:
            pin = pin_parts[0]
        # Verify pin
        if not chip_id.startswith("gpiochip") or \
                not chip_id[-1].isdigit() or \
                not pin.startswith("gpio") or \
                not pin[4:].isdigit():
            raise config.error(
                f"Invalid Power Pin configuration: {cfg_pin}")
        pin = int(pin[4:])
        return pin, chip_id, invert

    def initialize(self):
        pass

    def get_name(self):
        return self.name

    def get_device_info(self):
        return {
            'device': self.name,
            'status': self.state,
            'type': "gpio"
        }

    def refresh_status(self):
        try:
            val = self.line.get_value()
        except Exception:
            self.state = "error"
            msg = f"Error Refeshing Device Status: {self.name}"
            logging.exception(msg)
            raise self.server.error(msg) from None
        self.state = "on" if val else "off"

    def set_power(self, state):
        try:
            self.line.set_value(int(state == "on"))
        except Exception:
            self.state = "error"
            msg = f"Error Toggling Device Power: {self.name}"
            logging.exception(msg)
            raise self.server.error(msg) from None
        self.state = state

    def close(self):
        self.line.release()

# The power plugin has multiple configuration sections
def load_plugin_multi(config):
    return PrinterPower(config)

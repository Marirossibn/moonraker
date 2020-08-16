# Moonraker - HTTP/Websocket API Server for Klipper
#
# Copyright (C) 2020 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license
import argparse
import sys
import importlib
import os
import time
import socket
import logging
import json
import confighelper
from tornado import gen, iostream
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.util import TimeoutError
from tornado.locks import Event
from app import MoonrakerApp
from utils import ServerError, MoonrakerLoggingHandler

INIT_MS = 1000

CORE_PLUGINS = [
    'file_manager', 'klippy_apis', 'machine',
    'temperature_store', 'shell_command']

class Sentinel:
    pass

class Server:
    error = ServerError
    def __init__(self, args):
        config = confighelper.get_configuration(self, args)
        self.host = config.get('host', "0.0.0.0")
        self.port = config.getint('port', 7125)

        # Event initialization
        self.events = {}

        # Klippy Connection Handling
        self.klippy_address = config.get(
            'klippy_uds_address', "/tmp/klippy_uds")
        self.klippy_connection = KlippyConnection(
            self.process_command, self.on_connection_closed)
        self.init_list = []
        self.klippy_state = "disconnected"

        # XXX - currently moonraker maintains a superset of all
        # subscriptions, the results of which are forwarded to all
        # connected websockets. A better implementation would open a
        # unique unix domain socket for each websocket client and
        # allow Klipper to forward only those subscriptions back to
        # correct client.
        self.all_subscriptions = {}

        # Server/IOLoop
        self.server_running = False
        self.moonraker_app = app = MoonrakerApp(config)
        self.register_endpoint = app.register_local_handler
        self.register_static_file_handler = app.register_static_file_handler
        self.register_upload_handler = app.register_upload_handler
        self.ioloop = IOLoop.current()
        self.init_cb = PeriodicCallback(self._initialize, INIT_MS)

        # Setup remote methods accessable to Klippy.  Note that all
        # registered remote methods should be of the notification type,
        # they do not return a response to Klippy after execution
        self.pending_requests = {}
        self.remote_methods = {}
        self.register_remote_method(
            'process_gcode_response', self._process_gcode_response)
        self.register_remote_method(
            'process_status_update', self._process_status_update)

        # Plugin initialization
        self.plugins = {}
        self.klippy_apis = self.load_plugin(config, 'klippy_apis')
        self._load_plugins(config)

    def start(self):
        logging.info(
            f"Starting Moonraker on ({self.host}, {self.port})")
        self.moonraker_app.listen(self.host, self.port)
        self.server_running = True
        self.ioloop.spawn_callback(self._connect_klippy)

    # ***** Plugin Management *****
    def _load_plugins(self, config):
        # load core plugins
        for plugin in CORE_PLUGINS:
            self.load_plugin(config, plugin)

        # check for optional plugins
        opt_sections = set(config.sections()) - \
            set(['server', 'authorization', 'cmd_args'])
        for section in opt_sections:
            self.load_plugin(config[section], section, None)

    def load_plugin(self, config, plugin_name, default=Sentinel):
        if plugin_name in self.plugins:
            return self.plugins[plugin_name]
        # Make sure plugin exists
        mod_path = os.path.join(
            os.path.dirname(__file__), 'plugins', plugin_name + '.py')
        if not os.path.exists(mod_path):
            msg = f"Plugin ({plugin_name}) does not exist"
            logging.info(msg)
            if default == Sentinel:
                raise ServerError(msg)
            return default
        module = importlib.import_module("plugins." + plugin_name)
        try:
            load_func = getattr(module, "load_plugin")
            plugin = load_func(config)
        except Exception:
            msg = f"Unable to load plugin ({plugin_name})"
            logging.info(msg)
            if default == Sentinel:
                raise ServerError(msg)
            return default
        self.plugins[plugin_name] = plugin
        logging.info(f"Plugin ({plugin_name}) loaded")
        return plugin

    def lookup_plugin(self, plugin_name, default=Sentinel):
        plugin = self.plugins.get(plugin_name, default)
        if plugin == Sentinel:
            raise ServerError(f"Plugin ({plugin_name}) not found")
        return plugin

    def register_event_handler(self, event, callback):
        self.events.setdefault(event, []).append(callback)

    def send_event(self, event, *args):
        events = self.events.get(event, [])
        for evt in events:
            self.ioloop.spawn_callback(evt, *args)

    def register_remote_method(self, method_name, cb):
        if method_name in self.remote_methods:
            # XXX - may want to raise an exception here
            logging.info(f"Remote method ({method_name}) already registered")
            return
        self.remote_methods[method_name] = cb

    # ***** Klippy Connection *****
    async def _connect_klippy(self):
        ret = await self.klippy_connection.connect(self.klippy_address)
        if not ret:
            self.ioloop.call_later(1., self._connect_klippy)
            return
        # begin server iniialization
        self.init_cb.start()

    def process_command(self, cmd):
        method = cmd.get('method', None)
        if method is not None:
            # This is a remote method called from klippy
            cb = self.remote_methods.get(method, None)
            if cb is not None:
                params = cmd.get('params', {})
                cb(**params)
            else:
                logging.info(f"Unknown method received: {method}")
            return
        # This is a response to a request, process
        req_id = cmd.get('id', None)
        request = self.pending_requests.pop(req_id, None)
        if request is None:
            logging.info(
                f"No request matching request ID: {req_id}, "
                f"response: {cmd}")
            return
        if 'result' in cmd:
            result = cmd['result']
            if not result:
                result = "ok"
        else:
            err = cmd.get('error', "Malformed Klippy Response")
            result = ServerError(err, 400)
        request.notify(result)

    def on_connection_closed(self):
        self.init_list = []
        self.klippy_state = "disconnected"
        self.init_cb.stop()
        for request in self.pending_requests.values():
            request.notify(ServerError("Klippy Disconnected", 503))
        self.pending_requests = {}
        logging.info("Klippy Connection Removed")
        self.send_event("server:klippy_disconnect")
        self.ioloop.call_later(1., self._connect_klippy)

    async def _initialize(self):
        await self._check_ready()
        await self._request_endpoints()
        # Subscribe to "webhooks"
        # Register "webhooks" subscription
        if "webhooks_sub" not in self.init_list:
            try:
                await self.klippy_apis.subscribe_objects({'webhooks': None})
            except ServerError as e:
                logging.info(f"{e}\nUnable to subscribe to webhooks object")
            else:
                logging.info("Webhooks Subscribed")
                self.init_list.append("webhooks_sub")
        # Subscribe to Gcode Output
        if "gcode_output_sub" not in self.init_list:
            try:
                await self.klippy_apis.subscribe_gcode_output()
            except ServerError as e:
                logging.info(
                    f"{e}\nUnable to register gcode output subscription")
            else:
                logging.info("GCode Output Subscribed")
                self.init_list.append("gcode_output_sub")
        if "klippy_ready" in self.init_list:
            # Moonraker is enabled in the Klippy module
            # and Klippy is ready.  We can stop the init
            # procedure.
            self.init_cb.stop()

    async def _request_endpoints(self):
        result = await self.klippy_apis.list_endpoints(default=None)
        if result is None:
            return
        endpoints = result.get('endpoints', {})
        for ep in endpoints:
            self.moonraker_app.register_remote_handler(ep)

    async def _check_ready(self):
        try:
            result = await self.klippy_apis.get_klippy_info()
        except ServerError as e:
            logging.info(
                f"{e}\nKlippy info request error.  This indicates that\n"
                f"Klippy may have experienced an error during startup.\n"
                f"Please check klippy.log for more information")
            return
        # Update filemanager fixed paths
        fixed_paths = {k: result[k] for k in
                       ['klipper_path', 'python_path',
                        'log_file', 'config_file']}
        file_manager = self.lookup_plugin('file_manager')
        file_manager.update_fixed_paths(fixed_paths)
        is_ready = result.get('state', "") == "ready"
        if is_ready:
            await self._verify_klippy_requirements()
            logging.info("Klippy ready")
            self.klippy_state = "ready"
            self.init_list.append('klippy_ready')
            self.send_event("server:klippy_ready")
        else:
            msg = result.get('state_message', "Klippy Not Ready")
            logging.info("\n" + msg)

    async def _verify_klippy_requirements(self):
        result = await self.klippy_apis.get_object_list(default=None)
        if result is None:
            logging.info(
                f"Unable to retreive Klipper Object List")
            return
        req_objs = set(["virtual_sdcard", "display_status", "pause_resume"])
        missing_objs = req_objs - set(result)
        if missing_objs:
            err_str = ", ".join([f"[{o}]" for o in missing_objs])
            logging.info(
                f"\nWarning, unable to detect the following printer "
                f"objects:\n{err_str}\nPlease add the the above sections "
                f"to printer.cfg for full Moonraker functionality.")
        if "virtual_sdcard" not in missing_objs:
            # Update the gcode path
            result = await self.klippy_apis.query_objects(
                {'configfile': None}, default=None)
            if result is None:
                logging.info(f"Unable to set SD Card path")
            else:
                config = result.get('configfile', {}).get('config', {})
                vsd_config = config.get('virtual_sdcard', {})
                vsd_path = vsd_config.get('path', None)
                if vsd_path is not None:
                    file_manager = self.lookup_plugin('file_manager')
                    file_manager.register_directory(
                        'gcodes', vsd_path, can_delete=True)
                else:
                    logging.info(
                        "Configuration for [virtual_sdcard] not found,"
                        " unable to set SD Card path")

    def _process_gcode_response(self, response):
        self.send_event("server:gcode_response", response)

    def _process_status_update(self, eventtime, status):
        if 'webhooks' in status:
            # XXX - process other states (startup, ready, error, etc)?
            state = status['webhooks'].get('state', None)
            if state is not None:
                if state == "shutdown":
                    logging.info("Klippy has shutdown")
                    self.send_event("server:klippy_shutdown")
                self.klippy_state = state
        self.send_event("server:status_update", status)

    async def make_request(self, rpc_method, params):
        # XXX - This adds the "response_template" to a subscription
        # request and tracks all subscriptions so that each
        # client gets what its requesting.  In the future we should
        # track subscriptions per client and send clients only
        # the data they are asking for.
        if rpc_method == "objects/subscribe":
            for obj, items in params.get('objects', {}).items():
                if obj in self.all_subscriptions:
                    pi = self.all_subscriptions[obj]
                    if items is None or pi is None:
                        self.all_subscriptions[obj] = None
                    else:
                        uitems = list(set(pi) | set(items))
                        self.all_subscriptions[obj] = uitems
                else:
                    self.all_subscriptions[obj] = items
            params['objects'] = dict(self.all_subscriptions)
            params['response_template'] = {'method': "process_status_update"}

        base_request = BaseRequest(rpc_method, params)
        self.pending_requests[base_request.id] = base_request
        self.ioloop.spawn_callback(
            self.klippy_connection.send_request, base_request)
        result = await base_request.wait()
        return result

    async def _stop_server(self):
        # XXX - Currently this function is not used.
        # Should I expose functionality to shutdown
        # or restart the server, or simply remove this?
        logging.info(
            "Shutting Down Webserver")
        for plugin in self.plugins:
            if hasattr(plugin, "close"):
                await plugin.close()
        self.klippy_connection.close()
        if self.server_running:
            self.server_running = False
            await self.moonraker_app.close()
            self.ioloop.stop()

class KlippyConnection:
    def __init__(self, on_recd, on_close):
        self.ioloop = IOLoop.current()
        self.iostream = None
        self.on_recd = on_recd
        self.on_close = on_close

    async def connect(self, address):
        ksock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        kstream = iostream.IOStream(ksock)
        try:
            await kstream.connect(address)
        except iostream.StreamClosedError:
            return False
        logging.info("Klippy Connection Established")
        self.iostream = kstream
        self.iostream.set_close_callback(self.on_close)
        self.ioloop.spawn_callback(self._read_stream, self.iostream)
        return True

    async def _read_stream(self, stream):
        while not stream.closed():
            try:
                data = await stream.read_until(b'\x03')
            except iostream.StreamClosedError as e:
                return
            except Exception:
                logging.exception("Klippy Stream Read Error")
                continue
            try:
                decoded_cmd = json.loads(data[:-1])
                self.on_recd(decoded_cmd)
            except Exception:
                logging.exception(
                    f"Error processing Klippy Host Response: {data.decode()}")

    async def send_request(self, request):
        if self.iostream is None:
            request.notify(ServerError("Klippy Host not connected", 503))
            return
        data = json.dumps(request.to_dict()).encode() + b"\x03"
        try:
            await self.iostream.write(data)
        except iostream.StreamClosedError:
            request.notify(ServerError("Klippy Host not connected", 503))

    def is_connected(self):
        return self.iostream is not None and not self.iostream.closed()

    def close(self):
        if self.iostream is not None and \
                not self.iostream.closed():
            self.iostream.close()

# Basic WebRequest class, easily converted to dict for json encoding
class BaseRequest:
    def __init__(self, rpc_method, params):
        self.id = id(self)
        self.rpc_method = rpc_method
        self.params = params
        self._event = Event()
        self.response = None

    async def wait(self):
        # Log pending requests every 60 seconds
        start_time = time.time()
        while True:
            timeout = time.time() + 60.
            try:
                await self._event.wait(timeout=timeout)
            except TimeoutError:
                pending_time = time.time() - start_time
                logging.info(
                    f"Request '{self.rpc_method}' pending: "
                    f"{pending_time:.2f} seconds")
                self._event.clear()
                continue
            break
        if isinstance(self.response, ServerError):
            raise self.response
        return self.response

    def notify(self, response):
        self.response = response
        self._event.set()

    def to_dict(self):
        return {'id': self.id, 'method': self.rpc_method,
                'params': self.params}

def main():
    # Parse start arguments
    parser = argparse.ArgumentParser(
        description="Moonraker - Klipper API Server")
    parser.add_argument(
        "-c", "--configfile", default="~/moonraker.conf",
        metavar='<configfile>',
        help="Location of moonraker configuration file")
    parser.add_argument(
        "-l", "--logfile", default="/tmp/moonraker.log", metavar='<logfile>',
        help="log file name and location")
    cmd_line_args = parser.parse_args()

    # Setup Logging
    log_file = os.path.normpath(os.path.expanduser(cmd_line_args.logfile))
    cmd_line_args.logfile = log_file
    root_logger = logging.getLogger()
    file_hdlr = MoonrakerLoggingHandler(
        log_file, when='midnight', backupCount=2)
    root_logger.addHandler(file_hdlr)
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        '%(asctime)s [%(filename)s:%(funcName)s()] - %(message)s')
    file_hdlr.setFormatter(formatter)

    if sys.version_info < (3, 7):
        msg = f"Moonraker requires Python 3.7 or above.  " \
            f"Detected Version: {sys.version}"
        logging.info(msg)
        print(msg)
        exit(1)

    # Start IOLoop and Server
    io_loop = IOLoop.current()
    try:
        server = Server(cmd_line_args)
    except Exception:
        logging.exception("Moonraker Error")
        exit(1)
    try:
        server.start()
        io_loop.start()
    except Exception:
        logging.exception("Server Running Error")
    io_loop.close(True)
    logging.info("Server Shutdown")


if __name__ == '__main__':
    main()

"""Pytest configuration: installs a stub `indigo` module before any plugin import.

The stub follows the Auto Lights pattern (types.SimpleNamespace registered in
sys.modules) extended with the pieces this plugin exercises: device addresses,
plugin props replacement, folder management, and device creation recording.
"""

import datetime
import logging
import os
import sys
import types

indigo_stub = types.SimpleNamespace()


class Devices(dict):
    """Dict keyed by device id that also supports iteration and filtering."""

    def __iter__(self):
        return iter(self.values())

    def __missing__(self, key):
        dev = indigo_stub.Device(key)
        self[key] = dev
        return dev

    def iter(self, filter=""):
        for dev in self.values():
            if not filter or filter == "self":
                yield dev
            elif filter.startswith("self."):
                if dev.deviceTypeId == filter.split(".", 1)[1]:
                    yield dev


class Variables(dict):
    def __iter__(self):
        return iter(self.values())

    def __missing__(self, key):
        var = indigo_stub.Variable(key)
        self[key] = var
        return var


class Folder:
    def __init__(self, id, name):
        self.id = id
        self.name = name


class Folders(dict):
    def __iter__(self):
        return iter(self.values())

    def __contains__(self, key):
        if isinstance(key, str):
            return any(f.name == key for f in self.values())
        return dict.__contains__(self, key)

    def __getitem__(self, key):
        if isinstance(key, str):
            for f in self.values():
                if f.name == key:
                    return f
            raise KeyError(key)
        return dict.__getitem__(self, key)


class Device:
    def __init__(self, id, name="", address="", deviceTypeId="", folderId=0):
        self.id = id
        self.name = name or f"Dev-{id}"
        self.address = address
        self.deviceTypeId = deviceTypeId
        self.folderId = folderId
        self.pluginId = "com.vtmikel.justaddpower"
        self.pluginProps = {}
        self.states = {}
        self.enabled = True
        self.configured = True
        self.lastChanged = datetime.datetime.now()
        self.errorState = ""

    def __iter__(self):
        return iter(self.states.items())

    def updateStateOnServer(self, key, value, uiValue=None):
        self.states[key] = value

    def updateStatesOnServer(self, state_list):
        for entry in state_list:
            self.states[entry["key"]] = entry["value"]

    def replacePluginPropsOnServer(self, props):
        self.pluginProps = dict(props)

    def replaceOnServer(self):
        pass

    def stateListOrDisplayStateIdChanged(self):
        pass

    def setErrorStateOnServer(self, message):
        self.errorState = message or ""


class Variable:
    def __init__(self, id, name="", value=None):
        self.id = id
        self.name = name
        self.value = value


class _DummyHandler(logging.Handler):
    def __init__(self, baseFilename="/tmp/Logs/com.vtmikel.justaddpower/plugin.log"):
        super().__init__()
        self.baseFilename = baseFilename

    def emit(self, record):
        pass


class PluginBase:
    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs):
        self.pluginId = plugin_id
        self.pluginDisplayName = plugin_display_name
        self.pluginVersion = plugin_version
        self.pluginPrefs = plugin_prefs
        self.logger = logging.getLogger("Plugin")
        self.indigo_log_handler = _DummyHandler()
        self.plugin_file_handler = _DummyHandler()

    class StopThread(Exception):
        pass

    def sleep(self, seconds):
        pass


def _next_id(container):
    return max(container.keys(), default=1000) + 1


def _create_device(protocol=None, address="", name="", description="", deviceTypeId="", props=None, folder=0):
    dev_id = _next_id(indigo_stub.devices)
    folder_id = folder.id if isinstance(folder, Folder) else folder
    dev = Device(dev_id, name=name, address=address, deviceTypeId=deviceTypeId, folderId=folder_id)
    dev.pluginProps = dict(props or {})
    indigo_stub.devices[dev_id] = dev
    indigo_stub.device.created.append(dev)
    return dev


def _delete_device(dev):
    dev_id = dev.id if isinstance(dev, Device) else dev
    indigo_stub.devices.pop(dev_id, None)


def _create_folder(name):
    folder_id = _next_id(indigo_stub.devices.folders)
    folder = Folder(folder_id, name)
    indigo_stub.devices.folders[folder_id] = folder
    return folder


indigo_stub.Device = Device
indigo_stub.Variable = Variable
indigo_stub.PluginBase = PluginBase
indigo_stub.devices = Devices()
indigo_stub.devices.folders = Folders()
indigo_stub.devices.folder = types.SimpleNamespace(create=_create_folder)
indigo_stub.variables = Variables()
indigo_stub.device = types.SimpleNamespace(create=_create_device, delete=_delete_device, created=[])
indigo_stub.variable = types.SimpleNamespace()
indigo_stub.kProtocol = types.SimpleNamespace(Plugin="Plugin")


class IndigoDict(dict):
    pass


indigo_stub.Dict = IndigoDict
indigo_stub.server = types.SimpleNamespace(log=lambda *a, **k: None)

sys.modules["indigo"] = indigo_stub

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            os.pardir,
            "Just Add Power.indigoPlugin",
            "Contents",
            "Server Plugin",
        )
    ),
)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fake_indigo():
    """Reset the stub indigo module before each test."""
    indigo_stub.devices.clear()
    indigo_stub.devices.folders.clear()
    indigo_stub.variables.clear()
    indigo_stub.device.created.clear()
    yield indigo_stub

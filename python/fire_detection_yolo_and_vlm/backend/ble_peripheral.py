"""
BLE L2CAP peripheral stub for Linux/Jetson.

Requires BlueZ and pybluez (or dbus-python) for production use.
This module provides the interface — actual BLE setup depends on the
host Bluetooth stack and is not yet implemented.
"""

SERVICE_UUID = "0000ABED-0000-1000-8000-00805F9B34FB"
PSM_CHARACTERISTIC_UUID = "0000ABEE-0000-1000-8000-00805F9B34FB"
L2CAP_PSM = 0x0025


class BLEL2CAPPeripheral:
    def __init__(self, on_data_received=None):
        self.on_data_received = on_data_received
        self._running = False

    def start(self):
        """Start advertising and listening for L2CAP connections.

        TODO: Implement with BlueZ D-Bus API or pybluez:
        1. Register GATT service with SERVICE_UUID
        2. Add PSM characteristic that advertises L2CAP_PSM
        3. Open L2CAP CoC socket on L2CAP_PSM
        4. Accept connections and read/write framed data
        """
        self._running = True

    def stop(self):
        self._running = False

    def send(self, data: bytes):
        """Send data to connected central over L2CAP channel.

        TODO: Write length-prefixed frame to L2CAP socket.
        """
        pass

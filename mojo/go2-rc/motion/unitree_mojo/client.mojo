from std.ffi import OwnedDLHandle, c_int, c_size_t

from .state import Go2State


comptime ABI_VERSION = 1
comptime DEFAULT_LIBRARY = "libunitree_mojo.so"


struct Go2Client(Movable):
    """Native Mojo owner for a Unitree Go2 SDK2 client."""

    var _library: OwnedDLHandle
    var _handle: OpaquePointer[MutUntrackedOrigin]

    def __init__(
        out self,
        network_interface: String,
        library_path: String = DEFAULT_LIBRARY,
        velocity_watchdog_ms: UInt32 = 1000,
    ) raises:
        self._library = OwnedDLHandle(library_path)
        var abi_version = self._library.get_function[UInt32](
            "unitree_mojo_abi_version"
        )
        if abi_version() != ABI_VERSION:
            raise Error("unitree_mojo C ABI version mismatch")
        var create = self._library.get_function[
            OpaquePointer[MutUntrackedOrigin]
        ]("unitree_mojo_create")
        var interface_string = network_interface
        self._handle = create(
            interface_string.as_c_string_slice().unsafe_ptr(),
            velocity_watchdog_ms,
        )
        if Int(self._handle) == 0:
            raise Error("Unitree SDK2 client initialization failed")

    def __deinit__(deinit self):
        if Int(self._handle) != 0:
            try:
                var destroy = self._library.get_function[NoneType](
                    "unitree_mojo_destroy"
                )
                destroy(self._handle)
            except:
                pass

    def is_ready(ref self) raises -> Bool:
        var call = self._library.get_function[c_int]("unitree_mojo_is_ready")
        return call(self._handle) == 1

    def _check(ref self, name: String, result: c_int) raises:
        if result != 0:
            raise Error(name + " failed: " + self.last_error())

    def set_velocity(
        ref self, vx: Float32 = 0, vy: Float32 = 0, vyaw: Float32 = 0
    ) raises:
        var call = self._library.get_function[c_int](
            "unitree_mojo_set_velocity"
        )
        self._check("Move", call(self._handle, vx, vy, vyaw))

    def move_for(
        ref self,
        vx: Float32 = 0,
        vy: Float32 = 0,
        vyaw: Float32 = 0,
        duration_ms: UInt32 = 2000,
    ) raises:
        var call = self._library.get_function[c_int]("unitree_mojo_move_for")
        self._check("Move", call(self._handle, vx, vy, vyaw, duration_ms))

    def stop(ref self) raises:
        self._simple_command("unitree_mojo_stop", "StopMove")

    def stand_up(ref self) raises:
        self._simple_command("unitree_mojo_stand_up", "StandUp")

    def sit(ref self) raises:
        self._simple_command("unitree_mojo_sit", "Sit")

    def stand_down(ref self) raises:
        self._simple_command("unitree_mojo_stand_down", "StandDown")

    def hello(ref self) raises:
        self._simple_command("unitree_mojo_hello", "Hello")

    def dance(ref self) raises:
        self._simple_command("unitree_mojo_dance", "Dance1")

    def _simple_command(ref self, symbol: String, name: String) raises:
        # Symbol names are dynamic by design: this package loads a stable C ABI.
        var call = self._library.get_function[c_int](symbol)
        self._check(name, call(self._handle))

    def state_json(ref self) raises -> String:
        var buffer = List[UInt8](unsafe_uninit_length=512)
        var call = self._library.get_function[c_int]("unitree_mojo_state_json")
        var count = call(self._handle, buffer.unsafe_ptr(), c_size_t(512))
        if count < 0:
            raise Error("reading LowState failed")
        var bytes = List[UInt8]()
        for index in range(Int(count)):
            bytes.append(buffer[index])
        return String(from_utf8=bytes)

    def latest_state(ref self) raises -> Go2State:
        # unitree_mojo_state is a 36-byte, 4-byte-aligned versioned C struct.
        var storage = List[UInt8](unsafe_uninit_length=36)
        var call = self._library.get_function[c_int]("unitree_mojo_get_state")
        var result = call(self._handle, storage.unsafe_ptr())
        if result < 0:
            raise Error("reading LowState failed")
        var bytes = storage.unsafe_ptr()
        var words = bytes.unsafe_bitcast[UInt32]()
        if words[unsafe_offset=0] != ABI_VERSION:
            raise Error("unitree_mojo state ABI version mismatch")
        var floats = bytes.unsafe_bitcast[Float32]()
        var forces = bytes.unsafe_bitcast[Int16]()
        return Go2State(
            valid=bytes[unsafe_offset=4] == 1,
            battery_soc=bytes[unsafe_offset=5],
            power_v=floats[unsafe_offset=2],
            roll=floats[unsafe_offset=3],
            pitch=floats[unsafe_offset=4],
            yaw=floats[unsafe_offset=5],
            foot_front_right=forces[unsafe_offset=12],
            foot_front_left=forces[unsafe_offset=13],
            foot_rear_right=forces[unsafe_offset=14],
            foot_rear_left=forces[unsafe_offset=15],
            tick=words[unsafe_offset=8],
        )

    def has_state(ref self) raises -> Bool:
        return self.state_json() != "{}"

    def last_error(ref self) raises -> String:
        var buffer = List[UInt8](unsafe_uninit_length=512)
        var call = self._library.get_function[c_int]("unitree_mojo_last_error")
        var count = call(self._handle, buffer.unsafe_ptr(), c_size_t(512))
        if count <= 0:
            return String("unknown Unitree SDK2 error")
        var bytes = List[UInt8]()
        for index in range(Int(count)):
            bytes.append(buffer[index])
        return String(from_utf8=bytes)

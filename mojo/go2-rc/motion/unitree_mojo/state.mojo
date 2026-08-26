struct Go2State(Copyable, Movable):
    """A snapshot of the Go2 `rt/lowstate` fields exposed by this package."""

    var valid: Bool
    var battery_soc: UInt8
    var power_v: Float32
    var roll: Float32
    var pitch: Float32
    var yaw: Float32
    var foot_front_right: Int16
    var foot_front_left: Int16
    var foot_rear_right: Int16
    var foot_rear_left: Int16
    var tick: UInt32

    def __init__(
        out self,
        valid: Bool,
        battery_soc: UInt8,
        power_v: Float32,
        roll: Float32,
        pitch: Float32,
        yaw: Float32,
        foot_front_right: Int16,
        foot_front_left: Int16,
        foot_rear_right: Int16,
        foot_rear_left: Int16,
        tick: UInt32,
    ):
        self.valid = valid
        self.battery_soc = battery_soc
        self.power_v = power_v
        self.roll = roll
        self.pitch = pitch
        self.yaw = yaw
        self.foot_front_right = foot_front_right
        self.foot_front_left = foot_front_left
        self.foot_rear_right = foot_rear_right
        self.foot_rear_left = foot_rear_left
        self.tick = tick

# ALSA PCM capture/playback through libasound.so.2 via OwnedDLHandle.
# snd_pcm_set_params() does all format negotiation in one call, so no
# hw_params struct ABI is involved anywhere. Devices open non-blocking:
# read_available()/write_some() return what the device offers and the
# single-threaded app loop stays responsive.
from std.ffi import OwnedDLHandle, c_int

comptime SND_PCM_STREAM_PLAYBACK = 0
comptime SND_PCM_STREAM_CAPTURE = 1
comptime SND_PCM_NONBLOCK = 1
comptime SND_PCM_FORMAT_S16_LE = 2
comptime SND_PCM_ACCESS_RW_INTERLEAVED = 3
comptime _EAGAIN = -11
comptime _LATENCY_US = 100000


def _cstr(s: String) -> List[UInt8]:
    var out = List[UInt8]()
    for b in s.as_bytes():
        out.append(b)
    out.append(0)
    return out^


struct AlsaPcm(Movable):
    var lib: OwnedDLHandle
    var pcm: Int
    var channels: Int

    def __init__(out self, var lib: OwnedDLHandle, pcm: Int, channels: Int):
        self.lib = lib^
        self.pcm = pcm
        self.channels = channels

    @staticmethod
    def _open(device: String, rate: Int, channels: Int, stream: Int) raises -> AlsaPcm:
        # "hw:" ids go through the plug layer so S16LE/rate conversion works
        # on hardware with other native formats (GStreamer's audioconvert
        # equivalent, done by ALSA itself).
        var name = device
        if name.startswith("hw:"):
            name = "plug" + name
        var lib = OwnedDLHandle("libasound.so.2")
        var handle_out = List[Int]()
        handle_out.append(0)
        var cname = _cstr(name)
        var rc = Int(
            lib.call["snd_pcm_open", c_int](
                handle_out.unsafe_ptr(),
                cname.unsafe_ptr(),
                c_int(stream),
                c_int(SND_PCM_NONBLOCK),
            )
        )
        if rc != 0:
            raise Error("snd_pcm_open(" + name + ") failed: " + String(rc))
        var pcm = handle_out[0]
        rc = Int(
            lib.call["snd_pcm_set_params", c_int](
                pcm,
                c_int(SND_PCM_FORMAT_S16_LE),
                c_int(SND_PCM_ACCESS_RW_INTERLEAVED),
                c_int(channels),
                c_int(rate),
                c_int(1),  # soft_resample
                c_int(_LATENCY_US),
            )
        )
        if rc != 0:
            _ = lib.call["snd_pcm_close", c_int](pcm)
            raise Error("snd_pcm_set_params(" + name + ") failed: " + String(rc))
        return AlsaPcm(lib^, pcm, channels)

    @staticmethod
    def open_capture(device: String, rate: Int, channels: Int) raises -> AlsaPcm:
        return AlsaPcm._open(device, rate, channels, SND_PCM_STREAM_CAPTURE)

    @staticmethod
    def open_playback(device: String, rate: Int, channels: Int) raises -> AlsaPcm:
        return AlsaPcm._open(device, rate, channels, SND_PCM_STREAM_PLAYBACK)

    def read_available(mut self, max_frames: Int) raises -> List[UInt8]:
        # One non-blocking readi: returns captured S16LE bytes (possibly
        # empty). Xruns are recovered silently; a vanished device raises.
        var buf = List[UInt8](unsafe_uninit_length=max_frames * self.channels * 2)
        var got = Int(
            self.lib.call["snd_pcm_readi", Int](
                self.pcm, buf.unsafe_ptr(), max_frames
            )
        )
        if got > 0:
            var out = List[UInt8]()
            for i in range(got * self.channels * 2):
                out.append(buf[i])
            return out^
        if got == _EAGAIN or got == 0:
            return List[UInt8]()
        var rc = Int(
            self.lib.call["snd_pcm_recover", c_int](self.pcm, c_int(got), c_int(1))
        )
        if rc != 0:
            raise Error("capture failed: " + String(got))
        return List[UInt8]()

    def write_some(mut self, data: List[UInt8], byte_offset: Int) raises -> Int:
        # One non-blocking writei from byte_offset; returns bytes accepted.
        var frame_bytes = self.channels * 2
        var frames = (len(data) - byte_offset) // frame_bytes
        if frames <= 0:
            return 0
        var wrote = Int(
            self.lib.call["snd_pcm_writei", Int](
                self.pcm, data.unsafe_ptr().unsafe_offset(byte_offset), frames
            )
        )
        if wrote > 0:
            return wrote * frame_bytes
        if wrote == _EAGAIN or wrote == 0:
            return 0
        var rc = Int(
            self.lib.call["snd_pcm_recover", c_int](
                self.pcm, c_int(wrote), c_int(1)
            )
        )
        if rc != 0:
            raise Error("playback failed: " + String(wrote))
        return 0

    def drain(mut self):
        _ = self.lib.call["snd_pcm_drain", c_int](self.pcm)

    def close(mut self):
        if self.pcm != 0:
            _ = self.lib.call["snd_pcm_close", c_int](self.pcm)
            self.pcm = 0

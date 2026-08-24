# wendyaudio tests: /proc/asound device enumeration against fixtures, wav
# header parsing on hand-built bytes, and real libasound capture on the ALSA
# "null" device (silence, no hardware needed). run_tests.sh installs
# libasound2 in the container before running this.
from std.os import getenv

from wendyaudio.devices import list_capture_devices, list_playback_devices
from wendyaudio.wav import parse_wav
from wendyaudio.alsa import AlsaPcm


def u32le(mut out: List[UInt8], v: Int):
    out.append(UInt8(v & 0xFF))
    out.append(UInt8((v >> 8) & 0xFF))
    out.append(UInt8((v >> 16) & 0xFF))
    out.append(UInt8((v >> 24) & 0xFF))


def u16le(mut out: List[UInt8], v: Int):
    out.append(UInt8(v & 0xFF))
    out.append(UInt8((v >> 8) & 0xFF))


def tag(mut out: List[UInt8], s: String):
    for b in s.as_bytes():
        out.append(b)


def build_wav(rate: Int, channels: Int, nsamples: Int) -> List[UInt8]:
    # Canonical RIFF/WAVE with an extra junk chunk before data, to prove the
    # parser walks chunks instead of assuming a 44-byte header.
    var data_size = nsamples * channels * 2
    var out = List[UInt8]()
    tag(out, "RIFF")
    u32le(out, 4 + 24 + 12 + 8 + data_size)
    tag(out, "WAVE")
    tag(out, "fmt ")
    u32le(out, 16)
    u16le(out, 1)  # PCM
    u16le(out, channels)
    u32le(out, rate)
    u32le(out, rate * channels * 2)
    u16le(out, channels * 2)
    u16le(out, 16)
    tag(out, "JUNK")
    u32le(out, 4)
    u32le(out, 0)
    tag(out, "data")
    u32le(out, data_size)
    for i in range(data_size):
        out.append(UInt8(i & 0xFF))
    return out^


def main() raises:
    var fixtures = getenv("FIXTURES")
    if fixtures == "":
        raise Error("set FIXTURES=/path/to/tests/fixtures")

    # --- /proc/asound enumeration ---
    var mics = list_capture_devices(fixtures + "/proc_asound")
    if len(mics) != 1:
        raise Error("expected 1 capture device, got " + String(len(mics)))
    if mics[0].id != "hw:1,0":
        raise Error("capture id wrong: " + mics[0].id)
    if mics[0].name != "Brio 101 - USB Audio":
        raise Error("capture name wrong: " + mics[0].name)
    print("PASS: capture enumeration -> " + mics[0].id + " (" + mics[0].name + ")")

    var speakers = list_playback_devices(fixtures + "/proc_asound")
    if len(speakers) != 1 or speakers[0].id != "hw:0,0":
        raise Error("playback enumeration wrong")
    print("PASS: playback enumeration -> " + speakers[0].id)

    var none = list_capture_devices(fixtures + "/does-not-exist")
    if len(none) != 0:
        raise Error("missing /proc/asound should yield no devices")
    print("PASS: missing proc root -> empty")

    # --- wav parsing ---
    var wav = build_wav(22050, 2, 100)
    var info = parse_wav(wav)
    if info.rate != 22050 or info.channels != 2 or info.bits != 16:
        raise Error("wav fmt fields wrong")
    # RIFF hdr 12 + fmt (8+16) + JUNK (8+4) + data tag/size 8 = offset 56.
    if info.data_size != 400 or info.data_offset != 56:
        raise Error(
            "wav data chunk wrong: off="
            + String(info.data_offset)
            + " size="
            + String(info.data_size)
        )
    print("PASS: wav chunk walk (fmt + JUNK + data)")

    # --- real libasound on the null device ---
    var cap = AlsaPcm.open_capture("null", 16000, 1)
    var total = 0
    var nonzero = 0
    for _ in range(50):
        var chunk = cap.read_available(1600)
        total += len(chunk)
        for b in chunk:
            if b != 0:
                nonzero += 1
        if total >= 6400:
            break
    cap.close()
    if total < 6400:
        raise Error("null capture produced only " + String(total) + " bytes")
    if nonzero != 0:
        raise Error("null device should capture silence")
    print("PASS: null-device capture (" + String(total) + " bytes of silence)")

    # --- open failure is a clean error ---
    var opened = False
    try:
        var bad = AlsaPcm.open_capture("hw:99,0", 16000, 1)
        opened = True
        bad.close()
    except:
        pass
    if opened:
        raise Error("open of hw:99,0 unexpectedly succeeded")
    print("PASS: bad device raises")

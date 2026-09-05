# ALSA device enumeration straight from /proc/asound — no arecord/aplay
# subprocess. Card names come from /proc/asound/cards; per-PCM names from
# /proc/asound/cardN/pcmDc/info (c = capture, p = playback).
from std.pathlib import Path


struct AudioDevice(Copyable, Movable):
    var id: String
    var name: String

    def __init__(out self, id: String, name: String):
        self.id = id
        self.name = name


def _read_text(path: String) raises -> String:
    var f = open(Path(path), "r")
    var s = f.read()
    f.close()
    return s


struct _Card(Copyable, Movable):
    var num: Int
    var name: String

    def __init__(out self, num: Int, name: String):
        self.num = num
        self.name = name


def _parse_cards(text: String) raises -> List[_Card]:
    # Card lines look like:
    #  1 [Brio101        ]: USB-Audio - Brio 101
    # (continuation lines have no "]:" separator). Pretty name is the part
    # after the last " - " on the card line.
    var out = List[_Card]()
    for line in text.split("\n"):
        var l = String(line)
        var sep = l.find("]: ")
        if sep < 0:
            continue
        var bracket = l.find("[")
        if bracket < 0:
            continue
        var num_txt = String(String(l[byte=0:bracket]).strip())
        var num = 0
        var num_ok = True
        try:
            num = Int(num_txt)
        except:
            num_ok = False
        if not num_ok:
            continue
        var rest = String(l[byte = sep + 3 :])
        var dash = rest.rfind(" - ")
        var pretty = String(rest)
        if dash >= 0:
            pretty = String(rest[byte = dash + 3 :])
        out.append(_Card(num, String(pretty.strip())))
    return out^


def _pcm_info_name(path: String) raises -> String:
    var text = _read_text(path)
    for line in text.split("\n"):
        var l = String(line)
        if l.startswith("name: "):
            return String(String(l[byte=6:]).strip())
    return String("")


def _list_devices(proc_root: String, suffix: String) raises -> List[AudioDevice]:
    var out = List[AudioDevice]()
    try:
        var cards = _parse_cards(_read_text(proc_root + "/cards"))
        for card in cards:
            for dev in range(8):
                var info_path = (
                    proc_root
                    + "/card"
                    + String(card.num)
                    + "/pcm"
                    + String(dev)
                    + suffix
                    + "/info"
                )
                try:
                    var pcm_name = _pcm_info_name(info_path)
                    var id = "hw:" + String(card.num) + "," + String(dev)
                    var name = card.name
                    if pcm_name != "" and pcm_name != card.name:
                        name = card.name + " - " + pcm_name
                    out.append(AudioDevice(id, name))
                except:
                    pass  # no such pcm on this card
    except:
        pass  # no /proc/asound at all
    return out^


def list_capture_devices(proc_root: String) raises -> List[AudioDevice]:
    return _list_devices(proc_root, "c")


def list_playback_devices(proc_root: String) raises -> List[AudioDevice]:
    return _list_devices(proc_root, "p")

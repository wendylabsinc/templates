# Deliberately tiny JSON helpers — enough for template request/response bodies.
# Mojo 1.0 has no stdlib JSON (MMF-011); swap for a real parser when one lands.


def json_escape(s: String) raises -> String:
    var out = String("")
    for cp in s.codepoints():
        var c = Int(cp.to_u32())
        if c == 34:
            out += '\\"'
        elif c == 92:
            out += "\\\\"
        elif c == 10:
            out += "\\n"
        elif c == 13:
            out += "\\r"
        elif c == 9:
            out += "\\t"
        elif c < 0x20:
            out += " "
        else:
            out += String(cp)
    return out


def json_find_string(src: String, key: String) raises -> String:
    # Finds "key" : "value" (flat objects only); returns "" if absent.
    var pat = '"' + key + '"'
    var i = src.find(pat)
    if i < 0:
        return String("")
    var r0 = String(src[byte = i + pat.byte_length() :])
    var colon = r0.find(":")
    if colon < 0:
        return String("")
    var r1 = String(r0[byte = colon + 1 :])
    var q1 = r1.find('"')
    if q1 < 0:
        return String("")
    var rest = String(r1[byte = q1 + 1 :])
    var out = String("")
    var escaped = False
    for cp in rest.codepoints():
        var c = Int(cp.to_u32())
        if escaped:
            if c == 110:
                out += "\n"
            elif c == 116:
                out += "\t"
            else:
                out += String(cp)
            escaped = False
        elif c == 92:
            escaped = True
        elif c == 34:
            return out
        else:
            out += String(cp)
    return out


def json_find_number(src: String, key: String) raises -> Float64:
    var pat = '"' + key + '"'
    var i = src.find(pat)
    if i < 0:
        return 0.0
    var r0 = String(src[byte = i + pat.byte_length() :])
    var colon = r0.find(":")
    if colon < 0:
        return 0.0
    var rest = String(String(r0[byte = colon + 1 :]).strip())
    var num = String("")
    for cp in rest.codepoints():
        var c = Int(cp.to_u32())
        if (c >= 48 and c <= 57) or c == 45 or c == 46 or c == 101 or c == 69 or c == 43:
            num += String(cp)
        else:
            break
    if num == "":
        return 0.0
    return Float64(num)

# Camera-less behavior tests (run in a container with no /dev/video*):
# enumeration must come back empty without raising, and opening a path that
# is not a V4L2 device must raise a clean Error instead of crashing.
from wendycam.camera import Camera, list_cameras


def main() raises:
    var cams = list_cameras()
    if len(cams) != 0:
        raise Error("expected no cameras in container, got " + String(len(cams)))
    print("PASS: list_cameras() empty without /dev/video*")

    var opened = False
    try:
        var cam = Camera("/dev/null", 640, 480)
        opened = True
        cam.close()
    except:
        pass
    if opened:
        raise Error("Camera('/dev/null') unexpectedly succeeded")
    print("PASS: Camera open of non-V4L2 path raises")

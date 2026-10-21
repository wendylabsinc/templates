"""Camera startup and cross-thread delivery without requiring physical hardware."""
import ast
import asyncio
import logging
from pathlib import Path
import threading
from types import SimpleNamespace


def camera_class(gst):
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'python/camera-feed/app.py').read_text().replace('{{.PORT}}', '3003'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MJPEGCamera')
    ns = dict(asyncio=asyncio, threading=threading, Gst=gst, logger=logging.getLogger('camera-test'), build_source=lambda _: 'v4l2src')
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])), '<camera-template>', 'exec'), ns)
    return ns['MJPEGCamera']

class Sink:
    def __init__(self, data=b'frame'):
        self.data = data
    def emit(self, name, *args):
        if self.data is None:
            return None
        buf = SimpleNamespace(map=lambda _: (True, SimpleNamespace(data=self.data)), unmap=lambda _: None)
        return SimpleNamespace(get_buffer=lambda: buf)

class Pipeline:
    def __init__(self, sink):
        self.sink = sink
        self.states = []
    def set_state(self, state):
        self.states.append(state)
        return 'NO_PREROLL'
    def get_by_name(self, _):
        return self.sink
    def get_bus(self):
        return SimpleNamespace(pop_filtered=lambda _: None)

def gst_stub():
    return SimpleNamespace(State=SimpleNamespace(PLAYING='PLAYING', NULL='NULL'), StateChangeReturn=SimpleNamespace(FAILURE='FAILURE'), SECOND=1, MessageType=SimpleNamespace(ERROR='ERROR'), FlowReturn=SimpleNamespace(OK='OK'), MapFlags=SimpleNamespace(READ=1))

def test_startup_rejects_no_preroll_without_frames_and_tries_raw_fallback():
    gst = gst_stub()
    pipelines = [Pipeline(Sink(None)), Pipeline(Sink(None)), Pipeline(Sink())]
    candidates = []
    def parse(source):
        candidates.append(source)
        return pipelines[len(candidates)-1]
    gst.parse_launch = parse
    cam = camera_class(gst)()
    assert cam._start_pipeline('/dev/video4') is pipelines[2]
    assert 'videoconvert' in candidates[2] and 'image/jpeg' not in candidates[2]
    assert pipelines[0].states == ['PLAYING', 'NULL']
    assert pipelines[1].states == ['PLAYING', 'NULL']
    assert pipelines[2].states == ['PLAYING']

def test_streaming_thread_wakes_asyncio_waiter_and_coalesces_backlog():
    async def trial():
        gst = gst_stub()
        cam = camera_class(gst)()
        sink = Sink()
        cam.pipeline = Pipeline(sink)
        cam._loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=2)
        cam.queues[object()] = queue
        waiter = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        def produce():
            for n in range(20):
                sink.data = str(n).encode()
                assert cam._on_new_sample(sink) == 'OK'
        worker = threading.Thread(target=produce)
        worker.start()
        worker.join()
        assert await asyncio.wait_for(waiter, .5) == b'19'
        assert queue.empty()
        # A queued callback must not leak a frame from a replaced pipeline.
        cam._on_new_sample(sink)
        cam.pipeline = None
        await asyncio.sleep(0)
        assert queue.empty()
    asyncio.run(trial(), debug=True)

def test_closed_event_loop_is_ignored():
    cam = camera_class(gst_stub())()
    cam._loop = asyncio.new_event_loop()
    cam._loop.close()
    assert cam._on_new_sample(Sink()) == 'OK'

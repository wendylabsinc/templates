"""Control diagnostics must explain failures without recording secrets or driving hardware."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT/'teleop'/'bridge'))
from control_events import ControlEvents


def test_repeated_arm_replies_are_correlated_and_not_hidden_by_same_text():
    now = [10.]
    log = ControlEvents(clock=lambda:now[0], wall_clock=lambda:100.+now[0])
    first = log.sent('arm'); now[0] += .12
    log.reply(first, 'arm', 'refused: no hand data', 'HOLDING')
    second = log.sent('arm'); now[0] += .25
    log.reply(second, 'arm', 'refused: no hand data', 'HOLDING')
    replies = [e for e in log.snapshot()['events'] if e['event']=='command.reply']
    assert len(replies)==2
    assert replies[0]['request_id']!=replies[1]['request_id']
    assert replies[1]['latency_ms']==250.
    assert replies[1]['message']=='refused: no hand data'


def test_transitions_record_outage_duration_once_and_bound_history():
    now=[10.]; log=ControlEvents(clock=lambda:now[0], capacity=3)
    log.transition('link', True, 'robot.link', state='connected')
    now[0]=11.; log.transition('link',False,'robot.link',state='disconnected')
    for _ in range(20): log.transition('link',False,'robot.link',state='disconnected')
    now[0]=16.; log.transition('link',True,'robot.link',state='connected')
    assert len(log.snapshot()['events'])==3
    assert log.snapshot()['events'][-1]['previous_duration_s']==5.
    log.emit('extra'); assert len(log.snapshot()['events'])==3


def test_disconnect_reports_unconfirmed_command_and_cannot_count_as_success():
    log=ControlEvents(); request=log.sent('arm')
    log.disconnected()
    assert not log.snapshot()['pending_commands']
    assert log.snapshot()['events'][-1]['event']=='command.unconfirmed'
    assert log.snapshot()['events'][-1]['request_id']==request


def test_event_file_flushes_and_excludes_unapproved_fields(tmp_path):
    log=ControlEvents(path=tmp_path/'control-events.jsonl')
    log.emit('startup', commit='abc', key='secret', url='ws://host/?key=secret', pose=[1,2,3])
    log.close()
    raw=(tmp_path/'control-events.jsonl').read_text()
    assert 'secret' not in raw and 'pose' not in raw
    assert json.loads(raw)['commit']=='abc'
    assert json.loads(raw)['at_utc'].endswith('Z')


def test_onboard_wire_echoes_request_id_and_fresh_remote_state():
    import asyncio, time
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    sys.path.insert(0,str(ROOT/'onboard'))
    import g1_onboard as onboard
    async def exchange():
        cmd=onboard.Commander();cmd.deadman_bit=onboard.REMOTE_BITS['R2']
        cmd.remote_t=time.time();cmd.deadman_held=False
        app=web.Application();app['cmd']=cmd;app['secret']='test'
        app.router.add_get('/ctrl',onboard.ws_handler)
        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect('/ctrl?key=test') as ws:
                await ws.send_json({'cmd':'arm','request_id':'attempt-1'})
                while True:
                    reply=await ws.receive_json(timeout=2.)
                    if reply.get('command_reply'): break
                assert reply['command_reply']['request_id']=='attempt-1'
                assert reply['command_reply']['message'].startswith('refused: take_control')
                assert reply['control_safety']['button']=='R2'
                assert reply['control_safety']['held'] is False
                assert reply['control_safety']['fresh'] is True
    asyncio.run(exchange())


def test_sink_records_each_wire_reply_and_current_state_separately():
    import asyncio
    from unittest.mock import patch
    from types import SimpleNamespace
    import aiohttp
    from sink_realg1 import RealG1Sink
    with patch('threading.Thread.start'):
        sink=RealG1Sink('ws://localhost/ctrl')
    sink.control_events=ControlEvents()
    first=sink.control_events.sent('arm')
    second=sink.control_events.sent('arm')
    async def messages():
        for rid in (first,second):
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps({
                'state':'ARMED','msg':'ARMED — teleop motion enabled',
                'command_reply':{'request_id':rid,'command':'arm','message':'ARMED — teleop motion enabled'}}))
        yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps({'state':'HOLDING',
            'control_safety':{'configured':True,'fresh':True,'held':False,'button':'R2'}}))
    asyncio.run(sink._read(messages()))
    status=sink.status()
    assert status['state']=='HOLDING'
    assert status['control_safety']['held'] is False
    replies=[e for e in status['control_debug']['events'] if e['event']=='command.reply']
    assert len(replies)==2
    assert status['control_debug']['last_command']['state']=='ARMED'
    assert status['control_debug']['events'][-1]['state']=='HOLDING'


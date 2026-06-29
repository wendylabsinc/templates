import numpy as np

from app.lib.csi.parser import parse_csi_data

# CSI_DATA CSV: type,role,mac,rssi,rate,sig_mode,mcs,bw,smoothing,not_sounding,
# aggregation,stbc,fec,sgi,noise_floor,ampdu_cnt,channel,sec_channel,timestamp,
# ant,sig_len,rx_state,len,[<int8 imag,real pairs>]
SAMPLE = (
    "CSI_DATA,0,aa:bb:cc:dd:ee:ff,-55,11,1,7,1,0,0,0,0,0,1,-90,0,6,1,"
    "12345,0,128,0,8,[3,4,0,5,-3,4,6,8]"
)


def test_parses_link_and_meta():
    f = parse_csi_data(SAMPLE)
    assert f is not None
    assert f.link_id == "aa:bb:cc:dd:ee:ff"
    assert f.rssi == -55
    assert f.channel == 6


def test_amplitudes_from_imag_real_pairs():
    f = parse_csi_data(SAMPLE)
    # pairs (imag,real): (3,4),(0,5),(-3,4),(6,8) -> 5,5,5,10
    assert np.allclose(f.amplitudes, [5, 5, 5, 10])


def test_accepts_bytes():
    f = parse_csi_data(SAMPLE.encode())
    assert f is not None and f.link_id == "aa:bb:cc:dd:ee:ff"


def test_malformed_returns_none():
    assert parse_csi_data("garbage") is None
    assert parse_csi_data("CSI_DATA,1,2") is None
    assert parse_csi_data("") is None
    assert parse_csi_data(b"") is None


def test_missing_prefix_returns_none():
    assert parse_csi_data(SAMPLE.replace("CSI_DATA", "OTHER")) is None


def test_odd_array_returns_none():
    bad = SAMPLE.replace("[3,4,0,5,-3,4,6,8]", "[3,4,0]")
    assert parse_csi_data(bad) is None


# ESP32-C6 / C5 short layout: CSI_DATA,seq,mac,rssi,rate,noise_floor,fft_gain,
# agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word,"[data]"
C6_SAMPLE = (
    'CSI_DATA,7,20:6e:f1:19:90:90,-55,11,-97,0,0,6,12345,128,1,8,0,'
    '"[3,4,0,5,-3,4,6,8]"'
)


def test_parses_c6_layout_channel_at_index_8():
    f = parse_csi_data(C6_SAMPLE)
    assert f is not None
    assert f.link_id == "20:6e:f1:19:90:90"
    assert f.rssi == -55
    assert f.channel == 6  # C6 layout: channel is field 8, not 16
    assert np.allclose(f.amplitudes, [5, 5, 5, 10])


def test_quoted_array_is_parsed():
    f = parse_csi_data(C6_SAMPLE)
    assert f is not None and f.amplitudes.shape[0] == 4


def test_csi_header_line_returns_none():
    header = "type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,channel,local_timestamp,sig_len,rx_format,len,first_word,data"
    assert parse_csi_data(header) is None


def test_empty_array_returns_none():
    bad = SAMPLE.replace("[3,4,0,5,-3,4,6,8]", "[]")
    assert parse_csi_data(bad) is None

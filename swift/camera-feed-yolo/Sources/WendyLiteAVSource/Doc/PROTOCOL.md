# AV source wire protocol

Binary TCP protocol to stream audio and video data from a device to a client.
The device is the server: it listens on port 3333 and serves one client at a
time. The client drives the exchange, asking for media a frame at a time.

The service is advertised over mDNS with service type
`_wendy_lite_av_source._tcp`.

The current protocol version is 1.0.

## Message framing

Everything is exchanged as messages: a 4 byte header followed by a payload,
every multi-byte field in network order (big-endian).

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | magic, always `0xAF` |
| 1 | 1 | message type |
| 2 | 2 | payload size |

A message is at most 1408 bytes including the header, so a payload is at most
1404 bytes.

Messages are atomic: a sender writes one message completely before starting the
next, and never interleaves two. Messages of different types may follow each
other in any order, so a handshake answer can land between two chunks of a
frame, but never inside one.

## Message types

| Type | Name | Direction |
| --- | --- | --- |
| 0 | handshake | client to device, answered by the device |
| 1 | reserved | |
| 2 | reserved | |
| 3 | request | client to device |
| 4 | data | device to client |

Types 1 and 2 are reserved for future use. A receiver ignores messages of an
unknown type, using the payload size to skip over them.

## Type 0 — handshake

Announces a protocol version. 4 byte payload.

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 2 | version, major |
| 2 | 2 | version, minor |

The device answers a handshake with the same message carrying its own version,
and never sends one on its own.

A device that receives a major version higher than its own closes the
connection, without answering. No other version check is part of the protocol:
what a client does with the version it reads back is up to the client.

The handshake is optional. A client that never sends one is served normally.

## Type 3 — request

Asks for media on one channel. 12 byte payload.

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | channel |
| 1 | 1 | number of frames |
| 2 | 2 | reserved |
| 4 | 4 | delay in microseconds since the last frame |
| 8 | 4 | request id, echoed back in every chunk of the frame |

The number of frames and the delay are not honored yet: one request yields
exactly one frame, whatever they hold.

Reserved bytes are sent as zero and ignored on receipt.

Requests are served in the order they arrive. A device that has more requests
outstanding than it can hold discards the extra ones, and the protocol has no
message to report that, so a client has to tolerate a request that is never
answered.

## Type 4 — data

Carries one chunk of a frame. 20 byte payload header followed by media data.

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 1 | channel |
| 1 | 3 | last_chunk flag (1 bit) then chunk number (23 bits) |
| 4 | 4 | frame number |
| 8 | 4 | frame timestamp in microseconds |
| 12 | 4 | host timestamp in microseconds |
| 16 | 4 | request id |
| 20 | … | media data |

A frame is one unit of media on its channel, sent as a run of chunks numbered
from 0, the last one carrying the last_chunk flag. Concatenating the media data
of the chunks in order reproduces the frame.

The chunks of a frame follow each other with no other data message in between,
and only one frame is in flight at a time. Every chunk repeats the frame number
and the id of the request that asked for it.

The maximum message size leaves 1384 bytes of media data per chunk.

Frame numbers start at 0 on a new connection and count the frames the device
has sent, whatever the channel.

## Channels

| Channel | Medium |
| --- | --- |
| 1 | video, one JPEG image per frame |

All other channel values are reserved. A device ignores a request on a channel
it does not serve.

## Timestamps

Timestamps are 32 bit microsecond counters read from a monotonic clock, so they
wrap about every 71.6 minutes.

The frame timestamp is taken on the device clock. The host timestamp is the same
instant expressed on the client clock; while no clock synchronization is in
place it simply repeats the frame timestamp.

## Connection handling

A device serves one connection at a time. Further connection attempts are
accepted only once the current one is over.

The stream is not resynchronized after a framing error, since there is no way to
find the next message boundary reliably. A receiver closes the connection when a
header does not start with `0xAF`, or when the payload size would make the
message exceed 1408 bytes.

A message of a known type carrying an unexpected payload size is ignored, like
one of an unknown type.

"""Shared Ivy source constants used by index_builder tests."""

SAMPLE_IVY_TYPES = """\
#lang ivy1.7

type cid
type quic_packet_type = {initial, handshake, zero_rtt, one_rtt}
"""

SAMPLE_IVY_MAIN = """\
#lang ivy1.7

include types

type packet
action send(p: packet)
action recv(p: packet)

export send
import recv

after send {
    require p ~= 0;
}
"""

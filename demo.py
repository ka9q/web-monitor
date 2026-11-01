"""Synthetic Opus channels, also used for end-to-end tests."""
import array
import asyncio
import fractions
import math
import struct
import sys
import time

from av import AudioFrame, CodecContext


def encoder():
    codec = CodecContext.create('libopus', 'w')
    codec.sample_rate = 48000
    codec.layout = 'mono'
    codec.format = 's16'
    codec.bit_rate = 24000
    codec.time_base = fractions.Fraction(1, 48000)
    codec.options = {'application': 'audio'}
    return codec


async def generate(hub):
    group = {'id': 'demo', 'name': 'Demo', 'payload_types': [111]}
    codecs = [encoder(), encoder()]
    for index in range(2):
        channel = hub._channel(group, '127.0.0.1', 1001 + index)
        channel.name = ['Test tone · 440 Hz', 'Test tone · 660 Hz'][index]
        channel.mode = 'Opus mono'
    samples = 0
    sequences = [0, 0]
    deadline = time.monotonic()
    while True:
        for index, codec in enumerate(codecs):
            if samples % 48000 == 0:
                # Exercise the real status decoder, including quiet channels.
                status = (b'\0\x12\x04' + struct.pack('!I', 1001 + index) +
                          b'\x21\x08' + struct.pack('!d', [146520000.0, 446000000.0][index]) + b'\0')
                hub.ingest_status(group, status, '127.0.0.1')
            # Intermittent second channel simulates squelch; first is continuous.
            active = index == 0 or (samples // 48000) % 6 < 3
            wave = array.array('h', (int(3500 * math.sin(2 * math.pi * (440 + index * 220) * (samples + n) / 48000))
                                    for n in range(960)))
            if sys.byteorder != 'little':
                wave.byteswap()
            frame = AudioFrame(format='s16', layout='mono', samples=960)
            frame.planes[0].update(wave.tobytes())
            frame.sample_rate = 48000
            frame.pts = samples
            frame.time_base = fractions.Fraction(1, 48000)
            for packet in codec.encode(frame):
                if active:
                    header = struct.pack('!BBHII', 0x80, 111, sequences[index] & 65535,
                                         packet.pts & 0xffffffff, 1001 + index)
                    hub.ingest(group, header + bytes(packet), '127.0.0.1')
                    sequences[index] += 1
        samples += 960
        deadline += .02
        await asyncio.sleep(max(0, deadline - time.monotonic()))

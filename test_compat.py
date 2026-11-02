"""Regression checks for the legacy aiortc encoded-Opus adapter."""
import fractions
import types
import unittest
from unittest.mock import patch
from av import Packet
from server import _LegacyOpusPassthrough, configure_opus_passthrough


class CompatTests(unittest.TestCase):
    def test_legacy_adapter_preserves_payload_and_timestamp(self):
        packet = Packet(b'\xf8\xff\xfe')
        packet.pts = 123456
        packet.time_base = fractions.Fraction(1, 48000)
        sender = types.SimpleNamespace()
        with patch('server.OpusEncoder', type('OldEncoder', (), {})):
            configure_opus_passthrough(sender)
        encoder = sender._RTCRtpSender__encoder
        self.assertEqual(encoder.encode(packet), ([bytes(packet)], 123456))
        packet.pts = 1000
        packet.time_base = fractions.Fraction(1, 1000)
        self.assertEqual(encoder.encode(packet)[1], 48000)

    def test_modern_sender_is_untouched(self):
        sender = types.SimpleNamespace()
        with patch('server.OpusEncoder', type('NewEncoder', (), {'pack': lambda: None})):
            configure_opus_passthrough(sender)
        self.assertFalse(hasattr(sender, '_RTCRtpSender__encoder'))

    def test_legacy_adapter_rejects_missing_timestamp(self):
        with self.assertRaises(TypeError):
            _LegacyOpusPassthrough().encode(Packet(b'\xf8\xff\xfe'))

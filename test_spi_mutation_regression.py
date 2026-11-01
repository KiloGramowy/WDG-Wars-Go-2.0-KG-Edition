"""Software regression for a backend mutating xfer2's input list in place."""

import unittest
from unittest.mock import patch

import meshtastic_rx_probe as probe
import test_meshtastic_rx_probe as contract


class MutatingSpiModel(contract.SpiModel):
    def xfer2(self, data):
        response = super().xfer2(data)
        data[:] = response
        return response


class SpiMutationRegressionTests(unittest.TestCase):
    def test_successful_transfer_records_original_tx_not_mutated_rx(self):
        backend = MutatingSpiModel()
        audit = probe.SpiAudit(backend)
        command = [0x86, 0x36, 0x59, 0xE3, 0x53]
        response = audit.xfer2(command)
        self.assertEqual(command, [0, 0, 0, 0, 0])  # model really mutated input
        self.assertEqual(response, [0, 0, 0, 0, 0])
        self.assertEqual(audit.commands[0x86], b"\x86\x36\x59\xe3\x53")
        self.assertNotIn(0, audit.commands)
        previous = dict(audit.commands)
        with patch.object(backend, "xfer2", side_effect=OSError("synthetic transfer failure")):
            with self.assertRaises(OSError):
                audit.xfer2([0x86, 0, 0, 0, 0])
        self.assertEqual(audit.commands, previous)  # never record failed writes

    def test_actual_lorarf_both_profiles_with_mutating_spi_backend(self):
        # Reuse the complete independent opcode/PHY assertions, but with a
        # stronger transport model. No physical GPIO or SPI is imported.
        case = contract.ActualLoRaRF140ContractTests(
            "test_real_adapter_sends_both_verified_rx_only_profiles")
        result = unittest.TestResult()
        with patch.object(contract, "SpiModel", MutatingSpiModel):
            case.run(result)
        if result.skipped:
            self.skipTest(result.skipped[0][1])
        details = "\n".join(text for _, text in result.failures + result.errors)
        self.assertTrue(result.wasSuccessful(), details)


if __name__ == "__main__":
    unittest.main()

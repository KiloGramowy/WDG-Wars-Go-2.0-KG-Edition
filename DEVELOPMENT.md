# Developing WDG Wars Go — KG Edition

The complete game, including KG modifications, is open source. Keep the original
LOCOSP [MIT license](LICENSE), attribution and [third-party notices](THIRD_PARTY_NOTICES.md).
Use a separate clone and synthetic player data for development; never run the
suite against a live player profile, radio or upload account.

```sh
python -B -m unittest discover -s tests -v
python -B -m unittest test_meshtastic_rx_probe test_spi_mutation_regression
```

Optional private replay/archive fixtures are supplied outside Git, or their
checks skip. The 30/100 MiB upload-capacity cases are opt-in synthetic HTTP tests;
see their test file for environment variables and memory needs. Native firmware
contracts have separate build/validation instructions. A test pass does not
establish fresh RF, uConsole, Windows or flash compatibility.

Keep runtime data, GPS traces, keys, credentials, capture files, build caches,
backup archives and private Dark3D sources out of this repository. Preserve
protocols, durable ACKs, XP rules and existing user data. Updates use the existing
verified Git/source workflow; no closed game executable or binary OTA is planned.
See [release preparation](docs/RELEASE_PREPARATION.md) and the
[0.9.21 source candidate guide](docs/ota-0.9.21/SOURCE_RELEASE.md).

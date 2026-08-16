# Contributing

Contributions are welcome, especially reproducible field evidence from real storage/controller/bridge combinations.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e . pytest
python3 -m pytest
./build.sh
./nvme-doctor --version
```

Use `python3 -m pytest` from the repository root so the intended source tree is imported consistently.

Before submitting a change:

```bash
python3 -m compileall -q src tests tools
python3 -m pytest
./build.sh
./nvme-doctor --version
```

The committed/pre-built root `nvme-doctor` must match the source version and behavior.

## Diagnostic-rule policy

Prefer evidence over guesses. A new rule should:

1. identify the exact evidence and capability it requires;
2. distinguish fact, correlation, and hypothesis;
3. use conservative severity/confidence;
4. avoid destructive remediation by default;
5. include a regression test representing the real failure pattern;
6. preserve protocol boundaries (NVMe, ATA/SATA, USB transport, host PCIe path).

Examples of unacceptable shortcuts:

- diagnosing a failed SSD solely from a generic kernel timeout;
- blaming ASPM/APST solely because power management is enabled;
- treating unavailable platform evidence as zero/clean;
- interpreting a vendor-normalized ATA SMART value as a universal percentage without validating its semantics;
- reporting an AHCI controller's PCIe generation as a SATA drive generation.

## Documentation

When behavior or output changes, update README/docs/help examples in the same release. Run the documentation-staleness tests with the normal suite.

## License

Contributions are licensed under GPL-3.0-or-later.

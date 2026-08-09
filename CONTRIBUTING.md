# Contributing

Contributions are welcome.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest
pytest
```

## Diagnostic-rule policy

NVMe Doctor should prefer evidence over guesses.

A new rule should:

1. identify the exact evidence it uses;
2. distinguish a fact from a hypothesis;
3. assign a conservative confidence level;
4. avoid destructive remediation by default;
5. include a unit test with a representative snapshot.

Please do not add rules that diagnose a failed SSD solely from a generic kernel timeout, or diagnose ASPM/APST as the cause solely because power management is enabled.

## License

By contributing, you agree that your contribution is licensed under GPL-3.0-or-later.

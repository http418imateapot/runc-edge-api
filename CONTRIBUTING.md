# Contributing

Thanks for contributing to `runc-edge-api`.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Before Opening a Pull Request

```bash
pytest tests/ -v
```

## Contribution Guidelines

- Keep changes focused and minimal.
- Update documentation when behavior or deployment steps change.
- Preserve the API key and container isolation security model.
- Do not commit secrets, credentials, or device-specific configuration.

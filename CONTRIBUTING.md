# Contributing to Sverdrup

Thank you for your interest in contributing to Sverdrup!

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/your-username/sverdrup.git`
3. Create a feature branch: `git checkout -b feature/your-feature`
4. Follow the coding standards below
5. Write tests for new functionality
6. Commit with clear messages
7. Push and open a pull request

## Coding Standards

### Python
- PEP 8 compliance; use `black` for formatting
- Type hints for all functions
- Docstrings for public functions and classes
- Tests: pytest (in `tests/` directory)

### Go
- `go fmt` and `go vet` pass without warnings
- Clear, idiomatic Go style
- Tests: Go's `testing` package
- Aim for >80% coverage on new code

### Commits
- One logical change per commit
- Clear, concise commit messages (imperative mood, no agent credit)
- Reference issues if applicable

## Architecture Notes

See [ARCHITECTURE.md](ARCHITECTURE.md) for the system design. When modifying components:

- **Services:** Keep the domain mappings in `sverdrup/services.py` synchronized with ARCHITECTURE.md
- **Processor:** Maintain backward compatibility with existing `queries` table schema
- **Dashboard:** Ensure responsive design (test on mobile)

## Testing

Run tests before submitting a PR:

```bash
# Python (offline; uses a mock NextDNS API)
pip install -r requirements-dev.txt
python3 -m pytest

# Go
cd server && go test ./...
```

## Reporting Issues

Use GitHub Issues to report bugs or suggest features. Include:
- A clear description of the problem or suggestion
- Steps to reproduce (for bugs)
- Expected vs. actual behavior
- Environment details (OS, Docker version, Go version, etc.)

## Code of Conduct

Be respectful and constructive. This project is for personal use and learning.

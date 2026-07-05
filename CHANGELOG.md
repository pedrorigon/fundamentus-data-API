# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No changes yet.

## [0.1.1] - 2026-07-05

### Changed

- Runtime API version now resolves from package metadata or `FUNDAMENTUS_API_VERSION`.
- Release workflow injects the pushed tag version into build artifacts automatically.
- README release badge no longer depends on a pre-existing GitHub Release.

## [0.1.0] - 2026-07-05

### Added

- Initial local FastAPI service for Fundamentus asset details and dividends.
- Async HTTP scraping through direct public HTML requests.
- In-memory and optional SQLite cache with request coalescing.
- Optional SQLite persistence for cache reuse across restarts.
- Prometheus-compatible metrics endpoint.
- Structured JSON error responses.
- Fixture-based parser and endpoint test suite.
- Public open-source documentation, governance files and release process documentation.

[Unreleased]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/pedrorigon/fundamentus-data-API/releases/tag/v0.1.0

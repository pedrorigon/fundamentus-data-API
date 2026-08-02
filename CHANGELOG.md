# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.10] - 2026-08-02

### Added

- Add CVM open-data statement provider.
- Expose fundamentals endpoint.
- Report share counts per reporting year.
- Expose the filing universe grouped by sector.
- Add official listed fund histories.
- Expose quality accounting fields.
- Resolve provenanced quality facts.
- Add cross-validated durability facts.
- Add public quality evidence sources.
- Resolve sector-aware asset evidence.
- Read annual statements for foreign listings.
- Derive foreign listings from their own statements.
- Read multi-year statements for foreign listings.
- Resolve balance sheets for listed real estate trusts.
- Resolve a batch of tickers in one request.

### Changed

- Reuse decoded CVM archives.
- Read foreign listings from public pages.
- Keep closed statement archives instead of refetching them.
- Cache the filings of a company apart from the archive.

### Fixed

- Preserve temporal filing accuracy.
- Preserve CVM filing metadata.
- Isolate batch source failures.
- Reconcile public valuation inputs.
- Resolve net income to the reported figure.
- Reconcile fund dividend yield.
- Distinguish current company filings.
- Ignore empty consolidated filings.
- Accept international symbol formats.
- Declare the reporting currency of foreign statements.

## [0.1.9] - 2026-07-23

### Changed

- Batch multi-year quote resolution.

## [0.1.8] - 2026-07-23

### Added

- Resolve public historical equity quotes.

## [0.1.7] - 2026-07-18

### Added

- Add public valuation resolver.

### Fixed

- Preserve requested valuation dates.

## [0.1.6] - 2026-07-16

### Added

- Add ETF and international instruments.

## [0.1.5] - 2026-07-13

### Fixed

- Complete Status Invest valuation metrics.

## [0.1.4] - 2026-07-13

No user-facing changes.

## [0.1.3] - 2026-07-13

### Added

- Add instrument classification and valuation metrics.

### Changed

- Cache upstream classification data.

## [0.1.2] - 2026-07-06

### Added

- Support real estate fund (FII) pages in details and dividends parsing.

## [0.1.1] - 2026-07-05

### Added

- Release v0.1.1 with version resolution and automated release workflow.

## [0.1.0] - 2026-07-05

### Added

- Parse Fundamentus asset pages.
- Expose local asset data endpoints.
- Update versioning and documentation for v0.1.0 release.

[0.1.10]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/pedrorigon/fundamentus-data-API/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/pedrorigon/fundamentus-data-API/releases/tag/v0.1.0

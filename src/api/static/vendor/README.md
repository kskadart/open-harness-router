# Vendored frontend dependencies

The dashboard serves every file it loads from this directory: the page
never reaches a CDN at runtime. The files below are byte-for-byte copies of
the upstream releases, fetched with `curl --noproxy '*'` on 2026-11-01.

Checked on 2026-11-01 against the OSV database: no known advisories for
`preact@11.1.1`, `htm@3.1.1` or `chart.js@4.6.1`.

To update a file, download the new release, check its licence, and replace
the sha256 below.

## preact.mjs

- Package: `preact`
- Version: `11.1.1`
- Source: https://cdn.jsdelivr.net/npm/preact@11.1.1/dist/preact.mjs
- sha256: `37918605414d7dfb7540a77fc853cdc2bd108308ca506301bdefed32418b00e5`
- Licence: MIT
- Used as: the `preact` bare specifier of the import map in
  `dashboard.html`.

## preact-hooks.mjs

- Package: `preact`
- Version: `11.1.1`
- Source: https://cdn.jsdelivr.net/npm/preact@11.1.1/hooks/dist/hooks.mjs
- sha256: `24231102dc3e509c9c657bca591524e348eb8a5272c9d40538faef9beeeb654c`
- Licence: MIT
- Used as: the `preact/hooks` bare specifier of the import map in
  `dashboard.html`. The file imports the bare specifier `preact`, which the
  import map resolves.

## htm.mjs

- Package: `htm`
- Version: `3.1.1`
- Source: https://cdn.jsdelivr.net/npm/htm@3.1.1/dist/htm.mjs
- sha256: `ab33dd3f38059b9be4d5f5350128eefb2356639c4e0bbe9d9e8b3ba75847e9e4`
- Licence: Apache-2.0
- Used as: the `htm` bare specifier of the import map in `dashboard.html`.

## chart.umd.min.js

- Package: `chart.js`
- Version: `4.6.1`
- Source: https://cdn.jsdelivr.net/npm/chart.js@4.6.1/dist/chart.umd.min.js
- sha256: `87364cc2b2a76a6684929e7d6b80b18a897794db38400caff1fcf1791c6925d2`
- Licence: MIT
- Used as: a classic script; it defines `window.Chart`.

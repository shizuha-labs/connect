# Dependency review — 2026-10-03

The frontend production dependency audit passes with no findings after updating
Axios, DOMPurify and the compatible parser patches in the lockfile. Clean npm
installation, lint, the complete frontend unit suite and the production build
passed. This does **not** mean the development dependency audit is clean.

The full audit reports five high-severity entries rooted in
[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm):
`braces <=3.0.3`, reached through `micromatch`, `fast-glob`, `chokidar` and
`tailwindcss`. The latest Tailwind 3 release available at review was 3.4.19;
no patched compatible `braces` release was available. No advisory was suppressed
and no incompatible transitive override was forced.

This chain runs during CSS compilation and development file watching, and is
absent from the production dependency graph. Tailwind scans the versioned
application sources and vendored UI package selected in `tailwind.config.js`.
It does not compile end-user post, message, search or profile content. A deeply
nested pattern supplied to the build tool can exhaust its stack. Treat source
changes and build configuration as trusted code, review contributions before
privileged execution, and keep development servers private. The residual
build-time finding remains open and must be reassessed on dependency updates.

Tailwind 4 removes this dependency chain, but its migration changes utility
semantics, preflight defaults, configuration loading and supported browsers.
It requires a dedicated light/dark, desktop/mobile visual compatibility pass,
including shared UI components; a package override alone is not a valid fix.

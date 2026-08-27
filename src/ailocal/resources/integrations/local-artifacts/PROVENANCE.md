# Provenance

This component was developed as a standalone repository and folded into ailocal
when the local artifact capability became part of claude-local's supported
experience rather than an optional add-on. It keeps its own directory and its
own tests; ailocal owns only its installation, configuration and health.

Migrated from `local-artifacts` at commit `f112028`
(2026-08-27). The standalone `install.sh` /
`uninstall.sh` were dropped in the move: ailocal now owns the lifecycle, and two
installers for one capability is exactly the drift this merge removes.

History up to that commit lives in the original checkout at
`DevelopSolutions/local-artifacts`, which is superseded and should not be
installed separately.

Upstream notices for the vendored assets are unchanged: see NOTICE,
NOTICE.upstream, LICENSE.upstream and licenses/.

One upstream arrived after the migration: `themes/carbon.tokens.json` is
GENERATED from `@carbon/themes` (Apache-2.0) at a pinned version by
`tools/update_carbon_tokens.py`, and is a derived work of Carbon's DTCG token
source rather than vendored code. Attribution and the list of modifications are
in NOTICE; the licence text is `licenses/carbon-LICENSE.txt`; the architecture
and update procedure are in DESIGN-SYSTEM.md.

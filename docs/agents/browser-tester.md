# AVA browser tester

Executes Astra-designed browser cases and bounded authorized-origin crawls with Playwright, retaining reproducible evidence.

## Responsibility

Run browser tests using the repository's pinned Playwright CLI so Claude and Codex
share one executable workflow. Interpret results honestly; do not substitute
manual screenshots for executable checks.

## Procedure

Read the case catalog and verify the target is the intended app or labelled
fixture. Use an explicit origin, bounded pages/depth/timeouts, and isolated output
directory/port per worktree. Do not crawl arbitrary external links, authenticated
accounts, or production targets without task authorization. Follow crawler limits;
do not bypass errors by globally disabling network restrictions.

Inspect console/page errors, response failures, navigation, actual DOM states and
relevant accessibility/viewport behavior. Use stable user-visible locators. Keep
test data fictional; block third-party requests by default and avoid destructive
form submissions in exploratory crawls.

Capture failing case IDs, reproduction, expected/actual, traces/screenshots/report
paths and relevant environment. A network-block test should show the blocked
request did not reach the destination. Exit codes and test reports must agree.
Do not mask flaky failures with large retries or arbitrary sleeps.

## Deliverable

Playwright tests/results, bounded crawl report, defect handoff and scope statement.
When no product UI exists, run infrastructure fixtures and mark product tests
pending. Never claim that all web behavior, security or performance is proven by
a successful smoke test.

# Implementation Plan: GitHub Actions CI/CD

## 1. Goal

Automatically test pushes to every branch and pull requests targeting any branch. Only a tested
`main` commit may automatically deploy to the existing TradingSupport installation. Keep local/manual deployment available
through the same deployment tool and safety checks.

## 2. Non-Goals

- No application, stock-selection, provider, database-schema or scheduling changes.
- No automatic database migration, universe approval, first stock sync or provider credential replacement.
- No new production service, self-hosted runner, image registry, polling loop or deployment timer.
- No deployment from pull requests, other branches or failed/skipped/cancelled tests.

## 3. Constraints

- **Owner decision (2026-11-27):** this is the sole additional active plan allowed by `AGENTS.md`.
  It coexists with [the stock plan](PLAN_stock_db.md); no phases are migrated or archived.
  This plan governs CI/CD, while that plan governs application/data work and its release approval.
  Its older blanket prohibition on automatic code deployment is superseded only for this workflow.
- Maximum three phases. Execute/review one requested phase at a time; no commit or push is authorized.
- **Latest owner clarification (2026-11-27):** tests run automatically on pushes to every
  branch (including names containing `/`) and PRs targeting any branch. This supersedes the
  earlier `main`-only test triggers. Automatic deployment remains restricted to `main`.
  Tests run independently of whether automatic deployment is enabled. Failure is never allowed:
  use GitHub Actions `continue-on-error: false` on required test jobs and test steps (the
  GitHub equivalent of `allow_failure: false`). Any failed, skipped, cancelled or missing
  required check blocks deployment of that candidate. No force/skip-tests escape hatch.
  Local/manual production updates remain allowed with mandatory tests automatically enforced.
- Reuse `deploy/update.sh`, `deploy/manage.py`, release allowlisting, the server deployment lock,
  refresh drain, snapshots, compatibility checks, health checks and rollback. Never bypass them.
- Keep `bash deploy/update.sh`, its `--dry-run` and recovery usable without GitHub availability.
  Preserve the local `docker compose up -d --build` development deployment as well.
- Use ephemeral GitHub-hosted Linux runners. Tests use dummy keys and disposable databases;
  production provider keys and the application environment stay on the server.
- GitHub deployment starts disabled. Enable it only in Phase 3 after the concrete release meets
  the stock plan's release/migration approvals; passing CI alone grants no data-change authority.
  If the candidate includes unreleased stock-plan behavior, activation waits for that plan's
  Phase 5 approval and reviewed release. CI and isolated deployment rehearsal can proceed earlier.

## 4. Current Understanding

Baseline inspected locally on 2026-11-27, before Phase 1 implementation:

- Remote: `https://github.com/Afanyi/HighLowDb.git`; current local branch: `dev`.
  There is no `.github/` directory or existing Actions workflow.
- The Dockerfile uses Python 3.13.15, pinned application dependencies, UID 10001 and one
  Gunicorn worker with four threads. Production limits the app to 512 MiB and publishes no port.
- `deploy/update.sh` delegates to `deploy/manage.py update`. It packages allowlisted source,
  tests it offline locally, verifies the SSH destination, uploads it and builds/tests on the
  target architecture. The stock plan records an ARM64 production host; do not ship an x86 image.
- `--dry-run` needs a previously built local image named `allstocksdatabase-app:latest` by
  default, uses dummy keys and does not read application/SSH settings or contact the server.
- Production updates preserve the server `.env` unless explicitly passed `--env`. CI must
  invoke ordinary update without `--env`, never first-install setup or automatic recovery.
- Strict SSH host-key checking and the `ubuntu`/`tradingsupport-vnic` destination checks already
  exist. The server lock serializes local and CI updates; refresh drain protects active jobs.
- Existing verification entry points are `python -m unittest -q`,
  `python3 -m unittest tests.check_deploy -q`, `python3 tests/check_deploy_docker.py`,
  `python3 tests/check_production.py` and `bash -n deploy/setup.sh deploy/update.sh`.
  The three `check_*` modules are not all covered by default unittest discovery.
  **Superseded by Phase 1 rework:** the combined `bash -n` invocation checks only its
  first script. The current runner checks each script separately and also runs the
  host-only CI regressions with `python3 -m unittest tests.check_ci -q`.
- Docker verification needs helper images: `caddy:2.10.2-alpine`, `curlimages/curl:8.12.1`,
  `alpine:3.22`, and the drill's `docker:29-cli`/Python controller build. Some invocations use
  `--pull=never`; provision these images before isolated tests on a fresh runner.
- The stock plan still has unfinished work and explicitly requires approval before live data
  changes. Existing schema/count checks may refuse a candidate; refusal must remain a failure.
- README, rollout docs and deployment messages still describe exclusively manual code updates.

**Remaining unknowns after Phase 1 inspection:** branch rules and Actions enablement settings,
supported environment protections, hosted runner Docker/Compose versions, CI-to-server SSH
reachability, trusted SSH host keys and a dedicated automation credential. Remote branches,
repository visibility/default branch and local Docker/Compose versions are recorded below.
Do not inspect or print private key/application environment contents for this inventory.

## 5. Areas to Inspect

New `.github/workflows/ci-cd.yml`; existing `Dockerfile`, Compose files, `requirements.txt`,
`deploy/update.sh`, `deploy/manage.py`, `deploy/release.py`, `deploy/data.py`,
`deploy/server.env.example`, verification modules, `README.md`, `deploy/ROLLOUT.md` and
`docs/deployment.md`. Change scripts/tests only where runner integration requires it.

## 6. Simplest Implementation Strategy

One workflow with unprivileged verification and a dependent deployment job:

1. Run verification on `push` with `branches: ['**']` and `pull_request` without a target-branch
   filter. This covers all branch names, including `feature/example`, and excludes tag pushes.
   A push to `main` includes merge commits and any permitted direct push; use branch rules
   to require PRs if desired. No path or branch exclusion may skip branch verification.
   PRs and pushes to branches other than `main` never deploy, even when all tests pass.
2. Build the existing app image and run all listed verification entry points, including the
   release dry run, using only dummy data. Dependency downloads/builds may use the network;
   application tests run offline and Docker drills use disposable fixture resources.
3. On `push` to `refs/heads/main` only, deploy after every required verification job succeeds
   and the proposed repository variable `ENABLE_AUTO_DEPLOY` is exactly `true`.
   The switch controls deployment only, never automatic testing. Declare every required test
   job in deployment's `needs` and require each result to equal `success`. Do not use
   `always()`/`failure()` to bypass this gate or suppress a test command's nonzero exit.
   Both jobs check out the immutable event SHA, with persisted Git credentials disabled.
   The deploy runner builds the local dependency image and the update tool rechecks the
   packaged source; the production host builds/tests its own architecture's candidate.
4. Provide a dedicated SSH key only to the deployment job, using a production environment
   where supported. Proposed settings: secret `DEPLOY_SSH_KEY`, variable `DEPLOY_HOST`
   and a verified `DEPLOY_KNOWN_HOSTS` value. Materialize private temporary SSH settings/key
   outside the checkout; run `bash deploy/update.sh --server-config ...` and clean up credentials.
   This Phase 2 design supersedes writing the CI settings to ignored `deploy/server.env`,
   preserving that file for local use and keeping the verified checkout clean.
   Do not obtain host trust from an unverified in-job `ssh-keyscan`.
5. Serialize production jobs with one concurrency group and `cancel-in-progress: false`.
   Before SSH, reject a run whose event SHA is no longer the remote `main` head, including
   reruns of old successful commits. Do not assume the queue preserves every merge/order;
   a superseded pending revision may be skipped. Keep the server lock for local/CI races.
6. Report source SHA, content release ID and success/failure in the run summary without private
   artifacts. Disabling `ENABLE_AUTO_DEPLOY` stops future automatic updates; local update and
   explicit recovery remain available. Never automatically cancel an ongoing cutover.

GitHub references checked during planning: [workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
[concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
and [secure use](https://docs.github.com/en/actions/reference/security/secure-use).
The owner clarification also follows [workflow syntax for failure handling and dependencies](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax).

## 7. Security Considerations

- Default token permission is `contents: read`; no repository write, package or identity-token
  permission is needed. Pin used actions to reviewed full commit SHAs at implementation time.
- Never use `pull_request_target` to execute PR code or pass deployment secrets to PR tests.
  Use environment variables/arguments rather than interpolating untrusted event text in shell.
- Keep SSH host verification, destination checks, private file permissions and secret-free logs.
  Upload neither application environments, keys, databases nor backups as Actions artifacts.
- Existing `ubuntu` sudo/Docker access grants host administration. A dedicated CI credential is
  sensitive even though the deployer targets only this app; do not describe it as a sandbox.
  Do not broaden sudo authority, weaken SSH restrictions or relax firewall rules to make CI pass. If current network policy
  prevents hosted-runner access, record the blocker before choosing another access design.
- Do not mount the Docker socket in the app. Administrative test-controller access is confined
  to the ephemeral test runner, with dummy data and no production credentials.
- Preserve login, CSRF, private upstream, stock manual-only behavior, crypto scheduling and
  protection of the shared TradingSupport services. No global Docker cleanup.

## 8. Implementation Phases

### Phase 1: Run existing verification in GitHub Actions

**Status:** Done — Passed Review
**Depends on:** None

#### Goal

Pushes to every branch and PRs targeting any branch receive reproducible checks without deployment access.

#### Scope

Add the workflow's verification jobs and document their local equivalents. Make only the
smallest necessary runner-compatibility adjustments to existing verification scripts.
Use `tests/check_ci.sh` as the shared command sequence for the workflow and local execution,
reusing the existing checks rather than maintaining two separate sequences.

#### Do Not Change

Production settings, deployment behavior, application logic, real databases or provider access.

#### Reuse First

The existing Dockerfile, unittest suite, three host-only verification modules and release dry run.

#### Steps

1. Confirm remote branch/Actions prerequisites and runner Docker/Compose compatibility.
2. Add all-branch push and unrestricted PR triggers, read-only permissions, pinned actions
   and immutable checkouts.
   Set `continue-on-error: false` explicitly on required test jobs and test steps; do not
   condition test execution on the deployment switch or credential availability.
3. Build the app image, provision helper images, then run the existing checks with dummy
   keys, temporary databases and isolated test resources. Include the production-style smoke
   test and the real Docker deployment drill; ordinary unittests alone are insufficient.
4. Validate workflow structure and exercise the command sequence locally. Verify failure
   propagation using disposable fixtures; record GitHub results when an authorized push exists.
5. Review isolation, cleanup and logs; record commands and any external verification still pending.

#### Acceptance Criteria

- Both configured events run application tests, host deployment tests, Docker deployment drill,
  isolated HTTPS/auth/CSRF smoke checks, shell syntax checks and packaged-source dry run.
- Tests start automatically for both events even when deployment is disabled or unconfigured.
- Branch coverage includes `main`, `dev` and a nested name such as `feature/example`;
  PRs targeting non-`main` branches also run checks. Tag pushes do not trigger this workflow.
- Every required test job and test step has `continue-on-error: false`. A failed command
  fails verification; no allowed-failure job, swallowed exit status or passing aggregate
  result may conceal it. A skipped/cancelled/missing required check cannot count as passing.
- Verification needs no production secrets, `.env`, `deploy/server.env`, live volume or SSH.
- A fresh runner provisions needed images instead of depending on a developer's image cache.
- The same verification procedure is documented and works locally; helper cleanup is app-scoped.

#### Verification

Run the inspected commands above, `bash deploy/update.sh --dry-run` and `git diff --check`.
Validate YAML and job/event configuration using an available workflow validator; determine
the exact validator command during implementation. Record any unrun hosted checks explicitly.

#### Security Check

Inspect action pins, permissions, events, dummy configuration, offline app-test execution,
Docker mounts and logs. Confirm no verification job receives deployment credentials.

#### Execution Evidence (2026-11-27)

- Dependencies: none. Read both active plans and the complete phase before implementation;
  the CI/CD exception applies and stock-plan application/data work was not changed.
- Added `.github/workflows/ci-cd.yml`: all-branch pushes (`**`, including nested names),
  unrestricted PR targets, no tag trigger, `contents: read`, a 45-minute verification job,
  explicit `continue-on-error: false` on the job and both steps. There is no deployment job,
  environment reference, deployment-secret access or deployment-switch condition.
- Checkout is pinned to official `actions/checkout` v7.0.1 commit
  `3d3c42e5aac5ba805825da76410c181273ba90b1`; the event SHA is explicit and persisted
  credentials are disabled. Verified the release/commit and action metadata against the
  official repository; its Node 24 action runs on an ephemeral `ubuntu-24.04` runner.
- Added `tests/check_ci.sh` as the shared local/CI sequence and documented it in README.
  It prepares all five existing helper images and builds the existing Dockerfile before
  running checks. No production runtime code, dependencies or release allowlist changed.
- Read-only public repository inventory confirmed `Afanyi/HighLowDb` is public, enabled,
  not archived, and defaults to `main`. `git ls-remote --heads origin` confirmed `main` and
  `dev`. Local verification used Docker 29.6.1 and Compose 5.3.1.
- **Full sequence passed:** `bash tests/check_ci.sh`, exit 0. This included **258 application
  unittests** in a read-only, network-disabled, memory-limited app container with a temporary
  `/tmp` filesystem, dummy keys and no host mounts; **22 host deployment tests**; the real
  isolated Docker setup/update, corrupt-stage refusal, refresh-drain deferral, unhealthy-cutover
  rollback and explicit-recovery drill; and verification that an unrelated sentinel container
  kept its identity and remained running.
- HTTPS smoke checks passed with an isolated test CA: missing/wrong login rejected, both
  datasets and health accessible with fixture credentials, missing-CSRF/cross-site form
  submissions rejected, valid fixture forms accepted. No live hostname route was changed.
- The packaged-source dry run passed its offline tests and produced release
  `c27b0c3b5d455e2d` in private `/tmp/allstocks-release-yxb7xrr1/review`, with no SSH,
  private application/SSH configuration read/upload or deployment.
- **Failure propagation passed:** disposable fake Docker/Python commands injected exit 23
  separately at image pull, build, app tests, host tests, Docker drill, HTTPS smoke and release
  dry run. Each stopped the script immediately with exit 23; an all-success fixture reached
  every required check. The fixtures used no real Docker/SSH or production credentials.
- **Configuration validation passed:** official checksum-verified actionlint 1.7.12
  (`/tmp/allstocks-actionlint-1.7.12 .github/workflows/ci-cd.yml`), Bash syntax checks,
  `git diff --check` and direct YAML assertions covering `main`, `dev`, nested branch names,
  unrestricted PR targets, tag exclusion, immutable checkout, no secrets/switch dependence
  and mandatory error propagation. Static event checks are not hosted event-run evidence.
- **Security inspection:** only dummy values are supplied to tests; no application environment,
  key, database or backup was added to Git or uploaded as an artifact. Application tests have
  no host mounts/network; administrative Docker test-controller access stays in the local
  fixture drill. Existing cleanup remains limited to its generated fixture resources.
  No credible security issue was identified in this change.
- **Not run:** real GitHub push/PR jobs, including Actions-side image provisioning and runner
  compatibility. No commit/push was authorized, and no authenticated GitHub administration
  tool was available to inspect branch rules/Actions enablement. The workflow is locally
  validated; hosted execution remains pending an authorized push. No production connection,
  deployment, secret change or later-phase implementation occurred. Phase 1 awaits review;
  it is not `Done — Passed Review`.

#### Review Result (2026-11-27)

**Review Failed.** Reviewed the complete CI/CD plan and Phase 1 criteria, current workflow,
shared runner, README diff and existing checks invoked by the runner. The stock-plan scope
exception and data-release boundaries remain consistent. No credible security issue was found
in the Phase 1 change. This is a same-session review, not an independent external review.

**Blocking finding (P2):** `tests/check_ci.sh:9` invokes
`bash -n tests/check_ci.sh deploy/setup.sh deploy/update.sh`. Bash parses only the first
script; the other filenames become its positional arguments. Consequently the promised
deployment-script syntax checks are not performed by this command, and a syntax failure
in `deploy/setup.sh` can leave verification green.

- Reproduced with the actual runner copied into a disposable fixture: `deploy/setup.sh`
  contained an invalid `if`, `deploy/update.sh` was a valid no-op, and fake Docker/Python
  tools succeeded. A direct syntax check of the broken setup returned **2**, while the
  complete verification runner incorrectly returned **0**. No repository script was altered
  and no Docker, SSH or production resource was used for the reproduction.
- Required rework: call `bash -n` separately for every intended shell script (or loop over
  them) and demonstrate that an invalid setup/update script stops the runner before Docker
  preparation. Preserve the existing failure propagation for all other required checks.

| Acceptance criterion | Review result |
| --- | --- |
| Both events run every required check | **Failed:** deployment shell-script syntax gate is incomplete |
| Automatic tests independent of deployment enablement/configuration | Configuration verified: no switch, secret or job condition |
| All branch names/PR targets covered; tags excluded | Configuration verified: `push.branches: ['**']`, unrestricted `pull_request` |
| No allowed failure or concealed failed required check | **Failed:** the broken-setup fixture is not checked and returns success |
| No production credentials, environment, volume or SSH needed | Verified by source inspection and prior isolated execution evidence |
| Fresh-runner images prepared | Verified explicit pull/build sequence; hosted provisioning remains pending |
| Same documented local/CI procedure; scoped cleanup | Verified shared runner invocation and existing namespaced fixture cleanup |

Fresh actionlint 1.7.12 validation, individual `bash -n` checks of all three current shell
scripts, and `git diff --check` passed. The current scripts are syntactically valid; the
failure concerns missing enforcement against a future broken deployment script. The prior
258 application tests, 22 deployment tests, Docker rollback/recovery, HTTPS/CSRF and release
dry-run evidence was inspected, not rerun: the focused reproduction already establishes
the blocking gate defect and no runtime code changed during review.

Optional findings: none. Actual GitHub push/PR runs remain unverified pending an authorized
push, as already recorded; no commit/push or production change was made. Only this review
record/status was added. Phase 2 remains `Not Started` with its dependency unsatisfied.

#### Rework Evidence (2026-11-27)

- Owner explicitly requested completing and re-reviewing Phase 1 until it passes. Resumed
  from `Review Failed`, set `In Progress` before edits and stayed within this phase.
- `tests/check_ci.sh` now invokes `bash -n` separately for itself, `deploy/setup.sh` and
  `deploy/update.sh`, before any tests or Docker preparation. It also runs the new host-only
  `tests.check_ci` regression suite. No production deployment script or runtime code changed.
- Added three stdlib regression tests in `tests/check_ci.py`: malformed syntax in each of
  the three shell files must return 2 before any fake tool starts; ten separately injected
  failures (CI tests, Docker version/Compose/pull/build/run, host tests, Docker drill, HTTPS
  smoke, release dry run) must immediately propagate exit 23; the successful fixture must
  reach every required suite and the final dry run. These use disposable source and fake
  commands, with no Docker/SSH access or real credentials.
- Verified the regression's negative control: restoring the original combined `bash -n`
  invocation only in its temporary fixture produces two expected test failures, one for
  each unverified deployment script. The corrected runner passes all three regression tests.
- README documents the added regression checks and corrects the host Python minimum to
  3.11, matching `unittest.TestCase.enterContext` already used by the host tests.
- **Complete corrected sequence passed:** `bash tests/check_ci.sh`, exit 0: three CI runner
  tests, 258 offline application tests, 22 host deployment tests, the full isolated Docker
  setup/update, corruption/refresher refusal, rollback/recovery and unchanged-sentinel drill,
  plus HTTPS/login/CSRF smoke checks. The final packaged-source dry run passed and produced
  release `1158ea65432c48dc` at private `/tmp/allstocks-release-09t3z_hh/review`.
- Fresh actionlint 1.7.12, individual Bash syntax checks, YAML event/permission/checkout
  assertions, Python AST parsing and whitespace checks passed. The required checks still
  run independently of deployment enablement and have no allowed-failure path.
- Security inspection found no credible issue: CI has read-only repository permissions,
  pinned checkout without persisted credentials, no deployment environment/secrets, offline
  application tests and scoped dummy-data drills. No production connection or change occurred.
- Actual hosted push/PR runs remain pending an authorized push, as specified by the phase's
  verification procedure; this rework does not claim hosted runner compatibility as tested.
  Phase 1 is ready for the requested re-review. Phases 2 and 3 remain `Not Started`.

#### Re-review Result (2026-11-27)

**Done — Passed Review.** Re-read the final workflow, shared runner, regression tests,
README changes and all Phase 1 criteria after the successful corrected full sequence.
The previous blocking finding is resolved: every shell script is now checked separately,
and malformed setup/update scripts fail before any test/Docker preparation starts. The
negative-control run confirms the regression detects the original bug. No blocking or
optional finding remains, and no credible security issue was found in this scoped change.

| Acceptance criterion | Final review evidence | Result |
| --- | --- | --- |
| Both configured events invoke every required check | One unconditional verification job calls the same runner; all three syntax checks, CI/app/deployment tests, Docker drill, HTTPS/CSRF smoke and archive dry run passed | Passed: implementation and local execution |
| Tests independent of deployment configuration | Workflow has no enablement condition, deployment environment or secret reference | Passed: configuration |
| All branches/PR targets covered; tags excluded | Actionlint and YAML assertions verify `push.branches: ['**']`, nested branch coverage and unrestricted PR targets | Passed: configuration |
| Required failures cannot become success | Job/steps have `continue-on-error: false`; three regression tests cover all shell files, ten command failures, all-success and the old-code negative control | Passed |
| No production credentials/environment/volume/SSH required | Full local sequence uses dummy values and temporary resources; app tests have no network or host mounts; dry run reports no configuration read/upload or SSH | Passed |
| Explicit preparation for a runner without cached images | Inspected all invoked image references against the five-image pull list and existing Docker build; pull/build sequence ran successfully | Passed: implementation/local preparation |
| Shared local/CI procedure and scoped cleanup | README and workflow both call `bash tests/check_ci.sh`; Docker drills cleaned their temporary fixture resources and retained the unrelated sentinel | Passed |

Verification is complete for the Phase 1 implementation/local scope. Hosted GitHub push/PR
execution and runner compatibility remain explicitly **not verified**, pending an authorized
push; Phase 1's procedure requires recording this limit, and hosted rollout evidence remains
in Phase 3. This is the requested same-session re-review, not an independent external review.
No check was waived or represented as run on GitHub. No commit, push, production deployment,
credential change or later-phase work occurred. Phase 2 is now eligible by dependency but
remains `Not Started`; the plan stays active because Phases 2 and 3 are unfinished.

### Phase 2: Add guarded deployment through the existing updater

**Status:** Done — Passed Review
**Depends on:** Phase 1

#### Goal

Add the automatic deployment path with its enablement switch off and retain local deployment.

#### Scope

The workflow deployment job, minimal deployment integration/messages and meaningful additions
to `tests/check_deploy.py` for any changed client behavior. Document proposed GitHub settings.
The client-only `deploy/ci.py` integrates the workflow with `deploy/update.sh`; an optional
private SSH settings path preserves existing local configuration without creating another
server-side deployer.

#### Do Not Change

Server credentials/environment, first installation, proxy rules, migrations, refresh behavior
or existing deployment gates. Do not enable or perform a production deployment in this phase.

#### Reuse First

`deploy/update.sh`, literal `server.env` parsing, strict SSH, release manifests and server rollback.

#### Steps

1. Add the deployment job with explicit successful-verification dependencies, `push`/`main`
   conditions and the disabled-by-default `ENABLE_AUTO_DEPLOY` condition.
   List every required verification job in `needs` and require all their results to be
   `success`. Keep mandatory automatic predeployment tests on the local/manual update path;
   add no force/skip-tests option or alternate route around a failed candidate's checks.
2. Add immutable checkout, local image preparation and private SSH configuration. Invoke the
   ordinary updater without `--env`; remove job credentials even when a step fails.
3. Add production concurrency without cancelling active jobs, and the stale-main guard before
   SSH. Keep lock contention/drain timeout nonzero with the current release left running.
4. Add source/release outcome reporting and replace misleading manual-only messages where needed.
5. Rehearse with fake SSH/Docker and existing isolated deployment drills, checking failure,
   stale revision, missing configuration, concurrency, rollback and manual entry points.

#### Acceptance Criteria

- Deployment requires a successful verification of its exact event SHA; failed/skipped/cancelled
  checks, PRs, other branches and absent/false enablement never reach SSH.
- Demonstrate separately that failure of each required suite blocks deployment before SSH,
  and that skipped, cancelled or missing required jobs also block it. The all-success case
  is eligible only when the existing branch, enablement and release gates also pass.
- A successful push check on `dev` or `feature/example`, or a successful PR check targeting
  any branch, never starts deployment or receives deployment credentials.
- Local/manual production updates automatically run their mandatory tests and stop before
  SSH/cutover if any fails; retaining local deployment introduces no test bypass.
- An old rerun is rejected when its SHA differs from current `main`; an active update is not
  cancelled just because another commit arrives. Local and CI clients share the server lock.
- Missing key/host trust, host mismatch or deployment failure produces a failed run.
- CI invokes the existing update path, retains the server environment and all deployment gates,
  and cannot treat a migration refusal or drain timeout as deployment success.
- `bash deploy/update.sh`, `--dry-run`, documented recovery and local Compose usage remain
  available without any GitHub token, Actions variable or Actions-only prerequisite.
- Logs/summaries identify source SHA and release outcome without exposing secrets or backups.

#### Verification

Exercise workflow event/condition cases with a validator and disposable/fake transport,
including one failing suite at a time, skipped/cancelled/missing results, all-success,
`main`/`dev`/nested branch pushes, PRs to different target branches and excluded tag pushes;
run deployment regression tests, the Docker rollback/recovery drill and the local dry run.
Use dummy credential material for cleanup/permission assertions, never production secrets.
Record the exact evidence for each criterion; do not claim a fake transport proves live SSH.

#### Security Check

Review secret scoping, temporary file permissions/cleanup, strict host trust, source identity,
shell quoting and all failure paths. Confirm no CI change weakens server checks or sudo policy.

#### Execution Evidence (2026-11-27)

- Dependency confirmed: CI/CD Phase 1 is `Done — Passed Review`. Read the full active CI/CD
  plan, the phase and its acceptance criteria; stock-plan application/data approval boundaries
  are preserved. Set Phase 2 `In Progress` before implementation. No Phase 3 work was started.
- Added the workflow's `deploy` job with `needs: [verify]` and an explicit condition requiring
  a `push`, `refs/heads/main`, repository `ENABLE_AUTO_DEPLOY == 'true'` and verification result
  `success`. Both jobs check out the immutable event SHA with credentials persistence disabled.
  Verification remains independent of deployment settings; only the production deployment
  step receives the dedicated SSH secret and built-in read-only GitHub token.
- Production jobs share `allstocks-production`, with `cancel-in-progress: false`, while
  retaining the server's existing local/CI deployment lock. No switch value, GitHub environment,
  branch rule, production secret or server access policy was created or changed in this phase.
  Missing/false enablement continues to disable automatic deployment by default.
- Added `deploy/ci.py`: independently validates event/ref/repository/enablement/result, requires
  a clean checkout matching the event SHA, checks current `main` through the fixed GitHub REST
  endpoint, validates credentials, builds the existing local image and rechecks `main` before
  invoking the ordinary updater. Stale queued runs, old reruns, unknown source and GitHub API
  failure stop without SSH. No personal access token or new dependency is required.
- The client creates a 0700 temporary directory and 0600 key, known-host and settings files.
  Native `ssh-keygen` validates an unencrypted private key and matching host entry; hashed host
  entries are supported. Temporary credentials are removed on successful/failed execution and
  are outside the source/build context. Private-key values, host-key content and GitHub tokens
  are removed from child-process environments. Raw tool output/exception text is not published;
  logs and the Actions summary contain only validated commit/release identifiers and outcome/stage.
- The updater accepts optional `--server-config`; default `deploy/server.env` behavior remains.
  The new optional `AS_KNOWN_HOSTS` requires a private file and `AS_KEY`; explicit CI trust uses
  `-F /dev/null`, its own known-host file, no global known-host file, `IdentitiesOnly=yes` and
  `IdentityAgent=none`, retaining strict host checking and destination identity checks.
  CI invokes update without `--env`, setup, recovery or skip/force flags. Existing source and
  server-candidate tests, drain timeout, locks, backup, schema/count checks, health and rollback
  remain mandatory and unchanged. Local updates do not require a GitHub token or switch.
- Added the client helper to the existing release allowlist so packaged deployment tooling and
  host-side regression tests include it. No runtime Dockerfile, application, provider, database,
  schedule or sudo/SSH policy changed. Updated deployment messages, README, rollout/configuration
  notes and the SSH settings example, including the four GitHub settings and failure handling.
- **Focused checks passed:** 39 tests combined (`tests.check_ci` = 3, `tests.check_deploy` = 36).
  New tests cover ineligible PR/other-branch/tag/disabled/failed/skipped/cancelled/missing results;
  stale or dirty/wrong checkouts; `main` advancing during build; missing settings/API failure;
  invalid private keys; mismatched/fingerprint/hashed host entries; failed builds/updates;
  missing release confirmation; safe reports; key file permissions/cleanup and explicit trust
  without changing local SSH settings. Existing client tests still prove failed mandatory
  local tests and destination identity mismatch stop before upload/cutover.
- **Workflow validation passed:** actionlint 1.7.12 and evaluation of the actual job condition
  across 180 event/ref/enablement/verification-result combinations. Additional YAML assertions
  confirm `needs`, both immutable checkouts, isolated verification, explicit failure handling,
  production environment and non-cancelling concurrency. These are configuration/fixture checks,
  not a claim of real GitHub scheduler or deployment execution.
- **Complete sequence passed:** `bash tests/check_ci.sh`, exit 0: 3 CI regression tests,
  258 offline application tests, 36 host deployment tests, the isolated real Docker setup/update,
  corrupt-stage/active-job refusal, failed-cutover rollback, explicit recovery and unaffected
  sentinel checks, plus HTTPS/login/CSRF smoke checks and the packaged-source dry run.
  Dry-run release: `11aa82b01e5f5b1e`, private `/tmp/allstocks-release-ql72bs_w/review`.
- Native offline `ssh -G` confirmed the explicit strict trust/identity/agent options parse as
  intended without connecting. Python AST, source whitespace, actionlint and `git diff --check`
  passed. Scoped security inspection identified no credible issue or weakened deployment gate.
- **Not run:** hosted Actions jobs, real GitHub-token API access, actual scheduler concurrency,
  production SSH/credential validity or a live deployment. These remain Phase 3 checks; mocks
  and local Docker fixtures are not represented as live evidence. No production connection,
  GitHub setting change, commit, push or activation occurred. Phase 2 awaits review; Phase 3
  remains `Not Started`.

#### Review Result (2026-11-27)

**Done — Passed Review.** Reviewed the whole CI/CD plan for consistency, the current Phase 2
diff and client helper, and all acceptance criteria. The existing stock-plan release/migration
approvals still govern activation. No blocking, optional or credible security finding remains.
The implementation reuses the existing updater and its server-side gates; it introduces no
alternative cutover path, framework, service or runtime dependency.

| Acceptance criterion | Review evidence | Result |
| --- | --- | --- |
| Only a successfully verified event SHA can deploy | `needs: [verify]`, explicit success result, identical immutable checkout refs, independent helper eligibility/source checks | Passed: implementation/configuration |
| Failed/skipped/cancelled/missing checks prevent deployment | Fresh 180-case evaluation of the actual workflow condition plus runner failure regressions and helper ineligibility tests | Passed: configuration/fixtures |
| Passing non-main pushes/PRs never receive deployment credentials | Job condition excludes these events before the production job; verify job has no deployment environment or secret references | Passed: configuration |
| Local mandatory tests still block failed releases | Existing real-CLI fake-transport regression confirms test failure stops before SSH; default local settings remain independent of GitHub | Passed |
| Stale revisions rejected; active jobs not cancelled; shared lock preserved | Before/after-build main checks tested, including old reruns and advancing main; static concurrency group has cancellation disabled; ordinary updater enters unchanged server lock | Passed: code/fixtures/configuration |
| Invalid settings/trust/target/deployment fail | Native test-key/host validation, mismatched/fingerprint/hashed host cases, file privacy tests, existing destination-identity checks and failed-updater tests | Passed |
| Existing updater, server environment and safety gates preserved | CI invokes update without `--env`; lock/drain/snapshot/compatibility/health/rollback logic unchanged; prior real Docker drill proves rollback/data/environment preservation | Passed |
| Local update/dry-run/recovery/Compose remain available | Optional SSH-config argument preserves default client behavior; local tests and prior dry-run/recovery drill passed; Compose files unchanged | Passed |
| Safe source/release outcome reporting | Validated hash fields and constant status/stage text only; raw tool/exception output withheld; tests assert token/private-key/error exclusion and temporary-key cleanup | Passed |

Fresh review verification passed: **39 focused tests** (3 CI runner and 36 deployment tests),
actionlint 1.7.12, separate Bash syntax checks, `git diff --check` and the 180 workflow-condition
cases. Compared the current helper, manager, release packager, deployment tests and operator docs
byte-for-byte with the allowlisted source from the successful full verification run above.
The prior **258 application tests**, Docker setup/update/rollback/recovery, HTTPS/CSRF and release
dry-run evidence therefore applies; those expensive suites were not repeated during this review.
Preserved the pre-existing, comment-only `.env.example` edit rather than treating it as phase work.

Reconciled the older strategy's proposed CI write to `deploy/server.env` with the implemented
temporary `--server-config` design; the superseded path is marked explicitly above. This review
changed only plan status/consistency/evidence, not implementation or GitHub/server configuration.

Limits: this is a same-session review, not independent external review. Hosted Actions execution,
live token/SSH validity, real scheduler concurrency and the approved production rollout remain
**not verified** and belong to Phase 3. No criterion was waived or presented as tested live.
No commit, push, secret change or deployment occurred. Phase 3 is now eligible by dependency
but remains `Not Started`; the plan remains active because that phase is unfinished.

### Phase 3: Validate and enable production deployment; finish operator docs

**Status:** Blocked — smoke fix verified locally; updated dev publication/hosted pass and stock release approval pending
**Depends on:** Phase 2

#### Goal

Enable tested `main` deployment against an approved compatible release and verify local fallback.

#### Scope

Read-only readiness inventory, hosted verification and necessary CI runner compatibility fixes,
required GitHub configuration, one monitored automatic rollout, local fallback rehearsal, and
updates to README, rollout/deployment docs and this plan's evidence.

#### Do Not Change

Application/data semantics, provider credentials, stock-plan approval requirements, unrelated
TradingSupport services or shared host/network security policy.

#### Reuse First

The guarded workflow, existing deployment readiness/snapshots and isolated rollback drill.

#### Steps

1. Confirm hosted CI results, repository/environment feature support, verified SSH trust and
   access. Configure the dedicated automation key/settings privately without printing values.
2. Confirm the exact candidate respects the stock plan's Phase 5 release/migration approval
   and the existing compatibility gate. Record any unmet release requirement as a blocker;
   keep auto-deployment off rather than bypassing the stock plan.
3. Configure supported environment/branch protections so only trusted `main` releases deploy;
   document actual settings and limitations. GitHub feature availability must be established
   before promising enforced PR checks or environment restrictions.
4. Once readiness is verified and activation is authorized, enable `ENABLE_AUTO_DEPLOY` for
   the next authorized merge. Observe verification before SSH, release identity, backup,
   app health and untouched shared services. Record the hosted run link and source SHA.
5. Verify that disabling the switch prevents a subsequent deployment, rehearse local update
   and recovery in isolated fixtures, then restore the intended enabled configuration.
6. Document triggers, settings, required checks, stale/failed/busy behavior, cancellation and
   recovery, disablement and local commands. Limit edits to CI/CD text; stock documentation
   changes reserved to the stock plan remain there. Record verification limits honestly.

#### Acceptance Criteria

- A real authorized `main` update has a linked successful verification followed by successful
  deployment of that same source/content release; the protected app is healthy afterward.
- Snapshot/release evidence exists, schema/data approvals were respected, production secrets
  remain server-side and unrelated services keep their identities and health.
- Hosted evidence shows tests run automatically on `main` and a non-`main` branch, including
  with deployment disabled, and that passing non-`main` checks never start deployment.
  A failed verification and a disabled switch are each demonstrated to prevent deployment;
  the isolated failed-cutover drill proves rollback preserves both datasets and environment.
- Local dry run and update/recovery rehearsal work with GitHub deployment disabled. No live
  fault injection or second live deployment is required solely to prove fallback.
- Operator docs explain test-before-deploy behavior and both supported local deployment paths,
  list actual configuration and describe how to disable CI deployment and recover manually.
- Each criterion has recorded evidence; missing hosted access/results or release approval
  leaves this phase incomplete with the exact blocker, never presumed passed.

#### Verification

Use hosted job history plus source/release IDs, private backup references, updater readiness,
read-only database checks and app-scoped service inspection. Verify unauthenticated public
requests remain rejected; use existing isolated smoke tests for authenticated/CSRF failures.
Run the local dry run, isolated fallback/rollback checks and `git diff --check`.

#### Security Check

Confirm secret-free logs/artifacts, actual branch/environment restrictions, SSH trust,
private upstream/authentication and no unexpected shared-service or permission changes.

#### Readiness Inventory and Blockers (2026-11-27)

- Confirmed dependency: Phase 2 is `Done — Passed Review`. Read the complete phase and
  acceptance criteria, checked the active plans' release boundaries and set `In Progress`
  before this read-only inventory. The local working tree was initially clean.
- Candidate: local `dev`, commit `ff0a082dd58980c4bd173c0fb0c8c93fd7eaeee3`.
  CI/CD commits `dd09ba8` (verification) and `ff0a082` (guarded deployment) are committed
  locally by the owner but remain unpublished. No commit or push was performed by this agent.
- Fresh `git ls-remote --heads origin` returned remote `dev` at
  `5b19f9ca48c47b9eccca48b92eb678484f9f7b6b` and remote `main` at
  `982f469fcc7b192e23b34009eab1bfaefb106c87`. Thus neither remote branch has the CI/CD commits.
- Public GitHub REST inventory returned **0 workflows** and **0 Actions runs** for
  `Afanyi/HighLowDb`. No hosted passing, failing, disabled-deployment or non-main workflow
  evidence exists yet. Publication to a non-main branch and successful hosted verification
  are prerequisites; tests can run there without enabling deployment.
- The stock plan still records Phase 2 `In Progress`, Phase 3 `Blocked`, and Phases 4/5
  `Not Started`. `docs/REBUILD_REPORT.md` does not exist. There is no recorded Phase 5
  approved report, migration rehearsal or reviewed stock release for this candidate.
  CI/CD section 3 explicitly requires that activation "waits for that plan's Phase 5
  approval and reviewed release" when unreleased stock behavior is included. Starting CI/CD
  Phase 3 does not waive those separate data/release approvals or authorize stock-plan work.
- No authenticated GitHub administration tool is currently available (`gh` absent and
  `GH_TOKEN`/`GITHUB_TOKEN` unset; token values were not read or printed). Public endpoints
  suffice for the inventory above. Actual repository/environment variables, secret-name
  presence, branch protections and the production environment's settings remain unverified;
  do not infer that the owner has not configured them. No settings were changed.

**Required before resuming activation:**

1. Publish the existing CI/CD commits to a non-main branch and obtain hosted verification
   results with automatic deployment disabled. Publishing/merging is not authorized by
   this inventory; do not manufacture a successful hosted result from local test evidence.
2. Complete the stock plan's required report/migration/release approval for the exact
   candidate, following that plan's own dependencies and scope. Do not bypass compatibility
   gates or silently approve the stock release as part of CI/CD activation.
3. Establish authenticated GitHub settings verification/administration, verify the actual
   production environment and trusted SSH configuration privately, then carry out this
   phase's monitored rollout, disablement test, local fallback rehearsal and operator evidence.

No production SSH, provider request, live database operation, server change, GitHub setting
change, secret upload, enablement, merge or deployment occurred. Existing local/manual
deployment remains available with its mandatory checks and stock-release approval requirements.
Earlier local regression/rollback evidence remains recorded under Phases 1/2; no additional
local suite or dry run was repeated after the blocking prerequisites were established.
No Phase 3 acceptance criterion is marked passed. The phase and plan remain unfinished.

#### Hosted dev Failure Investigation (2026-11-27)

- Owner reported the failed `dev` pipeline. Public Actions inventory now confirms push run
  [45981224379](https://github.com/Afanyi/HighLowDb/actions/runs/45981224379) for commit
  `ff0a082dd58980c4bd173c0fb0c8c93fd7eaeee3`. This supersedes the earlier unpublished-commit
  and zero-workflow/run readiness blocker; the earlier inventory is retained as dated history.
- Checkout succeeded. The **Application and deployment checks** job failed in **Run application
  and deployment checks**, from `2026-11-27T10:20:16Z` to `10:22:45Z`. The **Deploy production**
  job was **skipped**. This supplies real hosted evidence that the failed dev verification did
  not enter deployment; it is not a passing hosted verification or production-rollout result.
- Public check annotations report only `Process completed with exit code 1.` They do not
  identify a failing internal command, assertion or dependency. The logs-download endpoint
  returns HTTP 403 without authentication. No root cause has been claimed from the exit code.
- No installed GitHub plugin or authenticated CLI/token is currently available. The plugin
  directory does offer GitHub CI debugging; it was suggested for authenticated access, with
  no connection claimed. Requested the final error lines from the failed step as an alternate
  diagnostic source. Do not request or paste a production key/token to obtain those logs.
- The existing runner and Docker drills were inspected for runner differences. No code or
  security gate was changed speculatively without the detailed failure evidence. Previously
  passing local checks are not treated as reproducing this hosted failure.
- Next: obtain the actual error through authenticated logs or the owner's excerpt, reproduce
  it with a targeted fixture/environment, make the smallest CI-scoped correction, and verify
  it before requesting another authorized dev push/run. Stock release/report approval remains
  separately required before production activation. No GitHub setting, secret, production
  server, deployment, commit or push was changed by this investigation.

#### Authenticated Log Diagnosis and CI Compatibility Repair (2026-11-27)

- The owner installed and connected the GitHub plugin. Authenticated job-log retrieval now
  succeeds; this supersedes the missing-log/plugin limitation above. The existing stock
  release approval and unverified administration settings remain separate activation blockers.
- Hosted logs show 258 application tests, 36 deployment tests and all real Docker deployment
  drills passed. Failure occurs at the first production smoke-test Compose configuration
  command. Hosted Compose is **v2.38.2**, compared with local **v5.3.1**.
- Reproduced with the official v2.38.2 binary, verified against its published SHA-256, and
  disposable copies of both Compose files without a `.env`: the existing command exits 1
  with `env file .../.env not found`. Setting test-only `APP_ENV_FILE=/dev/null` makes that
  exact command exit 0. `--env-file /dev/null` controls interpolation but does not select
  the app service's separate `env_file`; `--no-env-resolution` alone is insufficient here.
- Checked the whole plan against this finding. The narrow smoke-test compatibility repair
  belongs to Phase 3 hosted verification; completed Phase 1/2 evidence is retained with its
  local-version limits. No application/data behavior, mandatory check, CI failure handling,
  deployment gate, real environment or production approval requirement changes.
- Set Phase 3 `In Progress` for the confirmed repair. Production activation remains blocked;
  repair verification and another hosted dev result are still pending.

#### Repair Verification and Remaining Blockers (2026-11-27)

- `tests/check_production.py` now supplies test-only `APP_ENV_FILE=os.devnull`, alongside
  its existing dummy settings and no-env-resolution option. Both Compose files and all
  application/deployment behavior remain unchanged. No real `.env` was created or copied.
- **Passed:** the full production smoke test under Compose **v2.38.2** and host Python 3.12,
  using a disposable source copy without `.env`. Only Compose commands were routed to the
  checksum-verified older binary; Docker operations used the ordinary local daemon. All
  existing production-config assertions, verified TLS, missing/wrong authentication checks,
  both dataset/health routes and both routes' CSRF/origin checks passed. The test cleaned up
  its own containers/network. An initial sandbox-only Docker access failure was rerun with
  approval; it is not counted as a test pass.
- **Passed:** the same empty-service-env configuration renders with local Compose **v5.3.1**;
  assertions confirm no published app port and an explicit `/dev/null` service env-file path.
  Fresh `python3 -m unittest tests.check_ci tests.check_deploy -q` passed **39 tests**;
  `git diff --check` passed. The previously successful hosted 258 application tests and Docker
  deployment drills were not repeated because this repair changes only smoke-test configuration.
- Same-session scoped review found no unrelated implementation changes or credible security
  issue. Mandatory checks, authentication/CSRF assertions, strict failure handling, main-only
  deployment and server-side secrets/approval requirements remain intact. No test was skipped
  or made allowed-to-fail to address this error.
- Phase 3 remains incomplete: the owner must publish the local repair to `dev` for fresh
  hosted verification; this task did not commit/push or rerun the unchanged failing revision.
  A passing hosted run is not claimed. Stock-plan report/release approval and the remaining
  configuration/monitored-production acceptance criteria still block activation. No GitHub
  setting, secret, production server, deployment or other plan phase was changed.

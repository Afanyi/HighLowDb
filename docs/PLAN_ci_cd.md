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
   and ignored `deploy/server.env`; run `bash deploy/update.sh` and clean up credentials.
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

**Status:** Not Started
**Depends on:** Phase 1

#### Goal

Add the automatic deployment path with its enablement switch off and retain local deployment.

#### Scope

The workflow deployment job, minimal deployment integration/messages and meaningful additions
to `tests/check_deploy.py` for any changed client behavior. Document proposed GitHub settings.

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

### Phase 3: Validate and enable production deployment; finish operator docs

**Status:** Not Started
**Depends on:** Phase 2

#### Goal

Enable tested `main` deployment against an approved compatible release and verify local fallback.

#### Scope

Read-only readiness inventory, required GitHub configuration, one monitored automatic rollout,
local fallback rehearsal, and updates to README, rollout/deployment docs and this plan's evidence.

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
